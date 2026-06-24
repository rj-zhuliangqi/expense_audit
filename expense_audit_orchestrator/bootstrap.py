import os
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
    fetch_expense_invoice_types,
    fetch_field_mappings,
    fetch_invoice_info,
)
from expense_audit_orchestrator.runtime_client import (
    DEFAULT_GRAPH_PATH,
    GraphRuntimeClient,
    create_graph_runtime_client,
)

from .application import InvoiceResultSink, ReceiptAuditService, ReceiptResultSink
from .core import ReceiptDataPreparer
from .kingdee_ocr import create_kingdee_ocr_provider_from_env
from .writeback_client import AuditInfoWritebackClient, build_receipt_writeback_sink


def _resolve_graph_runtime_url(graph_runtime_url: str | None) -> str | None:
    candidate = graph_runtime_url if graph_runtime_url is not None else os.getenv("GRAPH_RUNTIME_URL")
    if candidate is None:
        return None

    normalized = candidate.strip()
    return normalized or None


def create_receipt_audit_service(
    graph_path: Path | str | None = DEFAULT_GRAPH_PATH,
    *,
    graph_content: dict[str, Any] | str | None = None,
    audit_service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    graph_runtime_url: str | None = None,
    graph_runtime_client: GraphRuntimeClient | None = None,
    invoice_result_sink: InvoiceResultSink | None = None,
    receipt_result_sink: ReceiptResultSink | None = None,
    enable_writeback: bool = False,
    writeback_client: AuditInfoWritebackClient | None = None,
) -> ReceiptAuditService:
    resolved_graph_path = None if graph_content is not None else (graph_path or DEFAULT_GRAPH_PATH)
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
    )
    resolved_receipt_result_sink = _resolve_receipt_result_sink(
        receipt_result_sink=receipt_result_sink,
        enable_writeback=enable_writeback,
        writeback_client=writeback_client,
        audit_service_url=audit_service_url,
    )
    return ReceiptAuditService(
        graph_runtime_client=runtime_client,
        data_preparer=data_preparer,
        graph_path=resolved_graph_path,
        graph_content=graph_content,
        invoice_result_sink=invoice_result_sink,
        receipt_result_sink=resolved_receipt_result_sink,
    )


def _resolve_receipt_result_sink(
    *,
    receipt_result_sink: ReceiptResultSink | None,
    enable_writeback: bool,
    writeback_client: AuditInfoWritebackClient | None,
    audit_service_url: str,
) -> ReceiptResultSink | None:
    if not enable_writeback:
        return receipt_result_sink

    resolved_writeback_client = writeback_client or AuditInfoWritebackClient(service_url=audit_service_url)
    writeback_sink = build_receipt_writeback_sink(resolved_writeback_client)
    if receipt_result_sink is None:
        return writeback_sink

    def composed_sink(receipt_result: dict[str, Any]) -> None:
        receipt_result_sink(receipt_result)
        writeback_sink(receipt_result)

    return composed_sink