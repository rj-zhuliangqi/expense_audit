import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .application import ReceiptResultSink
from .audit_client import (
    DEFAULT_AUDIT_SERVICE_URL,
    _build_auth_headers,
    _get_service_error_message,
    _is_success_payload,
)
from .writeback import assemble_result_audit_info


AUDIT_INFO_SAVE_PATH = "/api/audit-service/audit/audit-info-save"
DEFAULT_WRITEBACK_TIMEOUT = 30.0


class AuditInfoWritebackClient:
    def __init__(
        self,
        *,
        service_url: str = DEFAULT_AUDIT_SERVICE_URL,
        timeout: float = DEFAULT_WRITEBACK_TIMEOUT,
    ) -> None:
        self._service_url = service_url.rstrip("/")
        self._timeout = timeout

    def save_result_audit_info(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        endpoint = f"{self._service_url}{AUDIT_INFO_SAVE_PATH}"
        print(f"[核销单服务] 回写稽核结果: {endpoint}")

        request = Request(
            endpoint,
            data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            headers=_build_auth_headers({"Content-Type": "application/json"}),
            method="POST",
        )

        with urlopen(request, timeout=self._timeout) as response:
            response_payload = json.load(response)

        if not _is_success_payload(response_payload):
            raise ValueError(_get_service_error_message(response_payload, "回写稽核结果"))

        if not isinstance(response_payload, dict):
            raise ValueError("回写稽核结果 service returned invalid payload")

        return response_payload


def build_receipt_writeback_sink(client: AuditInfoWritebackClient) -> ReceiptResultSink:
    def sink(receipt_result: dict[str, Any]) -> None:
        payload = _build_writeback_payload_from_result(receipt_result)
        client.save_result_audit_info(payload)

    return sink


def build_receipt_writeback_file_sink(output_dir: Path | str) -> ReceiptResultSink:
    resolved_output_dir = Path(output_dir)

    def sink(receipt_result: dict[str, Any]) -> None:
        receipt_code = str(receipt_result.get("receiptCode") or "unknown")
        payload = _build_writeback_payload_from_result(receipt_result)
        output_file = resolved_output_dir / f"{receipt_code}.writeback-payload.json"
        _export_json_payload(payload, output_file, "writeback payload")

    return sink


def _build_writeback_payload_from_result(receipt_result: Mapping[str, Any]) -> dict[str, Any]:
    prepared_receipt = _build_prepared_receipt_from_result(receipt_result)
    return assemble_result_audit_info(prepared_receipt, receipt_result)


def _build_prepared_receipt_from_result(receipt_result: Mapping[str, Any]) -> dict[str, Any]:
    invoice_preparations: list[dict[str, Any]] = []
    for invoice_result in receipt_result.get("invoiceResults") or []:
        if not isinstance(invoice_result, Mapping):
            continue
        invoice_preparations.append(
            {
                "invoiceKey": str(invoice_result.get("invoiceKey") or ""),
                "invoiceFile": _resolve_mapping(invoice_result.get("invoiceFile")),
                "preparedInput": _resolve_mapping(invoice_result.get("preparedInput")),
            }
        )

    return {
        "receiptCode": str(receipt_result.get("receiptCode") or ""),
        "serviceData": _resolve_mapping(receipt_result.get("serviceData")),
        "invoicePreparations": invoice_preparations,
    }


def _resolve_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _export_json_payload(payload: dict[str, Any], output_path: Path, label: str) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    print(f"[receipt-sink] exported {label} to: {output_path}")
    return output_path


__all__ = [
    "AUDIT_INFO_SAVE_PATH",
    "AuditInfoWritebackClient",
    "build_receipt_writeback_file_sink",
    "build_receipt_writeback_sink",
]