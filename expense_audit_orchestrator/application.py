from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from expense_audit_orchestrator.runtime_client import DEFAULT_GRAPH_PATH, GraphRuntimeClient

from .core import DEFAULT_OCR_PATH, ReceiptDataPreparer
from .observability import get_logger, new_run_id, run_context
from .overall_advice import OverallAdviceProvider, resolve_llm_evaluate_endpoint

if TYPE_CHECKING:
    from .profiles import ExpenseProfile, ProfileResolver


InvoiceResultSink = Callable[[str, dict[str, Any]], None]
ReceiptResultSink = Callable[[dict[str, Any]], None]


class ReceiptAuditService:
    def __init__(
        self,
        graph_runtime_client: GraphRuntimeClient,
        data_preparer: ReceiptDataPreparer,
        *,
        graph_path: Path | str | None = None,
        graph_content: dict[str, Any] | str | None = None,
        profile_resolver: ProfileResolver | None = None,
        invoice_result_sink: InvoiceResultSink | None = None,
        receipt_result_sink: ReceiptResultSink | None = None,
        run_id_factory: Callable[[], str] = new_run_id,
        overall_advice_provider: OverallAdviceProvider | None = None,
        audit_service_url: str | None = None,
    ) -> None:
        # 动态路由模式（profile_resolver）与静态模式（graph_path/graph_content）互斥：
        # - 动态模式：每单据按 eiCode 路由到不同 profile，图路径由 profile.default_graph_path 决定
        # - 静态模式：所有单据用同一图（现有行为，向后兼容）
        if profile_resolver is not None:
            if graph_path is not None or graph_content is not None:
                raise ValueError(
                    "profile_resolver cannot be used together with graph_path or graph_content; "
                    "use either dynamic routing (profile_resolver) or static graph (graph_path/graph_content)"
                )
        elif graph_content is None and graph_path is None:
            # 静态模式默认用 DEFAULT_GRAPH_PATH（向后兼容）
            graph_path = DEFAULT_GRAPH_PATH
        elif graph_path is not None and graph_content is not None:
            raise ValueError("graph_path and graph_content cannot be set together")

        self._graph_runtime_client = graph_runtime_client
        self._data_preparer = data_preparer
        self._graph_path = graph_path
        self._graph_content = graph_content
        self._profile_resolver = profile_resolver
        self._invoice_result_sink = invoice_result_sink or _noop_invoice_result_sink
        self._receipt_result_sink = receipt_result_sink or _noop_receipt_result_sink
        self._run_id_factory = run_id_factory
        self._overall_advice_provider = overall_advice_provider
        self._audit_service_url = audit_service_url
        # LLM 网关端点：统一从 .env 的 NODE_GATEWAY_URL 解析，注入到图节点 context.llmGatewayUrl，
        # 让所有费用流程图共用同一配置，避免图 JSON 内硬编码 IP 地址。
        self._llm_evaluate_endpoint = resolve_llm_evaluate_endpoint()

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
        # 动态路由模式：先 fetch audit_info 拿 eiCode → resolve profile → 用 profile enricher 准备数据
        resolved_profile: ExpenseProfile | None = None
        enrichers_override: Mapping | None = None
        invoice_enrichers_override: Mapping | None = None
        if self._profile_resolver is not None:
            resolved_profile = self._resolve_profile_for_receipt(receipt_code)
            enrichers_override = resolved_profile.receipt_enrichers
            invoice_enrichers_override = resolved_profile.invoice_enrichers

        receipt_context = self._data_preparer.prepare_receipt_context(
            receipt_code,
            receipt_enrichers_override=enrichers_override,
        )
        invoice_preparations: list[dict[str, Any]] = []

        for invoice_file in receipt_context["invoiceFiles"]:
            invoice_prepare_kwargs: dict[str, Any] = {}
            if invoice_enrichers_override:
                invoice_prepare_kwargs["extra_enrichers_override"] = invoice_enrichers_override
            try:
                prepared_input = self._data_preparer.prepare_invoice_input(
                    receipt_code,
                    invoice_file,
                    receipt_context,
                    resolved_ocr_sample_path,
                    **invoice_prepare_kwargs,
                )
            except TypeError as exc:
                # 兼容旧版/测试替身 DataPreparer：老接口没有发票级 enricher 参数。
                if "extra_enrichers_override" not in str(exc) or not invoice_prepare_kwargs:
                    raise
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

        prepared_receipt = {
            "receiptCode": receipt_code,
            "serviceData": dict(receipt_context.get("serviceData") or {}),
            "receiptContext": receipt_context,
            "invoiceCount": len(invoice_preparations),
            "invoicePreparations": invoice_preparations,
            "summary": {
                "invoiceCount": len(invoice_preparations),
                "preparedCount": len(invoice_preparations),
            },
            # 动态路由模式下记录选中的 profile，供 process_prepared_receipt 选图
            "resolvedProfile": resolved_profile,
        }
        # 所有发票完成数据准备后一次性计算本单出租车连票关系，供执行前读取；
        # process_prepared_receipt() 会幂等重算，兼容从队列/磁盘加载的 prepared receipt。
        _inject_taxi_invoice_batch_context(prepared_receipt)
        return prepared_receipt

    def _resolve_profile_for_receipt(self, receipt_code: str) -> ExpenseProfile:
        """动态路由：fetch audit_info 拿 eiCode → resolver.resolve → ExpenseProfile。

        audit_info 在 prepare_receipt_context 内部也会 fetch，这里提前 fetch 一次用于路由。
        为避免重复请求，prepare_receipt_context 复用底座的 audit_info_provider 即可
        （底座 fetch 是幂等查询，重复一次开销可接受；后续可优化为传入 audit_info_override）。
        """
        audit_info = self._data_preparer.audit_info_provider(receipt_code)
        ei_code = _get_audit_info_ei_code(audit_info)
        profile = self._profile_resolver.resolve(
            ei_code,
            service_url=self._audit_service_url,
        )
        get_logger("profile_routing").info(
            "resolved expense profile by eiCode",
            extra={
                "receipt_code": receipt_code,
                "ei_code": ei_code,
                "profile": profile.name,
                "event": "profile_routing.resolved",
            },
        )
        return profile

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
        # 所有发票已完成数据准备后一次性计算本单出租车连票关系，确保连票组中的
        # 第一张发票也能命中 W19，不受发票执行顺序影响；兼容队列/磁盘加载数据。
        _inject_taxi_invoice_batch_context(prepared_receipt)
        remaining_apply_amount = _resolve_initial_apply_amount(prepared_receipt)

        # 动态路由模式：从 prepared_receipt 取出选中的 profile，用其 graph_path 执行
        resolved_profile: ExpenseProfile | None = prepared_receipt.get("resolvedProfile")
        effective_graph_path = self._graph_path
        if resolved_profile is not None and resolved_profile.default_graph_path is not None:
            effective_graph_path = resolved_profile.default_graph_path

        previous_invoice_numbers: list[str] = []

        for invoice_preparation in prepared_receipt["invoicePreparations"]:
            if remaining_apply_amount is not None:
                _update_preparation_apply_amount(invoice_preparation, remaining_apply_amount)

            # 出租车连号检测：注入本张发票之前已处理的所有发票号，供图中 W19 节点判断连号
            _inject_previous_invoice_numbers(invoice_preparation, previous_invoice_numbers)

            invoice_result = self._process_invoice_preparation(
                receipt_code, invoice_preparation, graph_path=effective_graph_path,
            )
            invoice_results.append(invoice_result)

            # 收集当前发票号用于后续发票的连号检测
            _collect_invoice_number(invoice_preparation, previous_invoice_numbers)

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
            "resolvedProfile": resolved_profile,
        }
        # 核销单级整体建议：所有发票跑完后、回写 sink 前生成。
        self._augment_with_overall_advice(receipt_result)
        self._receipt_result_sink(receipt_result)
        return receipt_result

    def _process_invoice_preparation(
        self,
        receipt_code: str,
        invoice_preparation: Mapping[str, Any],
        *,
        graph_path: Path | str | None = None,
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
            execution_time=started_at,
            llm_gateway_url=self._llm_evaluate_endpoint,
        )

        # 动态路由模式下用传入的 graph_path（来自 resolved profile），静态模式用 self._graph_path
        effective_graph_path = graph_path if graph_path is not None else self._graph_path

        try:
            if not prepared_input:
                raise ValueError("preparedInput is required for invoice execution")

            with run_context(receipt_code=receipt_code, run_id=run_id, invoice_key=invoice_key):
                runtime_result = self._graph_runtime_client.evaluate(
                    prepared_input=prepared_input,
                    graph_path=effective_graph_path,
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
        """调用整体建议 provider，把结果挂到 receipt_result['aiAuditAdvice']。

        任何异常都吞掉并记日志——整体建议是增强项，绝不能打断审计主链路。
        """
        provider = self._overall_advice_provider
        if provider is None:
            return
        receipt_code = str(receipt_result.get("receiptCode") or "")
        invoice_results = receipt_result.get("invoiceResults") or []
        try:
            with run_context(receipt_code=receipt_code, run_id=None, invoice_key=None):
                advice = provider(
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
        if isinstance(advice, str) and advice.strip():
            receipt_result["aiAuditAdvice"] = advice.strip()


def _get_audit_info_ei_code(audit_info: Any) -> str:
    """从 audit_info 中提取 eiCode（费用项编码），用于路由到对应 profile。"""
    if not isinstance(audit_info, Mapping):
        return ""
    for key in ("eiCode", "ei_code", "expenseItemCode"):
        value = audit_info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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
    execution_time: str | None = None,
    llm_gateway_url: str | None = None,
) -> None:
    """把 runId/receiptCode/invoiceKey/executionTime/llmGatewayUrl 注入 preparedInput.context。

    runId/receiptCode/invoiceKey 供图内 LLM 节点透传给 node_gateway；
    executionTime 作为各稽核点 decisionTable 输出列 create_time 的取值来源
    （图内规则用 `context.executionTime` 引用），即「该发票本次执行的时刻」；
    llmGatewayUrl 供图内 LLM 节点读取网关地址（统一从 .env 配置，避免图内硬编码 IP）。
    """
    if not isinstance(prepared_input, dict):
        return
    context = prepared_input.get("context")
    if not isinstance(context, dict):
        context = {}
        prepared_input["context"] = context
    context.setdefault("runId", run_id)
    context.setdefault("receiptCode", receipt_code)
    context.setdefault("invoiceKey", invoice_key)
    if execution_time is not None:
        context.setdefault("executionTime", execution_time)
    if llm_gateway_url:
        context.setdefault("llmGatewayUrl", llm_gateway_url)


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


def _inject_taxi_invoice_batch_context(prepared_receipt: Mapping[str, Any]) -> None:
    """为每张已准备发票注入本核销单出租车连票结果。"""
    invoice_preparations = prepared_receipt.get("invoicePreparations")
    if not isinstance(invoice_preparations, list):
        return

    grouped: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    candidates: list[tuple[dict[str, Any], dict[str, Any], str | None, bool]] = []

    for item in invoice_preparations:
        if not isinstance(item, dict):
            continue
        prepared_input = item.get("preparedInput")
        if not isinstance(prepared_input, dict):
            continue
        service_data = prepared_input.get("serviceData")
        if not isinstance(service_data, dict):
            continue
        serial_data = service_data.get("taxiInvoiceSerial")
        if not isinstance(serial_data, dict):
            continue

        invoice_no = str(serial_data.get("invoiceNo") or "").strip()
        current_prefix = str(serial_data.get("currentPrefix") or "").strip() or None
        is_taxi_invoice = bool(serial_data.get("isTaxiInvoice"))
        candidates.append((prepared_input, serial_data, current_prefix, is_taxi_invoice))
        if is_taxi_invoice and current_prefix and invoice_no:
            grouped[current_prefix].append((prepared_input, invoice_no))

    for prepared_input, serial_data, current_prefix, is_taxi_invoice in candidates:
        peer_invoice_numbers: list[str] = []
        if is_taxi_invoice and current_prefix:
            peer_invoice_numbers = [
                invoice_no
                for other_prepared_input, invoice_no in grouped[current_prefix]
                if other_prepared_input is not prepared_input and invoice_no
            ]

        serial_data["batchPeerInvoiceNumbers"] = peer_invoice_numbers
        serial_data["batchHit"] = bool(peer_invoice_numbers)

        service_data = prepared_input.get("serviceData")
        if not isinstance(service_data, dict):
            continue
        service_data["taxiInvoiceSerial"] = serial_data
        prepared_input["serviceData"] = service_data
        context = prepared_input.get("context")
        if not isinstance(context, dict):
            context = {}
            prepared_input["context"] = context
        context["serviceData"] = service_data


def _inject_previous_invoice_numbers(
    invoice_preparation: Mapping[str, Any],
    previous_invoice_numbers: list[str],
) -> None:
    """兼容保留旧的前序发票号上下文；W19 已不再读取该字段。"""
    prepared_input = invoice_preparation.get("preparedInput")
    if not isinstance(prepared_input, dict):
        return
    prepared_input["previousInvoiceNumbers"] = list(previous_invoice_numbers)


def _collect_invoice_number(
    invoice_preparation: Mapping[str, Any],
    previous_invoice_numbers: list[str],
) -> None:
    """从发票准备数据中提取 invoiceNo，追加到前序发票号列表中。"""
    invoice_file = invoice_preparation.get("invoiceFile")
    if not isinstance(invoice_file, dict):
        invoice_file = {}
    invoice_no = str(invoice_file.get("invoiceNo") or "")
    if invoice_no:
        previous_invoice_numbers.append(invoice_no)


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


# reject/failed 时仍允许发票「扣减后 finalAmount」计入 E31 有效合计的稽核码。
# - E31：金额充足度本身，不否定发票内容有效性（单据级判定，回写层单独处理）。
# - E34：发票明细含禁止报销项，LLM 已扣减并返回扣减后 finalAmount；按扣减后金额计入。
# 其余 reject/failed（sys-001 伪造 / E09 黑名单 / E05 重复 / sys-003 作废 / sys-004 红冲 /
# E17 充值卡 / E01 抬头 / E02 税号 …）视为整张无效，finalAmount 不计入（计 0）。
_INVOICE_FINAL_AMOUNT_EXEMPT_RULE_CODES = frozenset({"E31", "E34"})


def _iter_decision_rule_results(decision_output: Mapping[str, Any]) -> list[dict[str, Any]]:
    """提取 decisionOutput 下各决策表节点产出的结构化规则结果。"""
    rule_results: list[dict[str, Any]] = []
    for value in decision_output.values():
        if isinstance(value, Mapping) and any(
            key in value
            for key in ("distinguish_result", "reason_code", "audit_content", "audit_type")
        ):
            rule_results.append(dict(value))
    return rule_results


def _invoice_contributes_valid_amount(invoice_result: Mapping[str, Any]) -> bool:
    """该发票的扣减后 finalAmount 是否计入 E31 有效合计。

    - 执行失败（executionStatus != SUCCEEDED）→ 不计入。
    - 整体 reject/failed 时，若存在豁免集之外的 reject/failed 规则 → 整张无效，不计入。
    - 仅 E31（金额充足度）或 E34（内容扣减）reject/failed 时仍计入：
      E31 不否定内容；E34 按 LLM 返回的扣减后 finalAmount 计入（取不到时计 0）。
    - 无结构化子规则结果时，保守视为不计入（与历史行为一致）。
    """
    if invoice_result.get("executionStatus") != "SUCCEEDED":
        return False

    decision_status = str(invoice_result.get("decisionStatus") or "").lower()
    if decision_status not in ("failed", "reject"):
        return True

    decision_output = invoice_result.get("decisionOutput") or {}
    rule_results = _iter_decision_rule_results(decision_output)
    # 无结构化子规则结果时，保守视为不可计入（与历史行为一致）。
    if not rule_results:
        return False

    for rule_result in rule_results:
        reason_code = rule_result.get("reason_code") or rule_result.get("reasonCode")
        distinguish_result = str(
            rule_result.get("distinguish_result") or rule_result.get("distinguishResult") or ""
        ).lower()
        if (
            reason_code not in _INVOICE_FINAL_AMOUNT_EXEMPT_RULE_CODES
            and distinguish_result in ("reject", "failed")
        ):
            return False
    return True


def _extract_invoice_final_amount(invoice_result: Mapping[str, Any]) -> float | None:
    if not _invoice_contributes_valid_amount(invoice_result):
        return None

    # decisionOutput 既在 invoice_result 顶层、也在 runtimeResult 下（两处等价）。
    # 真实图里 E34 节点 outputPath=invoice_content_valid_result，引擎把它的输出嵌套在
    # decisionOutput["invoice_content_valid_result"] 下；扁平 mock 则把 finalAmount 放在
    # decisionOutput 顶层。两处形状都兼容，与 writeback._sum_invoice_final_amounts 对齐。
    decision_output = invoice_result.get("decisionOutput")
    if not isinstance(decision_output, Mapping):
        decision_output = (invoice_result.get("runtimeResult") or {}).get("decisionOutput") or {}

    candidates = [
        decision_output.get("invoice_finalAmount"),
        (decision_output.get("invoice_content_valid_result") or {}).get("invoice_finalAmount"),
    ]
    for final_amount in candidates:
        if final_amount is None:
            continue
        try:
            return float(final_amount)
        except (ValueError, TypeError):
            continue
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