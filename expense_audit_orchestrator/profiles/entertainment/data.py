"""业务招待费单据级数据准备和标准化。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
import json
import os
from pathlib import Path
from typing import Any

from expense_audit_orchestrator import audit_client
from expense_audit_orchestrator.observability import get_logger

from .client import EntertainmentApiClient
from ..personal_transport.data import is_taxi_invoice


_logger = get_logger("entertainment_data")

DEFAULT_E15_INVOICE_TYPE_MAP_PATH = Path(__file__).with_name("e15_invoice_type_map.json")
E15_INVOICE_TYPE_MAP_PATH_ENV = "E15_INVOICE_TYPE_MAP_PATH"

InvoiceSerialNumberProvider = Callable[[str, str, str | None], Sequence[Any]]


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_invoice_serial_prefix(invoice_no: Any) -> str | None:
    """按出租车发票连号规则提取比较前缀。

    发票号码去掉后两位后，剩余号码的前六位一致即视为连号。整个过程
    只按字符串处理，避免前导零丢失和超长发票号数值精度损失。
    """
    value = _string_value(invoice_no)
    if len(value) < 8:
        return None
    return value[:-2][:6]


def _normalize_invoice_serial_numbers(value: Any) -> list[str]:
    """兼容历史接口可能返回的多种列表项格式。"""
    if isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        item_value: Any = item
        if isinstance(item, Mapping):
            item_value = next(
                (
                    item.get(key)
                    for key in ("chequeNo", "invoiceNo", "serialNo")
                    if item.get(key) is not None
                ),
                None,
            )
        if isinstance(item_value, (Mapping, list, tuple, set)):
            continue
        normalized = _string_value(item_value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


W34_INVOICE_TYPE_VALUES = frozenset(
    {
        "RJ-001",
        "电子发票（普通发票）",
        "电子发票(普通发票)",
        "1-003",
        "增值税电子普通发票",
        "电子普通发票",
        "1-002",
        "增值税普通发票",
        "纸质普通发票",
    }
)


def _is_w34_invoice_type(
    ocr_data: Mapping[str, Any],
    service_data: Mapping[str, Any],
) -> bool:
    """判断当前发票是否属于 W34 约定的三类票种。"""
    return bool(_invoice_type_values(ocr_data, service_data).intersection(W34_INVOICE_TYPE_VALUES))


def build_w34_invoice_serial_enricher(
    *,
    service_url: str | None = None,
    provider: InvoiceSerialNumberProvider | None = None,
) -> Callable[[str, str, dict[str, Any], dict[str, Any]], dict[str, Any]]:
    """构造 W34 发票连号查询 enricher。

    W34 只检查电子发票（普通发票）、增值税电子普通发票和增值税普通发票，
    并通过 invoice-serial-number 接口查询跨核销单发票号码。接口返回当前
    发票本身时将其过滤，避免一张发票与自身比较产生误报。
    """
    resolved_service_url = service_url or audit_client.DEFAULT_AUDIT_SERVICE_URL
    invoice_serial_provider = provider or partial(
        audit_client.fetch_invoice_serial_numbers,
        service_url=resolved_service_url,
    )

    def enricher(
        receipt_code: str,
        file_path: str,
        ocr_data: dict[str, Any],
        service_data: dict[str, Any],
    ) -> dict[str, Any]:
        del file_path
        ocr = ocr_data if isinstance(ocr_data, Mapping) else {}
        services = service_data if isinstance(service_data, Mapping) else {}
        invoice_no = (
            _string_value(ocr.get("chequeNo"))
            or _string_value(ocr.get("invoiceNo"))
            or _string_value(ocr.get("serialNo"))
        )
        is_applicable = _is_w34_invoice_type(ocr, services)
        history_numbers: list[str] = []
        lookup_failed = False

        audit_info = services.get("auditInfo")
        if not isinstance(audit_info, Mapping):
            audit_info = {}
        instance_code = _string_value(audit_info.get("instanceCode")) or receipt_code
        accounting_code = (
            _string_value(ocr.get("accountingCode"))
            or _string_value(audit_info.get("accountingCode"))
        )

        if is_applicable and invoice_no:
            try:
                history_numbers = [
                    number
                    for number in _normalize_invoice_serial_numbers(
                        invoice_serial_provider(invoice_no, instance_code, accounting_code) or []
                    )
                    if number != invoice_no
                ]
            except Exception as exc:
                lookup_failed = True
                _logger.warning(
                    "业务招待费 W34 发票连号查询失败，降级为空列表",
                    extra={
                        "event": "data_prep.entertainment_w34_invoice_serial.fallback",
                        "receipt_code": receipt_code,
                        "instance_code": instance_code,
                        "cheque_no": invoice_no,
                        "accounting_code": accounting_code,
                        "error": str(exc),
                    },
                )

        return {
            "invoiceNo": invoice_no,
            "isApplicable": is_applicable,
            "historyNumbers": history_numbers,
            "historyHit": bool(history_numbers),
            "lookupFailed": lookup_failed,
            "relationSubject": "发票",
        }

    return enricher


def build_entertainment_taxi_invoice_serial_enricher(
    *,
    service_url: str | None = None,
    provider: InvoiceSerialNumberProvider | None = None,
) -> Callable[[str, str, dict[str, Any], dict[str, Any]], dict[str, Any]]:
    """构造 E34 的出租车历史及本核销单连号数据准备。

    E34 使用出租车专用的 ``invoice-serial-number-taxi`` 历史接口，并由
    应用层继续聚合本核销单内的出租车发票。历史接口返回空列表且本单无
    连号时通过；查询失败会保留 ``lookupFailed``，交由流程图拒绝，避免
    把接口故障静默当成无连号。
    """
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
        ocr = ocr_data if isinstance(ocr_data, Mapping) else {}
        services = service_data if isinstance(service_data, Mapping) else {}
        invoice_no = (
            _string_value(ocr.get("chequeNo"))
            or _string_value(ocr.get("invoiceNo"))
            or _string_value(ocr.get("serialNo"))
        )
        current_prefix = normalize_invoice_serial_prefix(invoice_no)
        taxi_invoice = is_taxi_invoice(ocr)
        history_numbers: list[str] = []
        lookup_failed = False

        audit_info = services.get("auditInfo")
        if not isinstance(audit_info, Mapping):
            audit_info = {}
        instance_code = _string_value(audit_info.get("instanceCode")) or receipt_code
        accounting_code = (
            _string_value(ocr.get("accountingCode"))
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
                    "业务招待费 E34 出租车发票历史连号查询失败，降级为空列表",
                    extra={
                        "event": "data_prep.entertainment_e34_taxi_invoice_serial.fallback",
                        "receipt_code": receipt_code,
                        "instance_code": instance_code,
                        "cheque_no": invoice_no,
                        "accounting_code": accounting_code,
                        "error": str(exc),
                    },
                )

        return {
            "invoiceNo": invoice_no,
            "currentPrefix": current_prefix,
            "historyNumbers": history_numbers,
            "historyHit": bool(history_numbers),
            "batchHit": False,
            "isTaxiInvoice": taxi_invoice,
            "isEntertainmentInvoice": taxi_invoice,
            "relationSubject": "出租车发票",
            "lookupFailed": lookup_failed,
        }

    return enricher


# 兼容已有外部调用方；正式 profile 使用名称更明确的 W34 enricher。
build_entertainment_invoice_serial_enricher = build_w34_invoice_serial_enricher


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
            # 没有核销单号时无法确认项目类别，不能把“无法查询”误判成
            # “不是赠送纪念品”。W33 会据此输出 WARNING 并提示人工处理。
            return {
                "giftDetailLookupStatus": "error",
                "giftDetailLookupError": "缺少核销单号，无法查询业务费用明细。",
                "businessFeeDetails": [],
                "giftBusinessFeeDetails": [],
                "hasGiftItem": False,
                "giftReceptionCount": 0,
            }

        lookup_status = "success"
        lookup_error = ""
        try:
            details = _normalize_business_fee_details(
                api_client.fetch_business_fee_details(instance_code)
            )
        except Exception as exc:
            # 外部接口异常必须显式进入 W33 WARNING，不能降级成“无赠送纪念品”
            # 后再返回 PASS；否则接口故障会被隐藏。
            lookup_status = "error"
            lookup_error = str(exc) or "业务费用明细接口返回异常。"
            _logger.warning(
                "获取业务招待费业务费用明细失败，W33 标记为 WARNING",
                extra={
                    "event": "entertainment.business_fee_details.error",
                    "receipt_code": receipt_code,
                    "instance_code": instance_code,
                    "error": lookup_error,
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
            "giftDetailLookupStatus": lookup_status,
            "giftDetailLookupError": lookup_error,
            "businessFeeDetails": details,
            "giftBusinessFeeDetails": gift_details,
            "hasGiftItem": bool(gift_details),
            "giftReceptionCount": reception_count,
        }

    return enrich


# 兼容旧调用方：没有核销单号时不触发网络请求，但显式返回 W33 查询异常。
def entertainment_receipt_enricher(
    receipt_code: str,
    service_data: Mapping[str, Any],
) -> dict[str, Any]:
    return build_entertainment_receipt_enricher()(receipt_code, service_data)


__all__ = [
    "DEFAULT_E15_INVOICE_TYPE_MAP_PATH",
    "E15_INVOICE_TYPE_MAP_PATH_ENV",
    "W34_INVOICE_TYPE_VALUES",
    "build_e15_invoice_type_enricher",
    "build_entertainment_invoice_serial_enricher",
    "build_entertainment_receipt_enricher",
    "build_entertainment_taxi_invoice_serial_enricher",
    "build_w34_invoice_serial_enricher",
    "entertainment_receipt_enricher",
    "load_e15_invoice_type_map",
    "normalize_invoice_serial_prefix",
]
