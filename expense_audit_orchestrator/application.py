from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from expense_audit_orchestrator.runtime_client import DEFAULT_GRAPH_PATH, GraphRuntimeClient

from .core import DEFAULT_OCR_PATH, ReceiptDataPreparer
from .observability import get_logger, new_run_id, run_context
from .overall_advice import OverallAdviceProvider


InvoiceResultSink = Callable[[str, dict[str, Any]], None]
ReceiptResultSink = Callable[[dict[str, Any]], None]


class ReceiptAuditService:
    def __init__(
        self,
        graph_runtime_client: GraphRuntimeClient,
        data_preparer: ReceiptDataPreparer,
        *,
        graph_path: Path | str | None = DEFAULT_GRAPH_PATH,
        graph_content: dict[str, Any] | str | None = None,
        invoice_result_sink: InvoiceResultSink | None = None,
        receipt_result_sink: ReceiptResultSink | None = None,
        run_id_factory: Callable[[], str] = new_run_id,
        overall_advice_provider: OverallAdviceProvider | None = None,
    ) -> None:
        if (graph_path is None) == (graph_content is None):
            raise ValueError("exactly one of graph_path or graph_content is required")

        self._graph_runtime_client = graph_runtime_client
        self._data_preparer = data_preparer
        self._graph_path = graph_path
        self._graph_content = graph_content
        self._invoice_result_sink = invoice_result_sink or _noop_invoice_result_sink
        self._receipt_result_sink = receipt_result_sink or _noop_receipt_result_sink
        self._run_id_factory = run_id_factory
        self._overall_advice_provider = overall_advice_provider

    def prepare_input(
        self,
        receipt_code: str,
        ocr_sample_path: Path | str | None = None,
    ) -> dict[str, Any]:
        return self._data_preparer.prepare(receipt_code, ocr_sample_path or DEFAULT_OCR_PATH)

    def prepare_receipt(
        self,
        receipt_code: str,
        ocr_sample_path: Path | str | None = None,
    ) -> dict[str, Any]:
        resolved_ocr_sample_path = ocr_sample_path or DEFAULT_OCR_PATH
        receipt_context = self._data_preparer.prepare_receipt_context(receipt_code)
        invoice_preparations: list[dict[str, Any]] = []

        for invoice_file in receipt_context["invoiceFiles"]:
            prepared_input = self._data_preparer.prepare_invoice_input(
                receipt_code,
                invoice_file,
                receipt_context,
                resolved_ocr_sample_path,
            )
            invoice_preparations.append(
                {
                    "invoiceKey": _resolve_invoice_key(invoice_file),
                    "invoiceFile": dict(invoice_file),
                    "preparedInput": prepared_input,
                }
            )

        return {
            "receiptCode": receipt_code,
            "serviceData": dict(receipt_context.get("serviceData") or {}),
            "receiptContext": receipt_context,
            "invoiceCount": len(invoice_preparations),
            "invoicePreparations": invoice_preparations,
            "summary": {
                "invoiceCount": len(invoice_preparations),
                "preparedCount": len(invoice_preparations),
            },
        }

    def process_receipt(
        self,
        receipt_code: str,
        ocr_sample_path: Path | str | None = None,
    ) -> dict[str, Any]:
        prepared_receipt = self.prepare_receipt(receipt_code, ocr_sample_path)
        return self.process_prepared_receipt(prepared_receipt)

    def process_prepared_receipt(
        self,
        prepared_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        invoice_results: list[dict[str, Any]] = []

        receipt_code = str(prepared_receipt.get("receiptCode") or "")
        remaining_apply_amount = _resolve_initial_apply_amount(prepared_receipt)

        for invoice_preparation in prepared_receipt["invoicePreparations"]:
            if remaining_apply_amount is not None:
                _update_preparation_apply_amount(invoice_preparation, remaining_apply_amount)

            invoice_result = self._process_invoice_preparation(receipt_code, invoice_preparation)
            invoice_results.append(invoice_result)

            used_amount = _extract_invoice_final_amount(invoice_result)
            if used_amount is not None and remaining_apply_amount is not None:
                remaining_apply_amount -= used_amount

            self._invoice_result_sink(receipt_code, invoice_result)

        receipt_result = {
            "receiptCode": receipt_code,
            "serviceData": dict(prepared_receipt.get("serviceData") or {}),
            "receiptContext": prepared_receipt["receiptContext"],
            "invoiceCount": len(invoice_results),
            "invoiceResults": invoice_results,
            "summary": _build_receipt_summary(invoice_results),
            "isAmountSufficient": (remaining_apply_amount is None or remaining_apply_amount <= 0),
            # Receipt-level amount context used by writeback to build the final
            # E31 message with real totals (有效发票合计 / 报销金额 / 缺少金额).
            "applyAmount": _resolve_initial_apply_amount(prepared_receipt),
            "remainingApplyAmount": remaining_apply_amount,
            "validInvoiceTotal": _resolve_valid_invoice_total(invoice_results, _resolve_initial_apply_amount(prepared_receipt), remaining_apply_amount),
        }
        # 核销单级整体建议：所有发票跑完后、回写 sink 前生成。
        self._augment_with_overall_advice(receipt_result)
        self._receipt_result_sink(receipt_result)
        return receipt_result

    def _process_invoice_preparation(
        self,
        receipt_code: str,
        invoice_preparation: Mapping[str, Any],
    ) -> dict[str, Any]:
        started_at = _utc_now_isoformat()
        run_id = self._run_id_factory()
        prepared_input = _resolve_prepared_input(invoice_preparation)
        invoice_key = str(invoice_preparation.get("invoiceKey") or _resolve_invoice_key(_resolve_invoice_file(invoice_preparation)))
        _inject_run_context(
            prepared_input,
            receipt_code=receipt_code,
            run_id=run_id,
            invoice_key=invoice_key,
        )

        try:
            if not prepared_input:
                raise ValueError("preparedInput is required for invoice execution")

            with run_context(receipt_code=receipt_code, run_id=run_id, invoice_key=invoice_key):
                runtime_result = self._graph_runtime_client.evaluate(
                    prepared_input=prepared_input,
                    graph_path=self._graph_path,
                    graph_content=self._graph_content,
                )
        except Exception as exc:
            return _build_invoice_result(
                receipt_code=receipt_code,
                invoice_preparation=invoice_preparation,
                prepared_input=prepared_input,
                decision_output=_build_failed_decision_output(str(exc)),
                runtime_result=None,
                execution_status="FAILED",
                error_message=str(exc),
                started_at=started_at,
                finished_at=_utc_now_isoformat(),
                run_id=run_id,
            )

        return _build_invoice_result(
            receipt_code=receipt_code,
            invoice_preparation=invoice_preparation,
            prepared_input=prepared_input,
            decision_output=_resolve_decision_output(runtime_result),
            runtime_result=runtime_result,
            execution_status="SUCCEEDED",
            error_message=None,
            started_at=started_at,
            finished_at=_utc_now_isoformat(),
            run_id=run_id,
        )

    def evaluate(
        self,
        receipt_code: str,
        ocr_sample_path: Path | str | None = None,
    ) -> dict[str, Any]:
        prepared_input = self.prepare_input(receipt_code, ocr_sample_path)
        return self._graph_runtime_client.evaluate(
            prepared_input=prepared_input,
            graph_path=self._graph_path,
            graph_content=self._graph_content,
        )

    def _augment_with_overall_advice(self, receipt_result: dict[str, Any]) -> None:
        """调用整体建议 provider，把结果挂到 receipt_result['overallSuggestion']。

        任何异常都吞掉并记日志——整体建议是增强项，绝不能打断审计主链路。
        """
        provider = self._overall_advice_provider
        if provider is None:
            return
        receipt_code = str(receipt_result.get("receiptCode") or "")
        invoice_results = receipt_result.get("invoiceResults") or []
        try:
            with run_context(receipt_code=receipt_code, run_id=None, invoice_key=None):
                suggestion = provider(
                    receipt_code,
                    invoice_results,
                    receipt_context=receipt_result.get("receiptContext"),
                )
        except Exception:
            get_logger("overall_advice").exception(
                "overall advice provider raised",
                extra={
                    "receipt_code": receipt_code,
                    "event": "overall_advice.invocation_failed",
                },
            )
            return
        if isinstance(suggestion, str) and suggestion.strip():
            receipt_result["overallSuggestion"] = suggestion.strip()


def _resolve_invoice_key(invoice_file: Mapping[str, Any]) -> str:
    invoice_key = invoice_file.get("invoiceKey")
    if isinstance(invoice_key, str) and invoice_key.strip():
        return invoice_key.strip()

    fid = invoice_file.get("fid")
    if isinstance(fid, str) and fid.strip():
        return fid.strip()

    return "unknown"


def _resolve_decision_output(runtime_result: Mapping[str, Any]) -> dict[str, Any]:
    decision_output = runtime_result.get("decisionOutput")
    if isinstance(decision_output, dict):
        return dict(decision_output)

    return {}


def _resolve_prepared_input(invoice_preparation: Mapping[str, Any]) -> dict[str, Any]:
    prepared_input = invoice_preparation.get("preparedInput")
    if isinstance(prepared_input, dict):
        return prepared_input

    return {}


def _resolve_invoice_file(invoice_preparation: Mapping[str, Any]) -> dict[str, Any]:
    invoice_file = invoice_preparation.get("invoiceFile")
    if isinstance(invoice_file, Mapping):
        return dict(invoice_file)

    return {}


def _resolve_decision_status(decision_output: Mapping[str, Any]) -> str:
    check_status = decision_output.get("checkStatus")
    if isinstance(check_status, str) and check_status.strip():
        return check_status.strip().lower()

    return "unknown"


def _build_failed_decision_output(error_message: str) -> dict[str, Any]:
    return {
        "checkStatus": "failed",
        "message": error_message,
    }


def _inject_run_context(
    prepared_input: dict[str, Any],
    *,
    receipt_code: str,
    run_id: str,
    invoice_key: str,
) -> None:
    """把 runId/receiptCode/invoiceKey 注入 preparedInput.context，供图内 LLM 节点透传给 node_gateway。"""
    if not isinstance(prepared_input, dict):
        return
    context = prepared_input.get("context")
    if not isinstance(context, dict):
        context = {}
        prepared_input["context"] = context
    context.setdefault("runId", run_id)
    context.setdefault("receiptCode", receipt_code)
    context.setdefault("invoiceKey", invoice_key)


def _build_invoice_result(
    *,
    receipt_code: str,
    invoice_preparation: Mapping[str, Any],
    prepared_input: dict[str, Any],
    decision_output: dict[str, Any],
    runtime_result: dict[str, Any] | None,
    execution_status: str,
    error_message: str | None,
    started_at: str,
    finished_at: str,
    run_id: str = "",
) -> dict[str, Any]:
    invoice_file = _resolve_invoice_file(invoice_preparation)
    return {
        "receiptCode": receipt_code,
        "runId": run_id,
        "invoiceKey": str(invoice_preparation.get("invoiceKey") or _resolve_invoice_key(invoice_file)),
        "invoiceFile": invoice_file,
        "preparedInput": prepared_input,
        "decisionOutput": decision_output,
        "decisionStatus": _resolve_decision_status(decision_output),
        "runtimeResult": runtime_result,
        "executionStatus": execution_status,
        "errorMessage": error_message,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "attempt": 1,
    }


def _build_receipt_summary(invoice_results: list[dict[str, Any]]) -> dict[str, Any]:
    succeeded_count = sum(1 for item in invoice_results if item.get("executionStatus") == "SUCCEEDED")
    failed_count = sum(1 for item in invoice_results if item.get("executionStatus") == "FAILED")
    warning_count = sum(1 for item in invoice_results if item.get("decisionStatus") == "warning")

    overall_status = "SUCCESS"
    if failed_count and succeeded_count:
        overall_status = "PARTIAL_SUCCESS"
    elif failed_count:
        overall_status = "FAILED"

    return {
        "invoiceCount": len(invoice_results),
        "completedCount": len(invoice_results),
        "succeededCount": succeeded_count,
        "failedCount": failed_count,
        "warningCount": warning_count,
        "overallStatus": overall_status,
    }


def _noop_invoice_result_sink(_receipt_code: str, _invoice_result: dict[str, Any]) -> None:
    return None


def _noop_receipt_result_sink(_receipt_result: dict[str, Any]) -> None:
    return None


def _utc_now_isoformat() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_initial_apply_amount(prepared_receipt: Mapping[str, Any]) -> float | None:
    service_data = prepared_receipt.get("serviceData") or {}
    audit_info = service_data.get("auditInfo") or {}
    apply_amount = audit_info.get("applyAmount")
    if apply_amount is not None:
        try:
            return float(apply_amount)
        except (ValueError, TypeError):
            return None
    return None


def _update_preparation_apply_amount(
    invoice_preparation: Mapping[str, Any],
    apply_amount: float,
) -> None:
    prepared_input = invoice_preparation.get("preparedInput")
    if not isinstance(prepared_input, dict):
        return
    service_data = prepared_input.get("serviceData")
    if not isinstance(service_data, dict):
        return
    audit_info = service_data.get("auditInfo")
    if not isinstance(audit_info, dict):
        return
    updated_service_data = deepcopy(service_data)
    updated_service_data["auditInfo"]["applyAmount"] = apply_amount
    prepared_input["serviceData"] = updated_service_data
    context = prepared_input.get("context")
    if isinstance(context, dict):
        context["serviceData"] = updated_service_data


def _is_invoice_valid_excluding_e31(invoice_result: dict[str, Any]) -> bool:
    if invoice_result.get("executionStatus") != "SUCCEEDED":
        return False

    decision_status = invoice_result.get("decisionStatus", "").lower()
    if decision_status in ("failed", "reject"):
        # We need to make sure the rejection/failure was not caused solely by E31.
        # If there are other rule results that reject/fail, then the invoice is invalid.
        # If the only rejecting/failing rule is E31, then we consider it valid (we ignore E31).
        decision_output = invoice_result.get("decisionOutput") or {}
        has_other_rejections = False
        rule_results = []
        for value in decision_output.values():
            if isinstance(value, Mapping) and any(
                key in value
                for key in ("distinguish_result", "reason_code", "audit_content", "audit_type")
            ):
                rule_results.append(value)

        # If there are no structured sub-rule results, fall back to decisionStatus check
        if not rule_results:
            return False

        for r in rule_results:
            reason_code = r.get("reason_code") or r.get("reasonCode")
            distinguish_result = r.get("distinguish_result") or r.get("distinguishResult")
            if reason_code != "E31" and str(distinguish_result).lower() in ("reject", "failed"):
                has_other_rejections = True
                break

        if has_other_rejections:
            return False

    return True


def _extract_invoice_final_amount(invoice_result: dict[str, Any]) -> float | None:
    if not _is_invoice_valid_excluding_e31(invoice_result):
        return None

    runtime_result = invoice_result.get("runtimeResult") or {}
    decision_output = runtime_result.get("decisionOutput") or {}

    final_amount = decision_output.get("invoice_finalAmount")
    if final_amount is not None:
        try:
            return float(final_amount)
        except (ValueError, TypeError):
            pass

    invoice_content_result = runtime_result.get("invoice_content_valid_result") or {}
    final_amount = invoice_content_result.get("invoice_finalAmount")
    if final_amount is not None:
        try:
            return float(final_amount)
        except (ValueError, TypeError):
            pass

    return None


def _resolve_valid_invoice_total(
    invoice_results: list[dict[str, Any]],
    apply_amount: float | None,
    remaining_apply_amount: float | None,
) -> float | None:
    """Sum of valid-invoice final amounts (the amount that counted toward the
    receipt). Reconstructs the total from the applyAmount / remaining shortfall
    so the writeback E31 message can report 有效发票合计金额 / 缺少金额 even when
    individual invoice finalAmounts are unavailable.
    """
    if apply_amount is None or remaining_apply_amount is None:
        # Fall back to summing per-invoice finalAmounts where available.
        total = 0.0
        found = False
        for invoice_result in invoice_results:
            final_amount = _extract_invoice_final_amount(invoice_result)
            if final_amount is not None:
                total += final_amount
                found = True
        return total if found else None
    return apply_amount - remaining_apply_amount