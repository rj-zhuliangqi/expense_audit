from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from expense_audit_orchestrator.runtime_client import DEFAULT_GRAPH_PATH, GraphRuntimeClient

from .core import DEFAULT_OCR_PATH, ReceiptDataPreparer
from .observability import get_logger, new_run_id, run_context
from .overall_advice import OverallAdviceProvider, resolve_llm_evaluate_endpoint
from .receipt_summary import (
    build_ai_audit_advice,
    build_ai_audit_summary,
    build_ai_audit_summary_finance,
    extract_valid_invoice_final_amount,
    invoice_contributes_valid_amount,
)

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
        expense_profile: str | None = None,
        audit_risk_catalog: Mapping[str, Mapping[str, Any]] | None = None,
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
        self._expense_profile = expense_profile
        self._audit_risk_catalog = audit_risk_catalog
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
                # 正式 DataPreparer 支持该参数时，E15 票种判断仍由 invoice enricher 注入。
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
        # 先为兼容未进入核销单循环的调用方注入整单总量；正式核销单执行时，
        # process_prepared_receipt() 会按发票顺序改写为当前累计商品数量，最后一张即整单总量。
        _inject_total_goods_count(prepared_receipt)
        # E05 的发票重复检查需要整单视角；在逐票执行前一次性统计本单发票号，
        # 同一发票号的每一张票都注入命中标记。
        _inject_e05_duplicate_context(prepared_receipt)
        # 所有发票完成数据准备后一次性计算本单发票连号关系，供需要在执行前
        # 读取 prepared receipt 的调用方使用；process_prepared_receipt() 会幂等重算一次，
        # 兼容从队列/磁盘加载的既有 prepared receipt。
        _inject_invoice_serial_batch_context(prepared_receipt)
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
        # 兼容从磁盘/队列加载、尚未经过 prepare_receipt 聚合的 prepared receipt。
        _inject_total_goods_count(prepared_receipt)
        # 旧 prepared receipt 可能没有经过本版本的整单聚合；执行前幂等重算，
        # 确保队列重放和磁盘加载路径也能执行 E05 本单重复检查。
        _inject_e05_duplicate_context(prepared_receipt)
        # 所有发票已完成数据准备后一次性计算本单发票连号关系，确保同一
        # 连号组中的每张发票都能命中，不受发票执行顺序影响。
        _inject_invoice_serial_batch_context(prepared_receipt)
        remaining_apply_amount = _resolve_initial_apply_amount(prepared_receipt)

        # 动态路由模式：从 prepared_receipt 取出选中的 profile，用其 graph_path 执行
        resolved_profile: ExpenseProfile | None = prepared_receipt.get("resolvedProfile")
        effective_graph_path = self._graph_path
        if resolved_profile is not None and resolved_profile.default_graph_path is not None:
            effective_graph_path = resolved_profile.default_graph_path

        previous_invoice_numbers: list[str] = []
        previous_w34_invoice_numbers: list[str] = []
        gift_count_context = _resolve_gift_count_context(prepared_receipt)
        gift_lookup_status, gift_lookup_error = _resolve_gift_count_lookup_status(
            prepared_receipt
        )
        cumulative_goods_count = 0.0
        invoice_preparations = prepared_receipt["invoicePreparations"]
        amount_resolution_unknown = False
        # 差旅文档级规则（税额、补贴、场站、自驾等）只在首张发票执行；
        # 逐张规则仍对每张发票执行。提前汇总全部发票税额，避免首张票只
        # 比较当前票税额而漏掉后续票。
        travel_invoice_tax_total = _resolve_travel_invoice_tax_total(invoice_preparations)
        raised_travel_rule_codes: list[str] = []
        raised_travel_rule_keys: list[str] = []

        for index, invoice_preparation in enumerate(invoice_preparations):
            _update_travel_invoice_context(
                invoice_preparation,
                primary_invoice=(index == 0),
                raised_rule_codes=raised_travel_rule_codes,
                raised_rule_keys=raised_travel_rule_keys,
                invoice_tax_total=travel_invoice_tax_total,
            )
            if gift_count_context is not None:
                current_prepared_input = _resolve_prepared_input(invoice_preparation)
                cumulative_goods_count += _goods_quantity_total(current_prepared_input)
                _update_gift_count_state(
                    invoice_preparation,
                    cumulative_goods_count=cumulative_goods_count,
                    gift_reception_count=gift_count_context[1],
                    is_last_invoice=(index == len(invoice_preparations) - 1),
                )

            if remaining_apply_amount is not None:
                _update_preparation_apply_amount(invoice_preparation, remaining_apply_amount)

            # 保留旧的前序发票号字段；W34 另注入只包含适用票种的前序号码。
            _inject_previous_invoice_numbers(invoice_preparation, previous_invoice_numbers)
            _inject_previous_w34_invoice_numbers(
                invoice_preparation, previous_w34_invoice_numbers
            )

            invoice_result = self._process_invoice_preparation(
                receipt_code, invoice_preparation, graph_path=effective_graph_path,
            )
            invoice_results.append(invoice_result)

            # 文档级异常在后续发票输入中持续传递，流程图据此去重；逐票
            # 规则（日期、姓名、座位、W37/W39 等）不加入该集合。
            document_rule_codes, document_rule_keys = _extract_document_rule_context(invoice_result)
            for rule_code in document_rule_codes:
                if rule_code not in raised_travel_rule_codes:
                    raised_travel_rule_codes.append(rule_code)
            for rule_key in document_rule_keys:
                if rule_key not in raised_travel_rule_keys:
                    raised_travel_rule_keys.append(rule_key)
            _update_receipt_travel_context(
                prepared_receipt,
                raised_rule_codes=raised_travel_rule_codes,
                raised_rule_keys=raised_travel_rule_keys,
                invoice_tax_total=travel_invoice_tax_total,
            )

            # 收集当前发票号用于后续发票的连号检测。W34 只收集三类适用票种。
            _collect_invoice_number(invoice_preparation, previous_invoice_numbers)
            _collect_w34_invoice_number(invoice_preparation, previous_w34_invoice_numbers)

            used_amount = _extract_invoice_final_amount(
                invoice_result,
                prepared_input=_resolve_prepared_input(invoice_preparation),
            )
            if used_amount is None and _invoice_has_unresolved_e36_amount(invoice_result):
                # E36 的有效金额来自 LLM。缺失/错误/模型服务失败时，不能把
                # OCR 总额当作有效金额，也不能让 E31 误判为“只是金额不足”。
                amount_resolution_unknown = True
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
            "isAmountSufficient": (
                None
                if remaining_apply_amount is None or amount_resolution_unknown
                else remaining_apply_amount <= 0
            ),
            # Receipt-level amount context used by writeback to build the final
            # E31 message with real totals (有效发票合计 / 报销金额 / 缺少金额).
            "applyAmount": _resolve_initial_apply_amount(prepared_receipt),
            "remainingApplyAmount": (
                None if amount_resolution_unknown else remaining_apply_amount
            ),
            "validInvoiceTotal": (
                None
                if amount_resolution_unknown
                else _resolve_valid_invoice_total(
                    invoice_results,
                    _resolve_initial_apply_amount(prepared_receipt),
                    remaining_apply_amount,
                )
            ),
            "resolvedProfile": resolved_profile,
        }
        if gift_count_context is not None:
            has_gift_item, gift_reception_count = gift_count_context
            normalized_goods_count = (
                int(cumulative_goods_count)
                if cumulative_goods_count.is_integer()
                else cumulative_goods_count
            )
            receipt_result.update(
                {
                    "hasGiftItem": has_gift_item,
                    "giftReceptionCount": gift_reception_count,
                    "totalGoodsCount": normalized_goods_count,
                    # 业务费用明细接口异常时，项目类别/接待人数均不可确认。
                    # 不能把 hasGiftItem=False 的降级值当成 W33 PASS。
                    "isGiftCountReasonable": (
                        None
                        if gift_lookup_status == "error"
                        else (
                            not has_gift_item
                            or gift_reception_count <= cumulative_goods_count
                        )
                    ),
                    "giftDetailLookupStatus": gift_lookup_status,
                    "giftDetailLookupError": gift_lookup_error,
                }
            )
        # 核销单级金额汇总：所有发票跑完后生成，避免依赖最后一张发票。
        ai_audit_summary = build_ai_audit_summary(prepared_receipt, receipt_result)
        if ai_audit_summary:
            receipt_result["aiAuditSummary"] = ai_audit_summary

        # 给财务看的稽核点统计与给用户看的整体建议是两个不同字段。
        # 动态路由优先使用本单 resolved profile 的风险配置；静态模式使用
        # service 构造时绑定的 profile 配置。
        effective_profile_name = (
            resolved_profile.name if resolved_profile is not None else self._expense_profile
        )
        effective_risk_catalog = (
            resolved_profile.audit_risk_catalog
            if resolved_profile is not None
            else self._audit_risk_catalog
        )
        ai_audit_summary_finance = build_ai_audit_summary_finance(
            prepared_receipt,
            receipt_result,
            audit_risk_catalog=effective_risk_catalog,
            expense_profile=effective_profile_name,
        )
        if ai_audit_summary_finance:
            receipt_result["aiAuditSummaryFinance"] = ai_audit_summary_finance

        # 核销单级确定性建议：所有发票跑完后、回写 sink 前生成。
        self._augment_with_overall_advice(
            prepared_receipt,
            receipt_result,
            expense_profile=effective_profile_name,
        )
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

    def _augment_with_overall_advice(
        self,
        prepared_receipt: Mapping[str, Any],
        receipt_result: dict[str, Any],
        *,
        expense_profile: str | None = None,
    ) -> None:
        """Attach the deterministic receipt-level ``aiAuditAdvice``.

        ``overall_advice_provider`` remains accepted by the constructor for
        compatibility, but the normal audit path deliberately does not call
        it.  Advice is now calculated from the completed invoice results
        locally, so this step cannot trigger an LLM or another network call.
        """
        advice = build_ai_audit_advice(
            prepared_receipt,
            receipt_result,
            expense_profile=expense_profile,
        )
        if advice:
            receipt_result["aiAuditAdvice"] = advice


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


