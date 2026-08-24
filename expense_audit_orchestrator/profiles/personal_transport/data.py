"""个人交通费 profile 的数据准备和票种编码标准化。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from ... import audit_client
from ...observability import get_logger

DEFAULT_INVOICE_TYPE_MAP_PATH = Path(__file__).with_name("invoice_type_map.json")
_logger = get_logger("personal_transport_data")

InvoiceSerialNumberProvider = Callable[[str, str, str | None], list[str]]


def load_personal_transport_invoice_type_map(
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """加载个人交通费票种映射表。

    ``code`` 是 OCR/业务侧统一使用的票种编码；``name`` 和 ``aliases``
    用于兼容费用项接口返回的票种名称、老票种编码等字段。
    """
    resolved_path = Path(path) if path is not None else DEFAULT_INVOICE_TYPE_MAP_PATH
    raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"personal transport invoice type map must be a list: {resolved_path}")

    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if not code or not name:
            continue
        aliases = [str(value).strip() for value in item.get("aliases", []) if str(value).strip()]
        result.append({"code": code, "name": name, "aliases": aliases})
    return result


def _normalized_values(value: Any) -> set[str]:
    if value is None:
        return set()
    text = str(value).strip()
    return {text} if text else set()


def _entry_values(entry: Mapping[str, Any]) -> set[str]:
    values = _normalized_values(entry.get("code")) | _normalized_values(entry.get("name"))
    aliases = entry.get("aliases")
    if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)):
        for alias in aliases:
            values |= _normalized_values(alias)
    return values


def _resolve_invoice_type_code(value: Any, mapping: Sequence[Mapping[str, Any]]) -> str | None:
    values = _normalized_values(value)
    if not values:
        return None
    for entry in mapping:
        if values & _entry_values(entry):
            return str(entry["code"])
    return None


def normalize_invoice_serial_prefix(invoice_no: Any) -> str | None:
    """按出租车连票规则生成发票号码比较前缀。

    规则是去掉发票号最后两位后，比较剩余号码的前六位。全程保留字符串，
    避免前导零和超长发票号在数值转换时丢失信息。
    """
    value = str(invoice_no or "").strip()
    if len(value) < 8:
        return None
    return value[:-2][:6]


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def is_taxi_invoice(
    ocr_data: Mapping[str, Any],
    mapping: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """判断当前 OCR 发票是否属于出租车票。"""
    resolved_mapping = mapping if mapping is not None else load_personal_transport_invoice_type_map()
    invoice_type_code = _resolve_invoice_type_code(ocr_data.get("invoiceType"), resolved_mapping)
    if invoice_type_code == "8":
        return True

    text_values: list[str] = []
    for key in (
        "invoiceType",
        "invoiceTypeName",
        "invoiceTypeDesc",
        "remark",
        "remarks",
        "note",
        "goodsName",
        "itemName",
        "invoiceContent",
    ):
        value = _string_value(ocr_data.get(key))
        if value:
            text_values.append(value)

    items = ocr_data.get("items")
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
        for item in items:
            if isinstance(item, Mapping):
                for key in ("goodsName", "itemName", "name", "remark"):
                    value = _string_value(item.get(key))
                    if value:
                        text_values.append(value)

    return any("出租车" in value or "的士" in value for value in text_values)


def build_taxi_invoice_serial_enricher(
    *,
    service_url: str | None = None,
    provider: InvoiceSerialNumberProvider | None = None,
) -> Callable[[str, str, dict[str, Any], dict[str, Any]], dict[str, Any]]:
    """构造个人交通费出租车历史连票查询 enricher。"""
    resolved_service_url = service_url or audit_client.DEFAULT_AUDIT_SERVICE_URL
    invoice_serial_provider = provider or partial(
        audit_client.fetch_taxi_invoice_serial_numbers,
        service_url=resolved_service_url,
    )

    def enricher(
        receipt_code: str,
        file_path: str,
        ocr_data: dict[str, Any],
        service_data: dict[str, Any],
    ) -> dict[str, Any]:
        del file_path
        mapping = load_personal_transport_invoice_type_map()
        invoice_no = (
            _string_value(ocr_data.get("chequeNo"))
            or _string_value(ocr_data.get("invoiceNo"))
            or _string_value(ocr_data.get("serialNo"))
        )
        current_prefix = normalize_invoice_serial_prefix(invoice_no)
        taxi_invoice = is_taxi_invoice(ocr_data, mapping)
        history_numbers: list[str] = []
        lookup_failed = False

        audit_info = service_data.get("auditInfo") if isinstance(service_data, Mapping) else {}
        if not isinstance(audit_info, Mapping):
            audit_info = {}
        instance_code = _string_value(audit_info.get("instanceCode")) or receipt_code
        accounting_code = (
            _string_value(ocr_data.get("accountingCode"))
            or _string_value(audit_info.get("accountingCode"))
        )

        if taxi_invoice and invoice_no and current_prefix:
            try:
                history_numbers = list(
                    invoice_serial_provider(invoice_no, instance_code, accounting_code) or []
                )
            except Exception as exc:
                lookup_failed = True
                _logger.warning(
                    "出租车发票历史连票查询失败，降级为空列表",
                    extra={
                        "event": "data_prep.taxi_invoice_serial.fallback",
                        "receipt_code": receipt_code,
                        "instance_code": instance_code,
                        "cheque_no": invoice_no,
                        "accounting_code": accounting_code,
                        "error": str(exc),
                    },
                )

        return {
            "invoiceNo": invoice_no or "",
            "currentPrefix": current_prefix,
            "historyNumbers": history_numbers,
            "historyHit": bool(history_numbers),
            "batchHit": False,
            "isTaxiInvoice": taxi_invoice,
            "lookupFailed": lookup_failed,
        }

    return enricher


def _allowed_invoice_type_codes(
    expense_invoice_types: Any,
    mapping: Sequence[Mapping[str, Any]],
) -> list[str]:
    if not isinstance(expense_invoice_types, Sequence) or isinstance(expense_invoice_types, (str, bytes)):
        return []

    allowed: list[str] = []
    for item in expense_invoice_types:
        if not isinstance(item, Mapping):
            continue
        for key in ("manufacturerBillCode", "invoiceType", "manufacturerBillName"):
            code = _resolve_invoice_type_code(item.get(key), mapping)
            if code is not None:
                if code not in allowed:
                    allowed.append(code)
                break
    return allowed


def personal_transport_invoice_type_enricher(
    receipt_code: str,
    file_path: str,
    ocr_data: dict[str, Any],
    service_data: dict[str, Any],
) -> dict[str, Any]:
    """把 OCR 票种和费用项允许票种统一为 profile 维护的标准编码。"""
    del receipt_code, file_path
    mapping = load_personal_transport_invoice_type_map()
    invoice_type = (ocr_data or {}).get("invoiceType")
    allowed_types = _allowed_invoice_type_codes(
        (service_data or {}).get("expenseInvoiceTypes"),
        mapping,
    )
    return {
        "personalTransportInvoiceTypeMap": mapping,
        "personalTransportInvoiceTypeCode": _resolve_invoice_type_code(invoice_type, mapping),
        "personalTransportAllowedInvoiceTypeCodes": allowed_types,
    }


def personal_transport_receipt_enricher(
    receipt_code: str, service_data: Mapping[str, Any]
) -> dict[str, Any]:
    """个人交通费收据级 enricher。"""
    del receipt_code, service_data
    return {}


__all__ = [
    "DEFAULT_INVOICE_TYPE_MAP_PATH",
    "build_taxi_invoice_serial_enricher",
    "is_taxi_invoice",
    "load_personal_transport_invoice_type_map",
    "normalize_invoice_serial_prefix",
    "personal_transport_invoice_type_enricher",
    "personal_transport_receipt_enricher",
]
