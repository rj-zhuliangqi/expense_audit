from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from expense_audit_orchestrator.runtime_client import DEFAULT_GRAPH_PATH, GraphRuntimeClient

from .core import DEFAULT_OCR_PATH, ReceiptDataPreparer
from .observability import get_logger, new_run_id, run_context
from .overall_advice import OverallAdviceProvider, resolve_llm_evaluate_endpoint
from .receipt_summary import (
    build_ai_audit_advice,
    build_ai_audit_summary,
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
        # 所有发票完成数据准备后一次性计算本单出租车连票关系，供需要在执行前
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
        # 所有发票已完成数据准备后一次性计算本单出租车连票关系，确保连票组中的
        # 第一张发票也能命中 E34，不受发票执行顺序影响。
        _inject_invoice_serial_batch_context(prepared_receipt)
        remaining_apply_amount = _resolve_initial_apply_amount(prepared_receipt)

        # 动态路由模式：从 prepared_receipt 取出选中的 profile，用其 graph_path 执行
        resolved_profile: ExpenseProfile | None = prepared_receipt.get("resolvedProfile")
        effective_graph_path = self._graph_path
        if resolved_profile is not None and resolved_profile.default_graph_path is not None:
            effective_graph_path = resolved_profile.default_graph_path

        previous_invoice_numbers: list[str] = []
        gift_count_context = _resolve_gift_count_context(prepared_receipt)
        cumulative_goods_count = 0.0

        invoice_preparations = prepared_receipt["invoicePreparations"]
        for index, invoice_preparation in enumerate(invoice_preparations):
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

            # 出租车连票检测：保留旧的前序发票号字段，E34 实际读取整单关系上下文。
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
                    "isGiftCountReasonable": (
                        not has_gift_item
                        or gift_reception_count <= cumulative_goods_count
                    ),
                }
            )
        # 核销单级金额汇总：所有发票跑完后生成，避免依赖最后一张发票。
        ai_audit_summary = build_ai_audit_summary(prepared_receipt, receipt_result)
        if ai_audit_summary:
            receipt_result["aiAuditSummary"] = ai_audit_summary

        # 核销单级确定性建议：所有发票跑完后、回写 sink 前生成。
        self._augment_with_overall_advice(prepared_receipt, receipt_result)
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
    ) -> None:
        """Attach the deterministic receipt-level ``aiAuditAdvice``.

        ``overall_advice_provider`` remains accepted by the constructor for
        compatibility, but the normal audit path deliberately does not call
        it.  Advice is now calculated from the completed invoice results
        locally, so this step cannot trigger an LLM or another network call.
        """
        advice = build_ai_audit_advice(prepared_receipt, receipt_result)
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
    """兼容保留的内部入口，实际规则由整单汇总模块统一实现。"""
    return invoice_contributes_valid_amount(invoice_result)


def _extract_invoice_final_amount(invoice_result: Mapping[str, Any]) -> float | None:
    """提取 E31/E34 感知的发票扣减后金额，保持历史 float 返回类型。"""
    final_amount = extract_valid_invoice_final_amount(invoice_result)
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