def _coerce_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _goods_quantity_total(prepared_input: Mapping[str, Any]) -> float:
    items = prepared_input.get("items")
    if not isinstance(items, list):
        return 0.0
    total = 0.0
    for item in items:
        if not isinstance(item, Mapping):
            continue
        # OCR 标准明细的商品数量字段是 num；quantity 作为兼容别名。
        quantity = _coerce_number(item.get("num", item.get("quantity")))
        if quantity is not None:
            total += quantity
    return total


def _resolve_gift_count_lookup_status(
    prepared_receipt: Mapping[str, Any],
) -> tuple[str | None, str]:
    """Return the business-fee-detail lookup status used by W33.

    Older prepared receipts do not carry an explicit status.  If they do carry
    the W33 fields, treat them as a successful lookup for backwards
    compatibility; a newly prepared receipt writes ``error`` explicitly when
    the external service is unavailable.
    """
    service_data = prepared_receipt.get("serviceData")
    if not isinstance(service_data, Mapping):
        return None, ""
    entertainment_data = service_data.get("entertainment_data")
    if not isinstance(entertainment_data, Mapping):
        return None, ""
    if "hasGiftItem" not in entertainment_data and "giftReceptionCount" not in entertainment_data:
        return None, ""

    status = str(entertainment_data.get("giftDetailLookupStatus") or "success").strip().lower()
    if status not in {"success", "error"}:
        status = "error"
    error = str(entertainment_data.get("giftDetailLookupError") or "").strip()
    return status, error


