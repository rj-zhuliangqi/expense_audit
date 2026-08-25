from __future__ import annotations

import os
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any

from expense_audit_orchestrator.audit_client import (
    DEFAULT_AUDIT_SERVICE_URL,
    fetch_audit_info,
    fetch_audit_invoice_file_info,
    fetch_audit_invoice_files,
    fetch_company_blacklist,
    fetch_company_list,
    fetch_enterprise_base_info,
    fetch_expense_invoice_types,
    fetch_field_mappings,
    fetch_invoice_info,
)
from expense_audit_orchestrator.profiles import ExpenseProfile, ProfileResolver, get_profile
from expense_audit_orchestrator.runtime_client import (
    DEFAULT_GRAPH_PATH,
    GraphRuntimeClient,
    create_graph_runtime_client,
)

from .application import InvoiceResultSink, ReceiptAuditService, ReceiptResultSink
from .core import ReceiptDataPreparer
from .kingdee_ocr import create_kingdee_ocr_provider_from_env
from .observability import build_invoice_result_log_sink, get_logger, resolve_log_dir
from .overall_advice import OverallAdviceProvider
from .writeback_client import (
    AUDIT_INFO_SAVE_PATH,
    AuditInfoWritebackClient,
    build_receipt_writeback_file_sink,
    build_receipt_writeback_sink,
)


def _resolve_graph_runtime_url(graph_runtime_url: str | None) -> str | None:
    candidate = graph_runtime_url if graph_runtime_url is not None else os.getenv("GRAPH_RUNTIME_URL")
    if candidate is None:
        return None

    normalized = candidate.strip()
    return normalized or None


def _resolve_profile(
    profile: ExpenseProfile | str,
    *,
    telecom_asset_dir: Path | str | None = None,
    audit_service_url: str | None = None,
) -> ExpenseProfile:
    if isinstance(profile, ExpenseProfile):
        return profile
    return get_profile(
        profile,
        telecom_asset_dir=telecom_asset_dir,
        service_url=audit_service_url,
    )


def create_receipt_audit_service(
    profile: ExpenseProfile | str = "telecom",
    graph_path: Path | str | None = None,
    *,
    graph_content: dict[str, Any] | str | None = None,
    profile_resolver: "ProfileResolver | None" = None,
    audit_service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    graph_runtime_url: str | None = None,
    graph_runtime_client: GraphRuntimeClient | None = None,
    invoice_result_sink: InvoiceResultSink | None = None,
    receipt_result_sink: ReceiptResultSink | None = None,
    enable_writeback: bool = False,
    writeback_client: AuditInfoWritebackClient | None = None,
    writeback_output_dir: Path | str | None = None,
    telecom_asset_dir: Path | str | None = None,
    enable_invoice_logging: bool | None = None,
    invoice_log_dir: Path | str | None = None,
    overall_advice_provider: OverallAdviceProvider | None = None,
) -> ReceiptAuditService:
    # 动态路由模式（profile_resolver）与静态模式（profile/graph_path）互斥
    if profile_resolver is not None:
        if graph_path is not None or graph_content is not None or profile != "telecom":
            raise ValueError(
                "profile_resolver cannot be used together with profile, graph_path or graph_content; "
                "use either dynamic routing (profile_resolver) or static mode (profile + graph_path)"
            )
        # 动态模式：data_preparer 不绑定固定 enricher（每单据由 resolver 选中的 profile 决定）
        resolved_profile = None
        resolved_graph_path = None
    else:
        resolved_profile = _resolve_profile(
            profile,
            telecom_asset_dir=telecom_asset_dir,
            audit_service_url=audit_service_url,
        )
        resolved_graph_path = None if graph_content is not None else (
            graph_path or resolved_profile.default_graph_path or DEFAULT_GRAPH_PATH
        )
    runtime_client = graph_runtime_client or create_graph_runtime_client(
        _resolve_graph_runtime_url(graph_runtime_url)
    )
    resolved_ocr_provider = create_kingdee_ocr_provider_from_env()
    if resolved_ocr_provider is None:
        raise ValueError("kingdee ocr provider is required")
    data_preparer = ReceiptDataPreparer(
        ocr_provider=resolved_ocr_provider,
        audit_info_provider=partial(fetch_audit_info, service_url=audit_service_url),
        company_blacklist_provider=partial(fetch_company_blacklist, service_url=audit_service_url),
        company_list_provider=partial(fetch_company_list, service_url=audit_service_url),
        expense_invoice_types_provider=partial(fetch_expense_invoice_types, service_url=audit_service_url),
        invoice_info_provider=partial(fetch_invoice_info, service_url=audit_service_url),
        audit_invoice_files_provider=partial(fetch_audit_invoice_files, service_url=audit_service_url),
        audit_invoice_file_info_provider=partial(fetch_audit_invoice_file_info, service_url=audit_service_url),
        field_mappings_provider=partial(fetch_field_mappings, service_url=audit_service_url),
        receipt_enrichers=resolved_profile.receipt_enrichers if resolved_profile else {},
        extra_enrichers=resolved_profile.invoice_enrichers if resolved_profile else {},
        qichacha_provider=fetch_enterprise_base_info,
    )
    resolved_receipt_result_sink = _resolve_receipt_result_sink(
        receipt_result_sink=receipt_result_sink,
        enable_writeback=enable_writeback,
        writeback_client=writeback_client,
        writeback_output_dir=writeback_output_dir,
        audit_service_url=audit_service_url,
        profile=resolved_profile,
        profile_resolver=profile_resolver,
    )
    resolved_invoice_result_sink = _resolve_invoice_result_sink(
        invoice_result_sink=invoice_result_sink,
        enable_invoice_logging=enable_invoice_logging,
        invoice_log_dir=invoice_log_dir,
    )
    return ReceiptAuditService(
        graph_runtime_client=runtime_client,
        data_preparer=data_preparer,
        graph_path=resolved_graph_path,
        graph_content=graph_content,
        profile_resolver=profile_resolver,
        invoice_result_sink=resolved_invoice_result_sink,
        receipt_result_sink=resolved_receipt_result_sink,
        # Kept as an injection point for backwards compatibility.  The
        # application now generates aiAuditAdvice deterministically and does
        # not invoke this provider.
        overall_advice_provider=overall_advice_provider,
        audit_service_url=audit_service_url,
        expense_profile=resolved_profile.name if resolved_profile is not None else None,
        audit_risk_catalog=(
            resolved_profile.audit_risk_catalog if resolved_profile is not None else None
        ),
    )


