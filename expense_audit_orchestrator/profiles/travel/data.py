"""差旅费单据级、发票级数据准备和标准化。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from expense_audit_orchestrator.observability import get_logger

from .client import TRAVEL_API_PREFIX, TravelApiClient


_logger = get_logger("travel_data")

NOT_READY_MESSAGE = "差旅接口数据待接入，当前按通过处理"


def _value(data: Mapping[str, Any] | None, *keys: str, default: Any = None) -> Any:
    if not isinstance(data, Mapping):
        return default
    for key in keys:
        if key not in data or data[key] is None:
            continue
        value = data[key]
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                continue
        return value
    return default


def _text(data: Mapping[str, Any] | None, *keys: str) -> str | None:
    value = _value(data, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def _sum_numbers(values: Sequence[Any]) -> float | int | None:
    numbers = [_number(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        return None
    total = sum(numbers)
    return int(total) if float(total).is_integer() else total


def _records(raw: Any, nested_keys: Sequence[str] = ()) -> list[dict[str, Any]]:
    """兼容接口直接返回 list、单对象或带 monthly/selfPurchased 包装对象。"""
    if isinstance(raw, Mapping):
        values: list[Any] = []
        for key in nested_keys:
            nested = raw.get(key)
            if isinstance(nested, list):
                values.extend(nested)
        if values:
            return [dict(item) for item in values if isinstance(item, Mapping)]
        return [dict(raw)]

    if not isinstance(raw, list):
        return []

    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        nested_found = False
        for key in nested_keys:
            nested = item.get(key)
            if isinstance(nested, list):
                result.extend(dict(child) for child in nested if isinstance(child, Mapping))
                nested_found = True
        if not nested_found:
            result.append(dict(item))
    return result


def _records_with_source(raw: Any, nested_sources: Mapping[str, str]) -> list[dict[str, Any]]:
    """展开月结/自购结构，并为订单保留来源，供月结火车规则使用。"""
    result: list[dict[str, Any]] = []
    if isinstance(raw, Mapping):
        for key, source in nested_sources.items():
            values = raw.get(key)
            if isinstance(values, list):
                result.extend({**dict(item), "_source": source} for item in values if isinstance(item, Mapping))
        if result:
            return result
        return [dict(raw)]

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            found = False
            for key, source in nested_sources.items():
                values = item.get(key)
                if isinstance(values, list):
                    result.extend({**dict(child), "_source": source} for child in values if isinstance(child, Mapping))
                    found = True
            if not found:
                result.append(dict(item))
    return result


def _parse_hour(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        # ISO 时间、常见中文/空格日期格式均取时间部分。
        normalized = text.replace("T", " ").replace("/", "-")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(normalized, fmt).hour
            except ValueError:
                continue
        if ":" in text:
            return int(text.split(":", 1)[0][-2:])
    except (TypeError, ValueError):
        return None
    return None


def _order_time(record: Mapping[str, Any], *keys: str) -> Any:
    return _value(record, *keys)


def _is_station_time_order(record: Mapping[str, Any]) -> bool:
    departure_hour = _parse_hour(
        _order_time(record, "departureTime", "departuretime", "takeoffTime", "takeofftime", "startTime")
    )
    arrival_hour = _parse_hour(
        _order_time(record, "arrivalTime", "arrivaltime", "landingTime", "landingtime", "endTime")
    )
    return (departure_hour is not None and departure_hour < 9) or (
        arrival_hour is not None and arrival_hour >= 22
    )


def _normalize_journeys(raw: Any) -> list[dict[str, Any]]:
    rows = _records(raw)
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "id": _value(row, "tjmCode", "id"),
                "journeyId": _value(row, "tjmJourneyid", "journeyid", "journeyId"),
                "errandCode": _value(row, "tjmErrandCode", "tjmErrandcode", "errandcode"),
                "errandGuid": _value(row, "tjmErrandGuid", "errandGuid"),
                "departureTime": _value(row, "tjmStartTime", "startTime", "departureTime"),
                "arrivalTime": _value(row, "tjmArrivalTime", "arrivalTime", "endTime"),
                "departure": _value(row, "tjmStartPlace", "startPlace", "departure"),
                "destination": _value(row, "tjmArrivalPlace", "arrivalPlace", "destination"),
                "days": _number(_value(row, "tjmSubsidyDays", "days")),
                "trafficType": _value(row, "tjmTrafficType", "trafficType"),
                "trafficTypeId": _value(row, "tjmTrafficTypeId", "trafficTypeId"),
                "isDomestic": _value(row, "tjmIsdomestic", "isDomestic"),
                "localTrafficApplyAmount": _number(_value(row, "tjmLoaclTrafficCost", "localTrafficCost")),
                "localTrafficStandard": _number(
                    _value(row, "tjmLoaclTrafficStandard", "localTrafficStandard")
                ),
                "trafficAmount": _number(_value(row, "tjmTrafficcost", "trafficCost")),
                "airportReturnAmount": _number(
                    _value(row, "tjmAirportreturncost", "airportReturnCost", "stationVehicleApplyAmount")
                ),
                "baggageAmount": _number(_value(row, "tjmBaggageCheckinCost", "baggageCheckinCost")),
                "mileage": _number(_value(row, "tjmMiles", "miles")),
                "mileageStandard": _number(_value(row, "tjmCarstandard", "carStandard")),
                "travellerId": _value(row, "tjmUserid", "userId"),
                "travellerName": _value(row, "tjmUsername", "userName"),
                "purpose": _value(row, "tjmItinerarydescription", "itineraryDescription"),
                "raw": row,
            }
        )
    return result


def _normalize_orders(raw: Any, *, kind: str) -> list[dict[str, Any]]:
    nested_sources = {
        "monthlyAirTickets": "monthly",
        "selfPurchasedAirTickets": "self_purchased",
        "monthlyTrainTickets": "monthly",
        "selfPurchasedTrainTickets": "self_purchased",
        "monthlyDidiOrders": "monthly",
        "selfPurchasedCityTransports": "self_purchased",
        "monthlyHotels": "monthly",
        "selfPurchasedHotels": "self_purchased",
    }
    rows = _records_with_source(raw, nested_sources)
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "kind": kind,
                "source": _value(row, "_source", "source", "orderType", "ordertype"),
                "id": _value(
                    row, "id", "recordid", "RecordID", "iD", "ticketnumber",
                    "ticketNumber", "transactionCode", "orderno",
                ),
                "ticketNumber": _value(
                    row, "ticketnumber", "ticketNumber", "ttmTicketcode", "transactionCode",
                    "teCode", "orderNo", "orderno", "originticketno",
                ),
                "date": _value(
                    row, "date", "flydate", "flyDate", "startTime", "starttime",
                    "departuretime", "departureTime", "ttmFlytime", "takeoffTime",
                ),
                "departureTime": _value(
                    row, "departuretime", "departureTime", "ttmFlytime", "startTime",
                    "takeoffTime",
                ),
                "arrivalTime": _value(
                    row, "arrivaltime", "arrivalTime", "ttmArrivaltime", "endTime",
                    "landingTime", "arrivaldate",
                ),
                "departure": _value(
                    row, "departure", "departurecity", "departureCity", "departureaddress",
                    "ttmFlycity",
                ),
                "destination": _value(
                    row, "destination", "arrivalcity", "arrivalCity", "arrivaladdress",
                    "ttmArrivalcity", "teCityName", "city",
                ),
                "flightNo": _value(row, "flightno", "flightNo", "ttmFlithtno", "shift"),
                "trainNo": _value(row, "trainno", "trainNo", "shift"),
                "passengerId": _value(row, "userid", "userId", "ttmStaffno", "workId"),
                "passengerName": _value(row, "username", "userName", "ttmStaffname"),
                "seat": _value(row, "seat", "ttmShippingspace"),
                "amount": _number(
                    _value(
                        row, "amount", "moneytotal", "moneybycompany", "ttmTotalamount",
                        "ttmTickeprice", "teTotla", "total",
                    )
                ),
                "errandCode": _value(row, "errandcode", "teErrandcode", "teErrandCode"),
                "journeyId": _value(row, "journeyid", "journeyId"),
                "airlineCode": _value(row, "airlineCode", "airlinecode"),
                "airlineName": _value(row, "airlineName", "airlinename", "suppliername"),
                "weight": _number(_value(row, "weight", "baggageWeight")),
                "weightUnit": _value(row, "weightUnit"),
                "raw": row,
            }
        )
    return result


def _normalize_other_expenses(raw: Any) -> list[dict[str, Any]]:
    rows = _records(raw)
    result: list[dict[str, Any]] = []
    for row in rows:
        type_code = str(_value(row, "typecode", "typeCode", default="") or "")
        result.append(
            {
                "id": _value(row, "id"),
                "typeCode": type_code,
                "typeName": _value(row, "typename", "typeName"),
                "ticketNumber": _value(row, "ticketnumber", "ticketNumber"),
                "date": _value(row, "date"),
                "amount": _number(_value(row, "amount")),
                "shift": _value(row, "shift"),
                "departure": _value(row, "departure"),
                "destination": _value(row, "destination"),
                "passengerId": _value(row, "userid", "userId"),
                "passengerName": _value(row, "username", "userName"),
                "airlineCode": _value(row, "airlineCode", "airlinecode"),
                "airlineName": _value(row, "airlineName", "airlinename"),
                "flightNo": _value(row, "flightNo", "flightno", "shift"),
                "weight": _number(_value(row, "weight", "baggageWeight")),
                "weightUnit": _value(row, "weightUnit"),
                "raw": row,
            }
        )
    return result


def _normalize_driving(raw: Any) -> list[dict[str, Any]]:
    rows = _records(raw)
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "id": _value(row, "id"),
                "date": _value(row, "date"),
                "amount": _number(_value(row, "amount")),
                "miles": _number(_value(row, "miles")),
                "departure": _value(row, "departure", "startplace"),
                "destination": _value(row, "destination", "arrivalplace"),
                "standard": _number(_value(row, "standard")),
                "currencyCode": _value(row, "currencycode", "currencyCode"),
                "rate": _number(_value(row, "rate")),
                "errandCode": _value(row, "errandcode", "errandCode"),
                "journeyId": _value(row, "journeyid", "journeyId"),
                "source": _value(row, "source"),
                "raw": row,
            }
        )
    return result


def _normalize_subsidies(raw: Any) -> list[dict[str, Any]]:
    rows = _records(raw)
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "id": _value(row, "id"),
                "errandCode": _value(row, "errandCode", "errandcode"),
                "days": _number(_value(row, "days")),
                "dailyAmount": _number(_value(row, "amount")),
                "total": _number(_value(row, "total")),
                "standard": _number(_value(row, "standard")),
                "currencyCode": _value(row, "currencyCode", "currencycode"),
                "rate": _number(_value(row, "rate")),
                "city": _value(row, "city"),
                "userId": _value(row, "userid", "userId"),
                "userName": _value(row, "username", "userName"),
                "raw": row,
            }
        )
    return result


def _build_station_standard(
    journeys: list[dict[str, Any]],
    flight_orders: list[dict[str, Any]],
    train_orders: list[dict[str, Any]],
) -> tuple[dict[str, Any], float | int | None, float | int | None]:
    apply_amount = _sum_numbers(item.get("airportReturnAmount") for item in journeys)
    explicit_allowed = _sum_numbers(
        _value(item.get("raw"), "stationVehicleAllowedAmount", "airportReturnAllowedAmount") for item in journeys
    )
    station_orders = [*flight_orders, *train_orders]
    eligible_orders = [item for item in station_orders if _is_station_time_order(item)]
    if explicit_allowed is not None:
        allowed_amount = explicit_allowed
    elif eligible_orders or (apply_amount in (None, 0)):
        allowed_amount = len(eligible_orders) * 150
    else:
        allowed_amount = None

    result = {
        "unitAmount": 150,
        "eligibleOrderCount": len(eligible_orders),
        "allowedAmount": allowed_amount,
        "applyAmount": apply_amount,
        "orders": eligible_orders,
    }
    return result, apply_amount, allowed_amount


def _empty_travel_audit(instance_code: str | None = None, message: str = NOT_READY_MESSAGE) -> dict[str, Any]:
    return {
        "instanceCode": instance_code or "",
        "employeeRole": "",
        "verifiUserName": "",
        "travelStartDate": None,
        "travelEndDate": None,
        "actualTravelDays": None,
        "travelSegments": [],
        "cityStandards": [],
        "stationVehicleStandard": {},
        "expenseItems": [],
        "flightOrders": [],
        "trainOrders": [],
        "hotelOrders": [],
        "transportOrders": [],
        "selfDrivingMileage": [],
        "subsidyInfo": {},
        "baggageInfo": [],
        "taxInfo": {},
        "invoiceScene": None,
        "selfBoughtMonthlyTrain": None,
        "ruleStates": {},
        "sourceStatus": {},
        "primaryInvoice": True,
        "raisedRuleCodes": [],
        "messages": [message] if message else [],
    }


def _travel_date_bounds(journeys: Sequence[Mapping[str, Any]]) -> tuple[Any, Any, float | int | None]:
    starts = [item.get("departureTime") for item in journeys if item.get("departureTime")]
    ends = [item.get("arrivalTime") for item in journeys if item.get("arrivalTime")]
    days = _sum_numbers(item.get("days") for item in journeys)
    return (min(starts) if starts else None, max(ends) if ends else None, days)


def normalize_travel_data(
    raw: Mapping[str, Any],
    *,
    instance_code: str,
    audit_info: Mapping[str, Any],
) -> dict[str, Any]:
    journeys = _normalize_journeys(raw.get("journeys"))
    flight_orders = _normalize_orders(raw.get("airTickets"), kind="flight")
    train_orders = _normalize_orders(raw.get("trainTickets"), kind="train")
    hotel_orders = _normalize_orders(raw.get("hotels"), kind="hotel")
    city_orders = _normalize_orders(raw.get("cityTransports"), kind="city_transport")
    other_transport_orders = _normalize_orders(raw.get("otherTransports"), kind="other_transport")
    driving = _normalize_driving(raw.get("drivingCars"))
    subsidies = _normalize_subsidies(raw.get("travelSubsidies"))
    other_expenses = _normalize_other_expenses(raw.get("otherExpenses"))
    business_fee_details = _records(raw.get("businessFeeDetails"))

    post_type_code = _text(audit_info, "postTypeCode", "posttypecode")
    post_type = _text(audit_info, "postType", "posttype", "postTypeName")
    if not post_type and post_type_code in {"7", "销售"}:
        post_type = "销售"
    elif not post_type and post_type_code in {"6", "售前"}:
        post_type = "售前"

    station_standard, station_apply, station_allowed = _build_station_standard(
        journeys, flight_orders, train_orders
    )
    travel_start_date, travel_end_date, actual_travel_days = _travel_date_bounds(journeys)
    city_apply = _sum_numbers(item.get("localTrafficApplyAmount") for item in journeys)
    if city_apply is None:
        city_apply = _sum_numbers(item.get("amount") for item in city_orders)
    city_invoice = _sum_numbers(item.get("amount") for item in city_orders)
    city_standard_only = _sum_numbers(
        (_number(item.get("localTrafficStandard")) or 0) * (_number(item.get("days")) or 0)
        for item in journeys
    )
    if station_allowed is None and station_apply not in (None, 0):
        city_standard = None
    else:
        city_standard = _sum_numbers([city_standard_only, station_allowed or 0])

    subsidy_apply = _sum_numbers(item.get("total") for item in subsidies)
    subsidy_calculated_values: list[Any] = []
    for item in subsidies:
        days = _number(item.get("days"))
        standard = _number(item.get("standard"))
        rate = _number(item.get("rate")) or 1
        if days is not None and standard is not None:
            subsidy_calculated_values.append(days * standard * rate)
    subsidy_calculated = _sum_numbers(subsidy_calculated_values)

    expense_items: list[dict[str, Any]] = []
    expense_items.extend({**item, "expenseType": "flight"} for item in flight_orders)
    expense_items.extend({**item, "expenseType": "train"} for item in train_orders)
    expense_items.extend({**item, "expenseType": "hotel"} for item in hotel_orders)
    expense_items.extend({**item, "expenseType": "city_transport"} for item in city_orders)
    expense_items.extend({**item, "expenseType": "other_transport"} for item in other_transport_orders)
    expense_items.extend({**item, "expenseType": "self_driving"} for item in driving)
    expense_items.extend({**item, "expenseType": "subsidy"} for item in subsidies)
    expense_items.extend({**item, "expenseType": "travel_other"} for item in other_expenses)

    source_status = dict(raw.get("sourceStatus") or {})
    messages = [
        str(status.get("message"))
        for status in source_status.values()
        if isinstance(status, Mapping) and status.get("status") != "READY" and status.get("message")
    ]
    if not source_status:
        messages.append(NOT_READY_MESSAGE)

    return {
        "instanceCode": instance_code,
        "employeeRole": post_type or "",
        "employeeRoleCode": post_type_code or "",
        "verifiUserName": _text(audit_info, "verifiUserName", "verifyUserName") or "",
        "travelStartDate": travel_start_date,
        "travelEndDate": travel_end_date,
        "actualTravelDays": actual_travel_days,
        "travelSegments": journeys,
        "cityStandards": [
            {
                "city": item.get("destination"),
                "days": item.get("days"),
                "standard": item.get("localTrafficStandard"),
                "currencyCode": _value(item.get("raw"), "tjmLoaclTrafficCurrencyCode"),
                "journeyId": item.get("journeyId"),
            }
            for item in journeys
        ],
        "stationVehicleStandard": station_standard,
        "cityTransportApplyAmount": city_apply,
        "cityTransportStandardAmount": city_standard,
        "cityTransportInvoiceAmount": city_invoice,
        "stationVehicleApplyAmount": station_apply,
        "stationVehicleAllowedAmount": station_allowed,
        "expenseItems": expense_items,
        "flightOrders": flight_orders,
        "trainOrders": train_orders,
        "hotelOrders": hotel_orders,
        "transportOrders": other_transport_orders,
        "selfDrivingMileage": driving,
        "selfDrivingApplyAmount": _sum_numbers(item.get("amount") for item in driving),
        "selfDrivingTheoryAmount": _sum_numbers(
            (_number(item.get("miles")) or 0) * (_number(item.get("standard")) or 0) * (_number(item.get("rate")) or 1)
            for item in driving
        ),
        "selfDrivingInvoiceAmount": _sum_numbers(item.get("amount") for item in driving),
        "subsidyInfo": {
            "items": subsidies,
            "businessFeeDetails": business_fee_details,
            "applyAmount": subsidy_apply,
            "calculatedAmount": subsidy_calculated,
            "mealMeetingApplyAmount": None,
            "mealMeetingAllowedAmount": None,
        },
        "baggageInfo": [item for item in other_expenses if item.get("typeCode") == "2001"],
        "taxInfo": {},
        "invoiceScene": None,
        "selfBoughtMonthlyTrain": None,
        "ruleStates": {},
        "sourceStatus": source_status,
        "primaryInvoice": True,
        "raisedRuleCodes": [],
        "messages": messages or [NOT_READY_MESSAGE],
        "raw": {
            key: value
            for key, value in raw.items()
            if key != "sourceStatus"
        },
    }


def build_travel_receipt_enricher(
    *,
    service_url: str | None = None,
    client: TravelApiClient | None = None,
):
    """构造绑定服务地址的差旅收据级 enricher。

    profile 在 bootstrap 中创建时可传入显式 ``audit_service_url``，避免
    差旅接口退回默认地址；直接调用 ``travel_receipt_enricher`` 的旧用法保持不变。
    """
    api_client = client or TravelApiClient(service_url=service_url)

    def enricher(receipt_code: str, service_data: Mapping[str, Any]) -> dict[str, Any]:
        return travel_receipt_enricher(receipt_code, service_data, client=api_client)

    return enricher


def travel_receipt_enricher(
    receipt_code: str,
    service_data: Mapping[str, Any],
    *,
    client: TravelApiClient | None = None,
) -> dict[str, Any]:
    """收据级差旅数据聚合器；外部接口失败时始终 fail-open。"""
    del receipt_code
    audit_info = service_data.get("auditInfo") if isinstance(service_data, Mapping) else None
    audit_info = audit_info if isinstance(audit_info, Mapping) else {}
    instance_code = _text(audit_info, "instanceCode")
    if not instance_code:
        return _empty_travel_audit(message=NOT_READY_MESSAGE)

    api_client = client or TravelApiClient()
    try:
        raw = api_client.fetch_all(instance_code)
    except Exception as exc:  # 防御可注入客户端整体失败
        _logger.warning(
            "差旅聚合客户端失败，降级为空数据",
            extra={"event": "travel_data.aggregate.fallback", "instance_code": instance_code, "error": str(exc)},
        )
        raw = {"sourceStatus": {}}
    return normalize_travel_data(raw, instance_code=instance_code, audit_info=audit_info)


def _is_rail_invoice(ocr_data: Mapping[str, Any]) -> bool:
    invoice_type = " ".join(
        str(_value(ocr_data, key, default="") or "")
        for key in ("invoiceType", "invoiceTypeCode", "invoiceTypeName", "invoiceKind")
    )
    goods_name = str(_value(ocr_data, "goodsName", default="") or "")
    return "RJ-010" in invoice_type or "数电铁路" in invoice_type or "RJ-010" in goods_name or "铁路" in goods_name


def _resolve_monthly_train_state(ocr_data: Mapping[str, Any], train_orders: Sequence[Mapping[str, Any]]) -> bool | None:
    if not _is_rail_invoice(ocr_data):
        return False
    invoice_no = _text(ocr_data, "invoiceNo", "chequeNo", "serialNo", "ticketNumber")
    train_no = _text(ocr_data, "trainNo", "trainno", "shift")
    invoice_date = _text(ocr_data, "invoiceDate", "date", "travelDate")
    candidates = list(train_orders)
    if invoice_no:
        candidates = [item for item in candidates if invoice_no in {str(item.get("ticketNumber") or ""), str(item.get("id") or "")}]
    if train_no and candidates:
        candidates = [item for item in candidates if str(item.get("trainNo") or "") == train_no] or candidates
    if invoice_date and candidates:
        candidates = [item for item in candidates if str(item.get("date") or "").startswith(invoice_date[:10])] or candidates
    if not candidates:
        return None
    return any(str(item.get("source") or "").lower() == "monthly" for item in candidates)


def travel_invoice_enricher(
    receipt_code: str,
    file_path: str,
    ocr_data: dict[str, Any],
    service_data: dict[str, Any],
) -> dict[str, Any]:
    """发票级差旅字段合并器，保留收据级接口结果。"""
    del receipt_code, file_path
    existing = service_data.get("travelAudit")
    travel_audit = dict(existing) if isinstance(existing, Mapping) else _empty_travel_audit()
    ocr = ocr_data if isinstance(ocr_data, Mapping) else {}

    tax_info = dict(travel_audit.get("taxInfo") or {})
    invoice_tax = _number(_value(ocr, "effectiveTaxAmount", "totalTaxAmount", "taxAmount"))
    if invoice_tax is not None:
        tax_info["invoiceDeductibleTax"] = invoice_tax
    form_tax = _number(
        _value(
            service_data,
            "formInputTax",
            "inputTaxAmount",
            "formTaxAmount",
        )
    )
    if form_tax is None:
        audit_info = service_data.get("auditInfo")
        if isinstance(audit_info, Mapping):
            form_tax = _number(_value(audit_info, "formInputTax", "inputTaxAmount", "formTaxAmount"))
    if form_tax is not None:
        tax_info["formInputTax"] = form_tax

    invoice_snapshot = {
        "invoiceNo": _value(ocr, "invoiceNo", "chequeNo", "serialNo"),
        "invoiceDate": _value(ocr, "invoiceDate", "date"),
        "invoiceAmount": _number(_value(ocr, "invoiceAmount", "totalAmount", "amount")),
        "passengerName": _value(ocr, "passengerName", "buyerName", "travellerName"),
        "flightNo": _value(ocr, "flightNo", "flightno"),
        "trainNo": _value(ocr, "trainNo", "trainno"),
        "goodsName": _value(ocr, "goodsName"),
        "invoiceType": _value(ocr, "invoiceType", "invoiceTypeCode", "invoiceTypeName"),
    }

    merged = {
        **travel_audit,
        "taxInfo": tax_info,
        "invoiceScene": travel_audit.get("invoiceScene"),
        "selfBoughtMonthlyTrain": _resolve_monthly_train_state(ocr, travel_audit.get("trainOrders") or []),
        "currentInvoice": invoice_snapshot,
        "primaryInvoice": travel_audit.get("primaryInvoice", True),
        "raisedRuleCodes": list(travel_audit.get("raisedRuleCodes") or []),
    }
    return merged


__all__ = [
    "NOT_READY_MESSAGE",
    "build_travel_receipt_enricher",
    "normalize_travel_data",
    "travel_invoice_enricher",
    "travel_receipt_enricher",
]