def _resolve_gift_count_context(
    prepared_receipt: Mapping[str, Any],
) -> tuple[bool, float] | None:
    """读取招待费核销单级 W33 上下文。

    W33 的接待人数来自核销单业务费用明细接口，不属于单张发票字段。
    非招待费单据或未注入该 profile 数据时返回 None，避免影响其他费用类型。
    """
    service_data = prepared_receipt.get("serviceData")
    if not isinstance(service_data, Mapping):
        return None
    entertainment_data = service_data.get("entertainment_data")
    if not isinstance(entertainment_data, Mapping):
        return None
    if "hasGiftItem" not in entertainment_data and "giftReceptionCount" not in entertainment_data:
        return None

    has_gift_item = bool(entertainment_data.get("hasGiftItem"))
    gift_reception_count = _coerce_number(entertainment_data.get("giftReceptionCount")) or 0.0
    return has_gift_item, gift_reception_count


def _update_gift_count_state(
    invoice_preparation: Mapping[str, Any],
    *,
    cumulative_goods_count: float,
    gift_reception_count: float,
    is_last_invoice: bool,
) -> None:
    """把 W33 的核销单级累计状态注入当前发票输入。

    图运行粒度仍是一张发票，但 W33 的比较口径是核销单累计值。
    非最后一张发票只推进状态；最后一张发票的累计值代表核销单最终值，
    回写层会进一步只保留最后一张发票的 W33 结果。
    """
    prepared_input = invoice_preparation.get("preparedInput")
    if not isinstance(prepared_input, dict):
        return

    normalized_goods_count: int | float = (
        int(cumulative_goods_count)
        if cumulative_goods_count.is_integer()
        else cumulative_goods_count
    )
    remaining_reception_count = gift_reception_count - cumulative_goods_count
    normalized_remaining: int | float = (
        int(remaining_reception_count)
        if remaining_reception_count.is_integer()
        else remaining_reception_count
    )

    # W33 图表达式读取 totalGoodsCount；这里将其改为当前循环的累计值。
    prepared_input["totalGoodsCount"] = normalized_goods_count
    prepared_input["cumulativeGoodsCount"] = normalized_goods_count
    prepared_input["giftRemainingReceptionCount"] = normalized_remaining
    prepared_input["isLastInvoice"] = is_last_invoice