def _resolve_invoice_result_sink(
    *,
    invoice_result_sink: InvoiceResultSink | None,
    enable_invoice_logging: bool | None,
    invoice_log_dir: Path | str | None,
) -> InvoiceResultSink | None:
    enabled = _resolve_bool_env("LOG_ENABLED", "LOG_ENABLED", enable_invoice_logging, default=True)
    if not enabled:
        return invoice_result_sink

    log_dir = invoice_log_dir if invoice_log_dir is not None else resolve_log_dir()
    log_sink = _wrap_safe_log_sink(build_invoice_result_log_sink(log_dir))
    if invoice_result_sink is None:
        return log_sink

    def composed_sink(receipt_code: str, invoice_result: dict[str, Any]) -> None:
        log_sink(receipt_code, invoice_result)
        invoice_result_sink(receipt_code, invoice_result)

    return composed_sink


def _wrap_safe_log_sink(log_sink: InvoiceResultSink) -> InvoiceResultSink:
    """日志 sink 异常绝不能打断审计主链路。"""
    logger = get_logger("invoice_result_sink")

    def safe_sink(receipt_code: str, invoice_result: dict[str, Any]) -> None:
        try:
            log_sink(receipt_code, invoice_result)
        except Exception:
            logger.exception(
                "invoice result log sink failed",
                extra={
                    "receipt_code": receipt_code,
                    "run_id": invoice_result.get("runId"),
                    "invoice_key": invoice_result.get("invoiceKey"),
                    "event": "invoice.log_sink.failed",
                },
            )

    return safe_sink


