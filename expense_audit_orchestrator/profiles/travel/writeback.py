from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# 差旅回写策略：只在回写层处理“是否属于差旅费”的兜底合规判断；
# 具体规则结果仍由差旅流程图输出，避免把未知场景误判成禁止场景。
_FORBIDDEN_TRAVEL_KEYWORDS = (
    "保险",
    "餐饮",
    "餐费",
    "签证",
    "快递",
)
_RECHARGE_CARD_KEYWORDS = ("充值卡", "预付卡", "储值卡")


def _first_value(record: Mapping[str, Any] | None, *keys: str) -> Any:
    if not isinstance(record, Mapping):
        return None
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _audit_info(service_data: Mapping[str, Any]) -> Mapping[str, Any]:
    value = service_data.get("auditInfo")
    return value if isinstance(value, Mapping) else {}


def _travel_audit(service_data: Mapping[str, Any]) -> Mapping[str, Any]:
    value = service_data.get("travelAudit")
    return value if isinstance(value, Mapping) else {}


def _normalize_text(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def travel_compliance_rule(goods_name: str, item: Mapping[str, Any]) -> bool:
    """Return whether an invoice line is eligible for travel writeback.

    The graph owns W39 and returns WARNING for unknown content.  This callback
    is deliberately conservative only for the explicitly prohibited business
    scenes and recharge/prepaid cards; unknown or empty content remains eligible
    here so that the graph's manual-review result is not silently converted into
    a hard rejection by the writeback assembler.
    """
    del item
    text = _normalize_text(goods_name)
    if not text:
        return True
    if any(keyword in text for keyword in _RECHARGE_CARD_KEYWORDS):
        return False
    return not any(keyword in text for keyword in _FORBIDDEN_TRAVEL_KEYWORDS)


def _segment_value(segment: Mapping[str, Any], *keys: str) -> Any:
    value = _first_value(segment, *keys)
    if value is not None:
        return value
    raw = segment.get("raw")
    return _first_value(raw if isinstance(raw, Mapping) else {}, *keys)


def _travel_segment_to_writeback(
    segment: Mapping[str, Any],
    *,
    instance_code: str,
) -> dict[str, Any]:
    """Map a normalized travel journey to the audit service DTO shape."""
    return {
        "miInstanceCode": instance_code,
        "journeyId": _segment_value(segment, "journeyId", "tjmJourneyid"),
        "errandCode": _segment_value(segment, "errandCode", "tjmErrandCode", "tjmErrandcode"),
        "startTime": _segment_value(segment, "departureTime", "startTime", "tjmStartTime"),
        "arrivalTime": _segment_value(segment, "arrivalTime", "endTime", "tjmArrivalTime"),
        "startPlace": _segment_value(segment, "departure", "startPlace", "tjmStartPlace"),
        "arrivalPlace": _segment_value(segment, "destination", "arrivalPlace", "tjmArrivalPlace"),
        "trafficType": _segment_value(segment, "trafficType", "tjmTrafficType"),
        "subsidyDays": _segment_value(segment, "days", "subsidyDays", "tjmSubsidyDays"),
        "isDomestic": _segment_value(segment, "isDomestic", "tjmIsdomestic"),
        "userId": _segment_value(segment, "travellerId", "userId", "tjmUserid"),
        "userName": _segment_value(segment, "travellerName", "userName", "tjmUsername"),
        "localTrafficCost": _number(
            _segment_value(segment, "localTrafficApplyAmount", "localTrafficCost", "tjmLoaclTrafficCost")
        ),
        "trafficCost": _number(_segment_value(segment, "trafficAmount", "trafficCost", "tjmTrafficcost")),
        "miles": _number(_segment_value(segment, "mileage", "miles", "tjmMiles")),
        "carStandard": _number(
            _segment_value(segment, "mileageStandard", "carStandard", "tjmCarstandard")
        ),
        "roomsCost": _number(_segment_value(segment, "hotelAmount", "roomsCost", "tjmRoomscost")),
        "otherCost": _number(_segment_value(segment, "otherAmount", "otherCost", "tjmOthercost")),
        "subsidyCost": _number(_segment_value(segment, "subsidyAmount", "subsidyCost", "tjmSubsidycost")),
        "airportReturnCost": _number(
            _segment_value(segment, "airportReturnAmount", "airportReturnCost", "tjmAirportreturncost")
        ),
        "baggageCheckinCost": _number(
            _segment_value(segment, "baggageAmount", "baggageCheckinCost", "tjmBaggageCheckinCost")
        ),
    }


def travel_audit_travels_builder(
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    service_data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build ``auditTravels`` from the travel journey interface result.

    ``invoice_pairs`` is accepted to follow the common profile builder
    contract.  Journey data is document-level, therefore it is intentionally
    emitted once per journey rather than once per invoice.
    """
    del invoice_pairs
    travel_audit = _travel_audit(service_data)
    audit_info = _audit_info(service_data)
    instance_code = str(
        _first_value(audit_info, "instanceCode", "miInstanceCode")
        or _first_value(travel_audit, "instanceCode", "miInstanceCode")
        or ""
    )
    segments = travel_audit.get("travelSegments")
    if not isinstance(segments, list):
        segments = travel_audit.get("journeys")
    if not isinstance(segments, list):
        segments = []

    return [
        _travel_segment_to_writeback(segment, instance_code=instance_code)
        for segment in segments
        if isinstance(segment, Mapping)
    ]


def _prepared_input(preparation: Mapping[str, Any], result: Mapping[str, Any]) -> Mapping[str, Any]:
    for owner in (result, preparation):
        value = owner.get("preparedInput") if isinstance(owner, Mapping) else None
        if isinstance(value, Mapping):
            return value
    return {}


def _invoice_tax_rate(prepared_input: Mapping[str, Any]) -> Any:
    items = prepared_input.get("items")
    if isinstance(items, list):
        rates = [
            _first_value(item, "taxRate", "tax_rate")
            for item in items
            if isinstance(item, Mapping)
        ]
        rates = [value for value in rates if value is not None]
        if rates:
            # 一张发票有多个税率时，保留明细税率列表，避免丢失结构化信息；
            # 单一税率仍按接口常见的 scalar DTO 输出。
            distinct = list(dict.fromkeys(str(value) for value in rates))
            return rates[0] if len(distinct) == 1 else rates
    return _first_value(prepared_input, "taxRate", "tax_rate")


def travel_form_invoice_tax_views_builder(
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    service_data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build the travel ``formInvoiceTaxViews`` writeback rows."""
    audit_info = _audit_info(service_data)
    instance_code = str(_first_value(audit_info, "instanceCode", "miInstanceCode") or "")
    rows: list[dict[str, Any]] = []

    for preparation, result in invoice_pairs:
        prepared_input = _prepared_input(preparation, result)
        per_invoice_service_data = prepared_input.get("serviceData")
        if not isinstance(per_invoice_service_data, Mapping):
            per_invoice_service_data = service_data
        travel_audit = _travel_audit(per_invoice_service_data)
        tax_info = travel_audit.get("taxInfo")
        tax_info = tax_info if isinstance(tax_info, Mapping) else {}
        current_invoice = travel_audit.get("currentInvoice")
        current_invoice = current_invoice if isinstance(current_invoice, Mapping) else {}
        current_invoice_info = per_invoice_service_data.get("currentInvoiceInfo")
        current_invoice_info = current_invoice_info if isinstance(current_invoice_info, Mapping) else {}

        invoice_no = _first_value(
            prepared_input,
            "invoiceNo",
            "chequeNo",
            "serialNo",
        ) or _first_value(current_invoice, "invoiceNo", "chequeNo", "serialNo")
        invoice_amount = _number(
            _first_value(
                prepared_input,
                "invoiceAmount",
                "totalAmount",
                "invoiceTotalAmount",
            )
            or _first_value(current_invoice, "invoiceAmount", "totalAmount")
        )
        invoice_tax = _number(
            _first_value(
                prepared_input,
                "totalTaxAmount",
                "taxAmount",
                "invoiceTax",
            )
            or _first_value(current_invoice, "invoiceTax", "taxAmount")
            or _first_value(tax_info, "currentInvoiceDeductibleTax", "invoiceDeductibleTax")
        )
        deductible_tax = _number(
            _first_value(tax_info, "currentInvoiceDeductibleTax", "invoiceDeductibleTax")
            or invoice_tax
        )
        invoice_info_id = _first_value(
            prepared_input,
            "invoice_info_id",
            "invoiceInfoId",
        ) or _first_value(current_invoice_info, "aiiid", "aiid", "invoiceInfoId")

        rows.append(
            {
                "miInstanceCode": instance_code,
                "invoiceNo": invoice_no,
                "invoiceAmount": invoice_amount,
                "invoiceTaxAmount": invoice_tax,
                "invoiceDeductibleTax": deductible_tax,
                "taxRate": _invoice_tax_rate(prepared_input),
                "invoiceInfoId": invoice_info_id,
            }
        )
    return rows


__all__ = [
    "travel_audit_travels_builder",
    "travel_compliance_rule",
    "travel_form_invoice_tax_views_builder",
]
