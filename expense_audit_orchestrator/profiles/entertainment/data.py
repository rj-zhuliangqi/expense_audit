"""业务招待费单据级数据准备和标准化。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
from typing import Any

from expense_audit_orchestrator.observability import get_logger

from .client import EntertainmentApiClient


_logger = get_logger("entertainment_data")

DEFAULT_E15_INVOICE_TYPE_MAP_PATH = Path(__file__).with_name("e15_invoice_type_map.json")
E15_INVOICE_TYPE_MAP_PATH_ENV = "E15_INVOICE_TYPE_MAP_PATH"


def _resolve_e15_map_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.getenv(E15_INVOICE_TYPE_MAP_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_E15_INVOICE_TYPE_MAP_PATH


def load_e15_invoice_type_map(path: str | Path | None = None) -> list[dict[str, Any]]:
    """加载 E15 适用发票类型配置；配置只在 enricher 构造时读取一次。"""
    map_path = _resolve_e15_map_path(path)
    with map_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    invoice_types = payload.get("invoiceTypes") if isinstance(payload, Mapping) else None
    if not isinstance(invoice_types, list):
        raise ValueError(f"E15 invoice type map must contain an invoiceTypes list: {map_path}")

    normalized: list[dict[str, Any]] = []
    for item in invoice_types:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if not code or not name:
            raise ValueError(f"E15 invoice type entry requires code and name: {item!r}")
        match_values = item.get("matchValues")
        if not isinstance(match_values, list):
            match_values = []
        values = {code, name, *(str(value).strip() for value in match_values if str(value).strip())}
        normalized.append({"code": code, "name": name, "matchValues": sorted(values)})
    return normalized


def _normalized_text_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    text = str(value).strip()
    return {text} if text else set()


def _invoice_type_values(
    ocr_data: Mapping[str, Any],
    service_data: Mapping[str, Any],
) -> set[str]:
    """收集 OCR 票种及票种服务配置中的编码/名称，兼容不同来源字段。"""
    values: set[str] = set()
    for key in ("invoiceType", "invoiceTypeCode", "invoiceTypeName", "invoiceKind", "manufacturerBillCode"):
        values.update(_normalized_text_values(ocr_data.get(key)))

    # OCR 可能返回 RJ-010，而费用票种接口给出 manufacturerBillCode=29；
    # 将当前费用项票种配置一并纳入匹配，避免把两套编码误认为不同票种。
    expense_invoice_types = service_data.get("expenseInvoiceTypes")
    if isinstance(expense_invoice_types, Sequence) and not isinstance(expense_invoice_types, (str, bytes)):
        ocr_values = set(values)
        for item in expense_invoice_types:
            if not isinstance(item, Mapping):
                continue
            item_values: set[str] = set()
            for key in ("invoiceType", "manufacturerBillCode", "manufacturerBillName", "invoiceTypeName"):
                item_values.update(_normalized_text_values(item.get(key)))
            if ocr_values.intersection(item_values):
                values.update(item_values)
    return values


def build_e15_invoice_type_enricher(
    path: str | Path | None = None,
):
    """构造 E15 发票类型 enricher，配置文件只读取一次并在内存中匹配。"""
    invoice_types = load_e15_invoice_type_map(path)

    def enrich(
        receipt_code: str,
        file_path: str,
        ocr_data: dict[str, Any],
        service_data: dict[str, Any],
    ) -> dict[str, Any]:
        del receipt_code, file_path
        ocr = ocr_data if isinstance(ocr_data, Mapping) else {}
        services = service_data if isinstance(service_data, Mapping) else {}
        actual_values = _invoice_type_values(ocr, services)
        for invoice_type in invoice_types:
            if actual_values.intersection(invoice_type["matchValues"]):
                return {
                    "isApplicable": True,
                    "invoiceTypeCode": invoice_type["code"],
                    "invoiceTypeName": invoice_type["name"],
                }
        return {
            "isApplicable": False,
            "invoiceTypeCode": "",
            "invoiceTypeName": "",
        }

    return enrich


def _number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _is_gift_detail(row: Mapping[str, Any]) -> bool:
    # 接口文档规定 bfdItemcode=3 为“赠送纪念品”；名称判断兼容历史数据。
    return _text(row, "bfdItemcode", "itemCode") == "3" or _text(
        row, "bfdItemname", "itemName"
    ) == "赠送纪念品"


def _normalize_business_fee_details(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        return [dict(raw)]
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def build_entertainment_receipt_enricher(
    *,
    service_url: str | None = None,
    client: EntertainmentApiClient | None = None,
):
    """构造招待费单据级 enricher，注入业务费用明细和赠送纪念品接待人数。"""

    api_client = client or EntertainmentApiClient(service_url=service_url)

    def enrich(receipt_code: str, service_data: Mapping[str, Any]) -> dict[str, Any]:
        audit_info = service_data.get("auditInfo") if isinstance(service_data, Mapping) else None
        audit_info = audit_info if isinstance(audit_info, Mapping) else {}
        instance_code = str(audit_info.get("instanceCode") or "").strip()
        if not instance_code:
            return {}

        try:
            details = _normalize_business_fee_details(
                api_client.fetch_business_fee_details(instance_code)
            )
        except Exception as exc:  # 外部数据缺失时保持非阻断，避免误报整单失败
            _logger.warning(
                "获取业务招待费业务费用明细失败，W33 按无适用赠送纪念品明细处理",
                extra={
                    "event": "entertainment.business_fee_details.fallback",
                    "receipt_code": receipt_code,
                    "instance_code": instance_code,
                    "error": str(exc),
                },
            )
            details = []

        gift_details = [item for item in details if _is_gift_detail(item)]
        reception_numbers = [
            parsed
            for parsed in (
                _number(item.get("bfdReceivenumber", item.get("receptionCount")))
                for item in gift_details
            )
            if parsed is not None
        ]
        reception_count = sum(reception_numbers) if reception_numbers else 0
        if isinstance(reception_count, float) and reception_count.is_integer():
            reception_count = int(reception_count)

        return {
            "instanceCode": instance_code,
            "businessFeeDetails": details,
            "giftBusinessFeeDetails": gift_details,
            "hasGiftItem": bool(gift_details),
            "giftReceptionCount": reception_count,
        }

    return enrich


# 兼容旧调用方：没有核销单号时仍返回空 dict，不触发网络请求。
def entertainment_receipt_enricher(
    receipt_code: str,
    service_data: Mapping[str, Any],
) -> dict[str, Any]:
    return build_entertainment_receipt_enricher()(receipt_code, service_data)


__all__ = [
    "DEFAULT_E15_INVOICE_TYPE_MAP_PATH",
    "E15_INVOICE_TYPE_MAP_PATH_ENV",
    "build_e15_invoice_type_enricher",
    "build_entertainment_receipt_enricher",
    "entertainment_receipt_enricher",
    "load_e15_invoice_type_map",
]