def _resolve_bool_env(
    py_name: str,
    env_name: str,
    explicit: bool | None,
    *,
    default: bool,
) -> bool:
    if explicit is not None:
        return explicit
    raw = os.getenv(env_name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_receipt_result_sink(
    *,
    receipt_result_sink: ReceiptResultSink | None,
    enable_writeback: bool,
    writeback_client: AuditInfoWritebackClient | None,
    writeback_output_dir: Path | str | None,
    audit_service_url: str,
    profile: ExpenseProfile | None,
    profile_resolver: "ProfileResolver | None" = None,
) -> ReceiptResultSink | None:
    if not enable_writeback:
        return receipt_result_sink

    if profile_resolver is not None:
        # 动态路由模式：回写策略按单据的 resolvedProfile 动态选择
        return _build_dynamic_writeback_sink(
            receipt_result_sink=receipt_result_sink,
            writeback_client=writeback_client,
            writeback_output_dir=writeback_output_dir,
            audit_service_url=audit_service_url,
        )

    # 静态模式：回写策略在构造时绑定固定 profile
    strategy_kwargs: dict[str, Any] = {
        "compliance_rule": profile.compliance_rule,
        "audit_travels_builder": profile.audit_travels_builder,
        "form_invoice_tax_views_builder": profile.form_invoice_tax_views_builder,
        "audit_rule_catalog": profile.audit_rule_catalog,
        "audit_risk_catalog": profile.audit_risk_catalog,
        "expense_profile": profile.name,
    }
    sinks: list[ReceiptResultSink] = []
    if writeback_output_dir is not None:
        sinks.append(
            build_receipt_writeback_file_sink(writeback_output_dir, **strategy_kwargs)
        )
    resolved_client = writeback_client or AuditInfoWritebackClient(
        service_url=audit_service_url, save_path=profile.writeback_save_path
    )
    sinks.append(build_receipt_writeback_sink(resolved_client, **strategy_kwargs))

    writeback_sink = _compose_sinks(sinks)
    if receipt_result_sink is None:
        return writeback_sink

    def composed_sink(receipt_result: dict[str, Any]) -> None:
        receipt_result_sink(receipt_result)
        writeback_sink(receipt_result)

    return composed_sink


def _build_dynamic_writeback_sink(
    *,
    receipt_result_sink: ReceiptResultSink | None,
    writeback_client: AuditInfoWritebackClient | None,
    writeback_output_dir: Path | str | None,
    audit_service_url: str,
) -> ReceiptResultSink | None:
    """动态路由模式下的回写 sink：从 receipt_result.resolvedProfile 取策略。

    prepare_receipt 已将 resolvedProfile 存入 prepared_receipt，process_prepared_receipt
    透传到 receipt_result。回写时按该 profile 的 compliance_rule/audit_travels_builder
    组装 payload，实现回写策略的按单据动态路由。
    """
    from .writeback_client import build_writeback_payload  # 局部导入避免循环依赖

    resolved_client = writeback_client or AuditInfoWritebackClient(service_url=audit_service_url)

    def dynamic_sink(receipt_result: dict[str, Any]) -> None:
        profile = _extract_resolved_profile(receipt_result)
        strategy_kwargs: dict[str, Any] = {
            "compliance_rule": profile.compliance_rule if profile else None,
            "audit_travels_builder": profile.audit_travels_builder if profile else None,
            "form_invoice_tax_views_builder": profile.form_invoice_tax_views_builder if profile else None,
            "audit_rule_catalog": profile.audit_rule_catalog if profile else None,
            "audit_risk_catalog": profile.audit_risk_catalog if profile else None,
            "expense_profile": profile.name if profile else None,
        }
        payload = build_writeback_payload(receipt_result, **strategy_kwargs)
        # 先导出 payload 再回写：回写失败时也能拿到实际发送的 payload 供排查
        if writeback_output_dir is not None:
            _export_dynamic_payload(payload, writeback_output_dir, receipt_result)
        # 动态模式下 save_path 也按 profile 决定（默认 AUDIT_INFO_SAVE_PATH）
        save_path = profile.writeback_save_path if profile else AUDIT_INFO_SAVE_PATH
        resolved_client.save_result_audit_info(payload, save_path=save_path)

    writeback_sink = dynamic_sink
    if receipt_result_sink is not None:
        def composed_sink(receipt_result: dict[str, Any]) -> None:
            receipt_result_sink(receipt_result)
            writeback_sink(receipt_result)
        return composed_sink
    return writeback_sink


def _extract_resolved_profile(receipt_result: Mapping[str, Any]) -> ExpenseProfile | None:
    """从 receipt_result 中提取 prepare_receipt 阶段存入的 resolvedProfile。"""
    profile = receipt_result.get("resolvedProfile")
    if isinstance(profile, ExpenseProfile):
        return profile
    return None


def _export_dynamic_payload(payload: dict[str, Any], output_dir: Path | str, receipt_result: Mapping[str, Any]) -> None:
    """动态模式下导出 writeback payload 到文件（调试用）。"""
    from .writeback_client import _export_json_payload  # 局部导入

    receipt_code = str(receipt_result.get("receiptCode") or "unknown")
    output_file = Path(output_dir) / f"{receipt_code}.writeback-payload.json"
    _export_json_payload(payload, output_file, "writeback payload")


def _compose_sinks(sinks: list[ReceiptResultSink]) -> ReceiptResultSink | None:
    if not sinks:
        return None
    if len(sinks) == 1:
        return sinks[0]

    def composed(receipt_result: dict[str, Any]) -> None:
        for sink in sinks:
            sink(receipt_result)

    return composed


__all__ = [
    "AUDIT_INFO_SAVE_PATH",
    "create_receipt_audit_service",
]