def _inject_e05_duplicate_context(prepared_receipt: Mapping[str, Any]) -> None:
    """注入 E05 所需的核销单级发票重复上下文。

    流程图运行粒度是一张发票，而“本核销单内重复”必须先看到整单所有发票号
    才能判断。因此这里先用 ``Counter`` 做一次 O(n) 统计，再把统计结果写回
    每张发票的 ``serviceData`` 和 ``context.serviceData``。历史占用信息也是
    在这里整理成可直接展示的核销单号列表和金额，避免在 Zen 表达式中反复
    filter 同一批数据。

    空发票号不参与统计；发票号只做字符串 trim，不转数字，以保留前导零。
    对已经落盘的旧 prepared receipt 该函数也是幂等的，便于队列重放。
    """
    invoice_preparations = prepared_receipt.get("invoicePreparations")
    if not isinstance(invoice_preparations, list):
        return

    invoice_numbers: Counter[str] = Counter()
    candidates: list[tuple[dict[str, Any], str]] = []

    for item in invoice_preparations:
        if not isinstance(item, Mapping):
            continue
        prepared_input = item.get("preparedInput")
        if not isinstance(prepared_input, dict):
            continue

        invoice_no = _normalize_e05_invoice_number(prepared_input)
        candidates.append((prepared_input, invoice_no))
        if invoice_no:
            invoice_numbers[invoice_no] += 1

    for prepared_input, invoice_no in candidates:
        service_data = _ensure_prepared_input_service_data(prepared_input)
        if service_data is None:
            continue

        duplicate_count = invoice_numbers.get(invoice_no, 0) if invoice_no else 0
        service_data["receiptInvoiceDuplicate"] = duplicate_count > 1
        service_data["receiptInvoiceDuplicateCount"] = duplicate_count

        history_records = _resolve_e05_history_records(service_data, invoice_no)
        instance_codes: list[str] = []
        history_amount = 0.0
        for record in history_records:
            instance_code = _resolve_e05_history_instance_code(record)
            if instance_code and instance_code not in instance_codes:
                instance_codes.append(instance_code)

            amount = _coerce_number(record.get("estimatedTotalAmount"))
            if amount is None:
                amount = _coerce_number(record.get("totalAmount"))
            if amount is not None:
                history_amount += amount

        service_data["e05HistoryDuplicateInstanceCodes"] = "、".join(instance_codes)
        service_data["e05HistoryDuplicateAmount"] = _normalize_e05_amount(history_amount)
        prepared_input["serviceData"] = service_data

        context = prepared_input.get("context")
        if not isinstance(context, dict):
            context = {}
            prepared_input["context"] = context
        context["serviceData"] = service_data


def _ensure_prepared_input_service_data(
    prepared_input: dict[str, Any],
) -> dict[str, Any] | None:
    service_data = prepared_input.get("serviceData")
    if isinstance(service_data, dict):
        return service_data

    context = prepared_input.get("context")
    context_service_data = context.get("serviceData") if isinstance(context, dict) else None
    if isinstance(context_service_data, dict):
        prepared_input["serviceData"] = context_service_data
        return context_service_data

    # 没有 serviceData 的旧 prepared receipt 无法参与历史检查，但仍可安全
    # 注入重复字段，后续流程图会按默认值处理历史重复。
    service_data = {}
    prepared_input["serviceData"] = service_data
    return service_data


def _normalize_e05_invoice_number(prepared_input: Mapping[str, Any]) -> str:
    for key in ("invoiceNo", "chequeNo", "serialNo"):
        value = prepared_input.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return ""


def _resolve_e05_history_records(
    service_data: Mapping[str, Any],
    invoice_no: str,
) -> list[Mapping[str, Any]]:
    history = service_data.get("invoiceUsageHistory")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes, bytearray)):
        return []

    records: list[Mapping[str, Any]] = []
    for item in history:
        if not isinstance(item, Mapping):
            continue
        cheque_no = item.get("chequeNo")
        if cheque_no is None:
            cheque_no = item.get("invoiceNo", item.get("serialNo"))
        normalized_cheque_no = "" if cheque_no is None else str(cheque_no).strip()
        if normalized_cheque_no == invoice_no and invoice_no:
            records.append(item)
    return records


def _resolve_e05_history_instance_code(record: Mapping[str, Any]) -> str:
    for key in ("miInstanceCode", "instanceCode", "instance_code"):
        value = record.get(key)
        normalized = "" if value is None else str(value).strip()
        if normalized:
            return normalized
    return "未知"


def _normalize_e05_amount(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _inject_total_goods_count(prepared_receipt: Mapping[str, Any]) -> None:
    invoice_preparations = prepared_receipt.get("invoicePreparations")
    if not isinstance(invoice_preparations, list):
        return
    total = sum(
        _goods_quantity_total(item.get("preparedInput"))
        for item in invoice_preparations
        if isinstance(item, Mapping) and isinstance(item.get("preparedInput"), Mapping)
    )
    normalized_total: int | float = int(total) if total.is_integer() else total
    for item in invoice_preparations:
        if not isinstance(item, Mapping):
            continue
        prepared_input = item.get("preparedInput")
        if isinstance(prepared_input, dict):
            prepared_input["totalGoodsCount"] = normalized_total


def _inject_invoice_serial_batch_context(prepared_receipt: Mapping[str, Any]) -> None:
    """为交通费和业务招待费注入核销单级发票连号关系。

    每个发票级 enricher 负责查询历史库并写入自己的 serial 数据；这里在所有
    发票准备完成后按前六位前缀聚合本单关系。这样同一连号组中的第一张发票
    也会命中，且在线准备、队列/磁盘加载两种执行路径都能幂等重算。
    """
    _inject_invoice_serial_context_for_key(
        prepared_receipt,
        serial_key="taxiInvoiceSerial",
        applicable_key="isTaxiInvoice",
        default_subject="出租车发票",
    )
    _inject_invoice_serial_context_for_key(
        prepared_receipt,
        serial_key="entertainmentInvoiceSerial",
        applicable_key="isTaxiInvoice",
        default_subject="出租车发票",
    )
    _inject_w34_invoice_batch_context(prepared_receipt)


def _inject_w34_invoice_batch_context(prepared_receipt: Mapping[str, Any]) -> None:
    """按发票号码差值为 W34 计算本核销单内的连续/近似关系。

    W34 不使用出租车前缀规则；仅对 W34 enricher 标记的适用票种，
    将任意两张发票号码差值不大于 10 的关系写入 batchHit。
    """
    invoice_preparations = prepared_receipt.get("invoicePreparations")
    if not isinstance(invoice_preparations, list):
        return

    candidates: list[tuple[dict[str, Any], dict[str, Any], str, int | None]] = []
    for item in invoice_preparations:
        if not isinstance(item, dict):
            continue
        prepared_input = item.get("preparedInput")
        if not isinstance(prepared_input, dict):
            continue
        service_data = prepared_input.get("serviceData")
        if not isinstance(service_data, dict):
            context = prepared_input.get("context")
            context_service_data = (
                context.get("serviceData") if isinstance(context, dict) else None
            )
            if not isinstance(context_service_data, dict):
                continue
            service_data = context_service_data
            prepared_input["serviceData"] = service_data
        serial_data = service_data.get("w34InvoiceSerial")
        if not isinstance(serial_data, dict):
            continue

        invoice_no = str(
            serial_data.get("invoiceNo") or prepared_input.get("invoiceNo") or ""
        ).strip()
        serial_data["invoiceNo"] = invoice_no
        candidates.append(
            (
                prepared_input,
                serial_data,
                invoice_no,
                _parse_numeric_invoice_number(invoice_no),
            )
        )

    for prepared_input, serial_data, invoice_no, invoice_number in candidates:
        peer_invoice_numbers: list[str] = []
        if serial_data.get("isApplicable") and invoice_number is not None:
            peer_invoice_numbers = [
                other_invoice_no
                for other_prepared_input, other_serial_data, other_invoice_no, other_number in candidates
                if (
                    other_prepared_input is not prepared_input
                    and other_serial_data.get("isApplicable")
                    and other_invoice_no
                    and other_number is not None
                    and abs(invoice_number - other_number) <= 10
                )
            ]

        history_numbers = _normalize_invoice_number_list(serial_data.get("historyNumbers"))
        # 接口返回当前发票自身时不能把自身作为连号依据。
        history_numbers = [number for number in history_numbers if number != invoice_no]
        serial_data["historyNumbers"] = history_numbers
        serial_data["historyHit"] = bool(history_numbers)
        serial_data["batchPeerInvoiceNumbers"] = peer_invoice_numbers
        serial_data["batchHit"] = bool(peer_invoice_numbers)
        history_peer_numbers = list(history_numbers)
        related_numbers = _merge_invoice_number_lists(
            peer_invoice_numbers, history_peer_numbers
        )
        serial_data["historyPeerInvoiceNumbers"] = history_peer_numbers
        serial_data["relatedInvoiceNumbers"] = related_numbers
        serial_data["relatedInvoiceNumbersText"] = "、".join(related_numbers)
        serial_data["relationDescription"] = _build_invoice_serial_relation_description(
            peer_invoice_numbers,
            history_peer_numbers,
            subject=str(serial_data.get("relationSubject") or "发票"),
        )

        service_data = prepared_input.get("serviceData")
        if not isinstance(service_data, dict):
            continue
        service_data["w34InvoiceSerial"] = serial_data
        prepared_input["serviceData"] = service_data
        context = prepared_input.get("context")
        if not isinstance(context, dict):
            context = {}
            prepared_input["context"] = context
        context["serviceData"] = service_data


def _parse_numeric_invoice_number(value: Any) -> int | None:
    normalized = str(value or "").strip()
    if not normalized or not normalized.isdecimal():
        return None
    try:
        return int(normalized)
    except (TypeError, ValueError):
        return None


def _inject_taxi_invoice_batch_context(prepared_receipt: Mapping[str, Any]) -> None:
    """兼容旧调用方：只重算个人交通费出租车连票关系。"""
    _inject_invoice_serial_context_for_key(
        prepared_receipt,
        serial_key="taxiInvoiceSerial",
        applicable_key="isTaxiInvoice",
        default_subject="出租车发票",
    )


def _inject_entertainment_invoice_batch_context(prepared_receipt: Mapping[str, Any]) -> None:
    """为业务招待费出租车发票重算本核销单内的连号关系。"""
    _inject_invoice_serial_context_for_key(
        prepared_receipt,
        serial_key="entertainmentInvoiceSerial",
        applicable_key="isTaxiInvoice",
        default_subject="出租车发票",
    )


def _inject_invoice_serial_context_for_key(
    prepared_receipt: Mapping[str, Any],
    *,
    serial_key: str,
    applicable_key: str,
    default_subject: str,
) -> None:
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
            # 队列/磁盘中的旧 prepared receipt 可能只保留 context.serviceData；
            # 恢复到 preparedInput.serviceData 后继续做幂等聚合。
            context = prepared_input.get("context")
            context_service_data = context.get("serviceData") if isinstance(context, dict) else None
            if not isinstance(context_service_data, dict):
                continue
            service_data = context_service_data
            prepared_input["serviceData"] = service_data
        serial_data = service_data.get(serial_key)
        if not isinstance(serial_data, dict):
            continue

        invoice_no = str(
            serial_data.get("invoiceNo") or prepared_input.get("invoiceNo") or ""
        ).strip()
        serial_data["invoiceNo"] = invoice_no
        # 每次执行都重新按字符串规则计算，避免旧 prepared receipt 中的过期
        # currentPrefix 让短号码或错误前缀误触发本单连号。
        current_prefix = _normalize_invoice_serial_prefix(invoice_no)
        serial_data["currentPrefix"] = current_prefix
        is_applicable = bool(serial_data.get(applicable_key))
        candidates.append((prepared_input, serial_data, current_prefix, is_applicable))
        if is_applicable and current_prefix and invoice_no:
            grouped[current_prefix].append((prepared_input, invoice_no))

    for prepared_input, serial_data, current_prefix, is_applicable in candidates:
        peer_invoice_numbers: list[str] = []
        if is_applicable and current_prefix:
            peer_invoice_numbers = [
                invoice_no
                for other_prepared_input, invoice_no in grouped[current_prefix]
                if other_prepared_input is not prepared_input and invoice_no
            ]

        serial_data["batchPeerInvoiceNumbers"] = peer_invoice_numbers
        serial_data["batchHit"] = bool(peer_invoice_numbers)

        invoice_no = str(serial_data.get("invoiceNo") or "").strip()
        history_numbers = _normalize_invoice_number_list(serial_data.get("historyNumbers"))
        serial_data["historyNumbers"] = history_numbers
        serial_data["historyHit"] = bool(history_numbers)
        # 历史接口可能返回当前发票本身，也可能只返回其连票集合；优先展示
        # 当前发票之外的号码，若接口只返回当前号码则保留原始结果，避免问题文案丢失依据。
        history_peer_numbers = [number for number in history_numbers if number != invoice_no]
        if history_numbers and not history_peer_numbers:
            history_peer_numbers = history_numbers
        serial_data["historyPeerInvoiceNumbers"] = history_peer_numbers

        related_numbers = _merge_invoice_number_lists(
            peer_invoice_numbers,
            history_peer_numbers,
        )
        serial_data["relatedInvoiceNumbers"] = related_numbers
        serial_data["relatedInvoiceNumbersText"] = "、".join(related_numbers)
        serial_data["relationDescription"] = _build_invoice_serial_relation_description(
            peer_invoice_numbers,
            history_peer_numbers,
            subject=str(serial_data.get("relationSubject") or default_subject),
        )

        service_data = prepared_input.get("serviceData")
        if not isinstance(service_data, dict):
            continue
        service_data[serial_key] = serial_data
        prepared_input["serviceData"] = service_data
        context = prepared_input.get("context")
        if not isinstance(context, dict):
            context = {}
            prepared_input["context"] = context
        context["serviceData"] = service_data


def _normalize_invoice_serial_prefix(invoice_no: Any) -> str | None:
    """按字符串规则计算发票连号前缀，兼容旧 prepared receipt。"""
    value = str(invoice_no or "").strip()
    if len(value) < 8:
        return None
    return value[:-2][:6]


def _normalize_invoice_number_list(value: Any) -> list[str]:
    """把历史/本单连票号码归一化为去重后的字符串列表。"""
    if isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, Mapping):
            continue
        normalized = str(item or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _merge_invoice_number_lists(*lists: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for values in lists:
        for value in values:
            normalized = str(value or "").strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
    return result


def _build_invoice_serial_relation_description(
    batch_peer_numbers: Sequence[str],
    history_peer_numbers: Sequence[str],
    *,
    subject: str,
) -> str:
    """生成 E34 问题文案使用的连号来源和号码说明。"""
    batch_text = "、".join(batch_peer_numbers)
    history_text = "、".join(history_peer_numbers)
    if batch_text and history_text:
        return f"本次核销单其他{subject}号 {batch_text} 及历史发票号 {history_text}"
    if batch_text:
        return f"本次核销单其他{subject}号 {batch_text}"
    if history_text:
        return f"历史发票号 {history_text}"
    return f"历史库或本次核销单中的其他{subject}"


def _build_taxi_invoice_relation_description(
    batch_peer_numbers: Sequence[str],
    history_peer_numbers: Sequence[str],
) -> str:
    """兼容旧调用方，生成交通费出租车连票文案。"""
    return _build_invoice_serial_relation_description(
        batch_peer_numbers,
        history_peer_numbers,
        subject="出租车发票",
    )


def _inject_previous_invoice_numbers(
    invoice_preparation: Mapping[str, Any],
    previous_invoice_numbers: list[str],
) -> None:
    """兼容保留旧的前序发票号上下文。

    E34 已改为读取费用 profile 对应的发票连号上下文，不再依赖该字段；保留注入
    仅用于兼容历史 preparedInput 消费方。
    """
    prepared_input = invoice_preparation.get("preparedInput")
    if not isinstance(prepared_input, dict):
        return
    prepared_input["previousInvoiceNumbers"] = list(previous_invoice_numbers)


def _inject_previous_w34_invoice_numbers(
    invoice_preparation: Mapping[str, Any],
    previous_w34_invoice_numbers: list[str],
) -> None:
    prepared_input = invoice_preparation.get("preparedInput")
    if not isinstance(prepared_input, dict):
        return
    prepared_input["previousW34InvoiceNumbers"] = list(previous_w34_invoice_numbers)


def _collect_w34_invoice_number(
    invoice_preparation: Mapping[str, Any],
    previous_w34_invoice_numbers: list[str],
) -> None:
    prepared_input = invoice_preparation.get("preparedInput")
    if not isinstance(prepared_input, Mapping):
        return
    service_data = prepared_input.get("serviceData")
    if not isinstance(service_data, Mapping):
        context = prepared_input.get("context")
        context_service_data = (
            context.get("serviceData") if isinstance(context, Mapping) else None
        )
        service_data = context_service_data
    serial_data = service_data.get("w34InvoiceSerial") if isinstance(service_data, Mapping) else None
    if not isinstance(serial_data, Mapping) or not serial_data.get("isApplicable"):
        return
    invoice_no = str(serial_data.get("invoiceNo") or prepared_input.get("invoiceNo") or "").strip()
    if invoice_no:
        previous_w34_invoice_numbers.append(invoice_no)


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



def _travel_audit_from_service_data(service_data: Any) -> dict[str, Any] | None:
    if not isinstance(service_data, dict):
        return None
    travel_audit = service_data.get("travelAudit")
    if not isinstance(travel_audit, dict):
        return None
    return travel_audit


def _iter_prepared_travel_audits(
    invoice_preparations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    audits: list[dict[str, Any]] = []
    for preparation in invoice_preparations:
        if not isinstance(preparation, Mapping):
            continue
        prepared_input = preparation.get("preparedInput")
        if not isinstance(prepared_input, Mapping):
            continue
        service_data = prepared_input.get("serviceData")
        travel_audit = _travel_audit_from_service_data(service_data)
        if travel_audit is not None:
            audits.append(travel_audit)
    return audits


def _resolve_travel_invoice_tax_total(
    invoice_preparations: Sequence[Mapping[str, Any]],
) -> float | None:
    total = 0.0
    found = False
    for travel_audit in _iter_prepared_travel_audits(invoice_preparations):
        tax_info = travel_audit.get("taxInfo")
        if not isinstance(tax_info, Mapping):
            continue
        # currentInvoiceDeductibleTax is written by a previous execution pass;
        # invoiceDeductibleTax is the current invoice value from the enricher.
        value = tax_info.get("currentInvoiceDeductibleTax")
        if value is None:
            value = tax_info.get("invoiceDeductibleTax")
        number = _coerce_number(value)
        if number is None:
            continue
        total += number
        found = True
    return total if found else None


def _update_travel_invoice_context(
    invoice_preparation: Mapping[str, Any],
    *,
    primary_invoice: bool,
    raised_rule_codes: Sequence[str],
    raised_rule_keys: Sequence[str],
    invoice_tax_total: float | None,
) -> None:
    prepared_input = invoice_preparation.get("preparedInput")
    if not isinstance(prepared_input, dict):
        return

    # build_rule_input stores serviceData both at the root and under context;
    # update both paths because queue/disk fixtures may have them as distinct
    # dictionaries rather than shared references.
    containers: list[dict[str, Any]] = [prepared_input]
    context = prepared_input.get("context")
    if isinstance(context, dict):
        containers.append(context)
    seen: set[int] = set()
    for container in containers:
        service_data = container.get("serviceData")
        if not isinstance(service_data, dict) or id(service_data) in seen:
            continue
        seen.add(id(service_data))
        travel_audit = service_data.get("travelAudit")
        if not isinstance(travel_audit, dict):
            continue
        travel_audit["primaryInvoice"] = primary_invoice
        travel_audit["raisedRuleCodes"] = list(dict.fromkeys(str(code) for code in raised_rule_codes))
        travel_audit["raisedRuleKeys"] = list(dict.fromkeys(str(key) for key in raised_rule_keys))
        if invoice_tax_total is not None:
            tax_info = travel_audit.get("taxInfo")
            tax_info = dict(tax_info) if isinstance(tax_info, Mapping) else {}
            current_tax = tax_info.get("currentInvoiceDeductibleTax")
            if current_tax is None:
                current_tax = tax_info.get("invoiceDeductibleTax")
            if current_tax is not None:
                tax_info["currentInvoiceDeductibleTax"] = current_tax
            tax_info["invoiceDeductibleTaxTotal"] = invoice_tax_total
            travel_audit["taxInfo"] = tax_info
            form_tax = _coerce_number(tax_info.get("formInputTax"))
            states = dict(travel_audit.get("ruleStates") or {})
            tax_state = (
                "missing"
                if form_tax is None
                else "pass"
                if abs(invoice_tax_total - form_tax) <= 0.01
                else "warning"
            )
            # r37 is the stable Feishu source-row state for E39. Keep the
            # descriptive legacy key for old prepared receipts.
            states["r37"] = tax_state
            states["travel_tax_amount"] = tax_state
            travel_audit["ruleStates"] = states
        service_data["travelAudit"] = travel_audit
        container["serviceData"] = service_data


def _update_receipt_travel_context(
    prepared_receipt: Mapping[str, Any],
    *,
    raised_rule_codes: Sequence[str],
    raised_rule_keys: Sequence[str],
    invoice_tax_total: float | None,
) -> None:
    service_data = prepared_receipt.get("serviceData")
    if not isinstance(service_data, dict):
        return
    travel_audit = service_data.get("travelAudit")
    if not isinstance(travel_audit, dict):
        return
    travel_audit["raisedRuleCodes"] = list(dict.fromkeys(str(code) for code in raised_rule_codes))
    travel_audit["raisedRuleKeys"] = list(dict.fromkeys(str(key) for key in raised_rule_keys))
    if invoice_tax_total is not None:
        tax_info = travel_audit.get("taxInfo")
        tax_info = dict(tax_info) if isinstance(tax_info, Mapping) else {}
        tax_info["invoiceDeductibleTaxTotal"] = invoice_tax_total
        travel_audit["taxInfo"] = tax_info
    service_data["travelAudit"] = travel_audit


_TRAVEL_DOCUMENT_SOURCE_ROWS = frozenset({
    2, 3, 5, 6, 7, 9, 12, 16, 18, 19, 20, 24, 37,
})


def _decision_output_rule_key(output_key: Any) -> str:
    text = str(output_key or "").strip()
    if text.startswith("travel_"):
        text = text[len("travel_"):]
    if text.endswith("_result"):
        text = text[:-len("_result")]
    return text


def _travel_rule_source_row(rule_key: str) -> int | None:
    match = re.match(r"^travel_r(\d{2})(?:_|$)", rule_key)
    return int(match.group(1)) if match else None


def _is_travel_document_rule_key(rule_key: str) -> bool:
    source_row = _travel_rule_source_row(rule_key)
    return source_row in _TRAVEL_DOCUMENT_SOURCE_ROWS


def _extract_document_rule_context(
    invoice_result: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    """Extract document-level exceptions by stable rule key, not code.

    E20/E31/E39 are intentionally repeated in the Feishu sheet. The full
    ``travel_rXX_...`` key is therefore the only safe deduplication identity;
    ``raisedRuleCodes`` remains a compatibility summary only.
    """
    decision_output = invoice_result.get("decisionOutput")
    if not isinstance(decision_output, Mapping):
        return [], []
    codes: list[str] = []
    keys: list[str] = []
    for output_key, value in decision_output.items():
        if not isinstance(value, Mapping):
            continue
        result = str(value.get("distinguish_result") or value.get("distinguishResult") or "").upper()
        if result in {"", "PASS"}:
            continue
        normalized_code = str(value.get("reason_code") or value.get("reasonCode") or "").strip()
        rule_key = _decision_output_rule_key(output_key)
        if not normalized_code or not _is_travel_document_rule_key(rule_key):
            continue
        if rule_key not in keys:
            keys.append(rule_key)
        if normalized_code not in codes:
            codes.append(normalized_code)
    return codes, keys


def _extract_document_rule_codes(invoice_result: Mapping[str, Any]) -> list[str]:
    """Backward-compatible code-only view of document-level exceptions."""
    return _extract_document_rule_context(invoice_result)[0]



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
    """兼容保留的内部入口，实际规则由整单汇总模块统一实现。"""
    return invoice_contributes_valid_amount(invoice_result)


def _extract_invoice_final_amount(
    invoice_result: Mapping[str, Any],
    *,
    prepared_input: Mapping[str, Any] | None = None,
) -> float | None:
    """提取 E31/E34 感知的发票扣减后金额，保持历史 float 返回类型。"""
    final_amount = extract_valid_invoice_final_amount(
        invoice_result,
        prepared_input=prepared_input,
    )
    return float(final_amount) if final_amount is not None else None


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


def _invoice_has_unresolved_e36_amount(invoice_result: Mapping[str, Any]) -> bool:
    """Return whether E36 ran but did not produce an LLM effective amount.

    E36 is the only current invoice rule whose LLM result changes the amount
    counted by E31.  A missing ``invoice_finalAmount`` therefore means that the
    amount is unknown, not that OCR ``totalAmount`` may be used as a fallback.
    """
    decision_output = _resolve_decision_output(invoice_result)
    for value in decision_output.values():
        if not isinstance(value, Mapping):
            continue
        reason_code = str(
            value.get("reason_code") or value.get("reasonCode") or ""
        ).strip().upper()
        if reason_code != "E36":
            continue
        return True
    return False
