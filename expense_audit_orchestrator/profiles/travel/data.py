"""差旅费单据级、发票级数据准备和标准化。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import re
from typing import Any

from expense_audit_orchestrator.observability import get_logger

from .client import TRAVEL_API_PREFIX, TravelApiClient


_logger = get_logger("travel_data")

NOT_READY_MESSAGE = "差旅接口数据未就绪，无法完成对应稽核，需人工复核"

# 接口文档中的类型编码。不要用 typeName 作为唯一判断依据，历史数据中
# typeName 存在中英文和人工录入差异，typeCode 更稳定。
TRAVEL_OTHER_EXPENSE_TYPES = {
    "2001": "baggage",
    "2008": "vaccine",
    "2007": "network_card",
    "2004": "refund_change",
}

_TRAVEL_SCENE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "flight": ("机票", "航空", "飞机", "燃油附加费", "民航发展基金", "其他税费"),
    "train": ("铁路", "火车", "车票", "高铁", "动车", "train"),
    "self_driving": ("自驾", "电费", "供电", "充电", "汽油", "加油", "通行费"),
    "other_transport": ("轮船", "船票", "大巴", "客车", "长途客运"),
    "city_transport": ("通行", "租赁", "现代服务", "乘车", "客运", "加油", "充电", "出租", "滴滴"),
    "hotel": ("住宿", "房费", "酒店", "宾馆"),
    "baggage": ("运输服务", "行李", "托运"),
    "vaccine": ("诊疗", "疫苗", "检查费"),
    "network_card": ("电信服务", "信息系统增值服务", "网络电话卡", "电话卡", "上网卡"),
    "refund_change": ("退票", "退改", "改期", "退改签"),
}
_FORBIDDEN_TRAVEL_SCENE_KEYWORDS = ("保险", "餐饮", "餐费", "签证", "快递")


def _date_key(value: Any) -> str | None:
    """将接口/OCR 中的日期归一化为 YYYY-MM-DD。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.search(r"(20\d{2})(\d{2})(\d{2})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def _normalized_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[\s\u3000\-_/·,.，。()（）]", "", str(value)).casefold()


def _same_person(left_id: Any, left_name: Any, right_id: Any, right_name: Any) -> bool | None:
    """优先按用户 ID 匹配，只有双方没有 ID 时才按姓名匹配。"""
    left_id_text = _normalized_text(left_id)
    right_id_text = _normalized_text(right_id)
    if left_id_text and right_id_text:
        return left_id_text == right_id_text
    left_name_text = _normalized_text(left_name)
    right_name_text = _normalized_text(right_name)
    if left_name_text and right_name_text:
        return left_name_text == right_name_text
    return None


def _money_equal(left: Any, right: Any, tolerance: float = 0.01) -> bool | None:
    left_number = _number(left)
    right_number = _number(right)
    if left_number is None or right_number is None:
        return None
    return abs(float(left_number) - float(right_number)) <= tolerance


def _source_ready(source_status: Mapping[str, Any], raw: Mapping[str, Any], key: str) -> bool:
    status = source_status.get(key)
    if isinstance(status, Mapping):
        return str(status.get("status") or "").upper() == "READY"
    # 没有 sourceStatus 时兼容旧的裸数据，但只有实际提供该字段才算可用。
    # 这也能避免客户端整体失败时只返回 {sourceStatus: {}} 仍被当作空列表放行。
    return key in raw


def _source_message(source_status: Mapping[str, Any], *keys: str) -> str:
    messages = []
    for key in keys:
        status = source_status.get(key)
        if isinstance(status, Mapping) and status.get("status") != "READY":
            message = str(status.get("message") or "").strip()
            if message:
                messages.append(message)
    return "；".join(dict.fromkeys(messages)) or NOT_READY_MESSAGE


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


def _classify_travel_scene(goods_name: Any, *, extra_text: Any = "") -> tuple[str, str | None]:
    """确定性分类 W39，返回 ``(state, scene)``。

    ``pass`` 表示命中差旅允许场景，``warning`` 表示命中禁止场景或无法
    通过关键词确定，后者不能再被当作 PASS 放行。
    """
    text = f"{goods_name or ''} {extra_text or ''}".strip()
    normalized = _normalized_text(text)
    if not normalized:
        return "missing", None
    for keyword in _FORBIDDEN_TRAVEL_SCENE_KEYWORDS:
        if _normalized_text(keyword) in normalized:
            return "warning", "forbidden"
    for scene, keywords in _TRAVEL_SCENE_KEYWORDS.items():
        if any(_normalized_text(keyword) in normalized for keyword in keywords):
            return "pass", scene
    return "warning", "unknown"


def _is_rail_invoice(ocr_data: Mapping[str, Any]) -> bool:
    invoice_type = " ".join(
        str(_value(ocr_data, key, default="") or "")
        for key in ("invoiceType", "invoiceTypeCode", "invoiceTypeName", "invoiceKind")
    )
    text = " ".join(
        str(_value(ocr_data, key, default="") or "")
        for key in ("goodsName", "remark", "trainNo", "trainno")
    )
    return bool(re.search(r"RJ-010|数电铁路|铁路|火车|高铁|动车|[GDZK]\d", f"{invoice_type} {text}", re.I))


def _is_flight_invoice(ocr_data: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(_value(ocr_data, key, default="") or "")
        for key in ("goodsName", "invoiceType", "invoiceTypeName", "flightNo", "flightno")
    )
    return bool(re.search(r"机票|航空|飞机|燃油|民航|flight|[A-Z]{2}\d{3,4}", text, re.I))


def _scene_is(ocr_data: Mapping[str, Any], *scenes: str) -> bool:
    state, scene = _classify_travel_scene(
        _value(ocr_data, "goodsName", "summary", "remark"),
        extra_text=" ".join(str(ocr_data.get(key) or "") for key in ("flightNo", "trainNo", "typeName")),
    )
    del state
    return scene in scenes


def _invoice_amount(ocr_data: Mapping[str, Any]) -> float | int | None:
    return _number(
        _value(
            ocr_data,
            "validInvoiceAmount",
            "invoiceAmount",
            "totalAmount",
            "amount",
            "total",
        )
    )


def _invoice_date(ocr_data: Mapping[str, Any]) -> str | None:
    return _date_key(_value(ocr_data, "travelDate", "invoiceDate", "date", "departureDate", "orderDate"))


def _match_orders(
    ocr_data: Mapping[str, Any],
    orders: Sequence[Mapping[str, Any]],
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """按票号、车次/航班、行程 ID、日期和乘客信息做稳定匹配。

    接口文档未承诺单一关联键，因此采用“强键优先、日期/人员辅助”的
    评分策略，避免只靠金额或数组顺序误关联。
    """
    if kind:
        orders = [item for item in orders if not item.get("kind") or item.get("kind") == kind]
    if not orders:
        return []
    invoice_no = _normalized_text(_value(ocr_data, "invoiceNo", "chequeNo", "serialNo", "ticketNumber"))
    journey_id = _normalized_text(_value(ocr_data, "journeyId", "journeyid"))
    flight_no = _normalized_text(_value(ocr_data, "flightNo", "flightno"))
    train_no = _normalized_text(_value(ocr_data, "trainNo", "trainno", "shift"))
    invoice_date = _invoice_date(ocr_data)
    user_id = _value(ocr_data, "passengerId", "userId", "userid", "travellerId")
    user_name = _value(ocr_data, "passengerName", "userName", "username", "travellerName")

    scored: list[tuple[int, dict[str, Any]]] = []
    for order in orders:
        score = 0
        order_no = _normalized_text(order.get("ticketNumber") or order.get("id"))
        order_journey = _normalized_text(order.get("journeyId"))
        order_flight = _normalized_text(order.get("flightNo"))
        order_train = _normalized_text(order.get("trainNo"))
        order_date = _date_key(order.get("date") or order.get("departureTime"))
        if invoice_no and order_no:
            if invoice_no == order_no:
                score += 100
            elif invoice_no not in order_no and order_no not in invoice_no:
                continue
            else:
                score += 40
        if journey_id and order_journey:
            if journey_id == order_journey:
                score += 60
            else:
                continue
        if flight_no and order_flight:
            if flight_no == order_flight:
                score += 30
            else:
                continue
        if train_no and order_train:
            if train_no == order_train:
                score += 30
            else:
                continue
        if invoice_date and order_date:
            if invoice_date == order_date:
                score += 15
            elif score >= 40:
                continue
        person_match = _same_person(user_id, user_name, order.get("passengerId"), order.get("passengerName"))
        if person_match is True:
            score += 10
        elif person_match is False and score >= 40:
            continue
        # 没有任何识别字段时保留所有订单，由调用方根据金额/日期进一步判断。
        scored.append((score, dict(order)))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        return []
    best = scored[0][0]
    return [item for score, item in scored if score == best] if best > 0 else [item for _, item in scored]


def _seat_state(seat: Any) -> str:
    text = str(seat or "").strip()
    if not text:
        return "missing"
    if any(keyword in text for keyword in ("商务", "特等", "一等", "高级软卧")):
        return "reject"
    if any(keyword in text for keyword in ("二等", "硬座", "软座", "硬卧", "软卧", "无座", "站票")):
        return "pass"
    return "missing"


def _business_fee_meal_amounts(
    business_fee_details: Sequence[Mapping[str, Any]],
    journeys: Sequence[Mapping[str, Any]],
    subsidies: Sequence[Mapping[str, Any]],
) -> tuple[float | int | None, float | int | None]:
    apply_values: list[Any] = []
    allowed_values: list[Any] = []
    for row in [*business_fee_details, *journeys, *subsidies]:
        text = " ".join(str(value or "") for value in row.values())
        if not any(keyword in text for keyword in ("含餐", "会议", "meal", "meeting")):
            continue
        apply_values.extend(
            _value(row, key)
            for key in ("mealMeetingApplyAmount", "applyAmount", "amount", "total", "cost")
        )
        allowed_values.extend(
            _value(row, key)
            for key in ("mealMeetingAllowedAmount", "allowedAmount", "standardAmount", "allowed")
        )
    return _sum_numbers(apply_values), _sum_numbers(allowed_values)


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
                "mileageRate": _number(_value(row, "tjmCarrate", "carRate", "rate")),
                "localTrafficRate": _number(_value(row, "tjmLoacltrafficrate", "localTrafficRate", "rate")),
                "subsidyAmount": _number(_value(row, "tjmSubsidycost", "subsidyCost")),
                "subsidyStandard": _number(_value(row, "tjmSubsidystandard", "subsidyStandard")),
                "hotelAmount": _number(_value(row, "tjmRoomscost", "roomsCost")),
                "hotelStandard": _number(_value(row, "tjmRoomsstandard", "roomsStandard")),
                "otherAmount": _number(_value(row, "tjmOthercost", "otherCost")),
                "stationVehicleAllowedAmount": _number(
                    _value(row, "stationVehicleAllowedAmount", "airportReturnAllowedAmount")
                ),
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
                "reason": _value(row, "reason", "remark"),
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
                "weight": _number(_value(row, "weight", "baggageWeight", "baggageweight")),
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


def _build_receipt_rule_states(
    *,
    journeys: Sequence[Mapping[str, Any]],
    flight_orders: Sequence[Mapping[str, Any]],
    train_orders: Sequence[Mapping[str, Any]],
    city_orders: Sequence[Mapping[str, Any]],
    driving: Sequence[Mapping[str, Any]],
    subsidies: Sequence[Mapping[str, Any]],
    business_fee_details: Sequence[Mapping[str, Any]],
    source_status: Mapping[str, Any],
    employee_role: str,
    city_apply: float | int | None,
    city_standard: float | int | None,
    city_invoice: float | int | None,
    station_apply: float | int | None,
    station_allowed: float | int | None,
    subsidy_apply: float | int | None,
    subsidy_calculated: float | int | None,
    self_driving_theory: float | int | None,
    raw_sources: Mapping[str, Any],
) -> dict[str, str]:
    """生成无需 OCR 的核销单级状态。

    状态值是流程图的数据契约：pass/reject/missing/dedup/warning。缺失
    接口数据绝不转换成 pass；没有该类费用且接口已成功返回空列表时才 pass。
    """
    states: dict[str, str] = {}
    city_present = bool(city_orders) or any(
        item.get("localTrafficApplyAmount") not in (None, 0)
        or item.get("trafficAmount") not in (None, 0)
        for item in journeys
    )
    station_present = station_apply not in (None, 0) or bool(
        [item for item in [*flight_orders, *train_orders] if _is_station_time_order(item)]
    )

    if not (_source_ready(source_status, raw_sources, "journeys") and _source_ready(source_status, raw_sources, "cityTransports")):
        states["e38_city_transport_amount"] = "missing"
    elif not city_present:
        states["e38_city_transport_amount"] = "pass"
    elif city_apply is None or city_standard is None or city_invoice is None:
        states["e38_city_transport_amount"] = "missing"
    else:
        states["e38_city_transport_amount"] = (
            "pass"
            if float(city_apply) <= float(city_standard) and float(city_apply) <= float(city_invoice)
            else "reject"
        )

    if not _source_ready(source_status, raw_sources, "journeys"):
        states["e23_role_city_transport"] = "missing"
    elif not city_present or not employee_role:
        states["e23_role_city_transport"] = "missing" if city_present else "pass"
    else:
        states["e23_role_city_transport"] = (
            "reject"
            if any(role in employee_role for role in ("销售", "售前")) and float(city_apply or 0) > 0
            else "pass"
        )

    if not (_source_ready(source_status, raw_sources, "journeys") and _source_ready(source_status, raw_sources, "airTickets") and _source_ready(source_status, raw_sources, "trainTickets")):
        states["e30_station_vehicle"] = "missing"
    elif not station_present:
        states["e30_station_vehicle"] = "pass"
    elif station_apply is None or station_allowed is None:
        states["e30_station_vehicle"] = "missing"
    else:
        states["e30_station_vehicle"] = "pass" if station_apply <= station_allowed else "reject"

    meal_apply, meal_allowed = _business_fee_meal_amounts(business_fee_details, journeys, subsidies)
    if not _source_ready(source_status, raw_sources, "businessFeeDetails"):
        states["e25_meal_meeting_subsidy"] = "missing"
    elif meal_apply is None and meal_allowed is None:
        states["e25_meal_meeting_subsidy"] = "pass"
    elif meal_apply is None or meal_allowed is None:
        states["e25_meal_meeting_subsidy"] = "missing"
    else:
        states["e25_meal_meeting_subsidy"] = "pass" if meal_apply <= meal_allowed else "reject"

    if not _source_ready(source_status, raw_sources, "travelSubsidies"):
        states["e31_subsidy_amount"] = "missing"
    elif not subsidies:
        states["e31_subsidy_amount"] = "pass"
    elif subsidy_apply is None or subsidy_calculated is None:
        states["e31_subsidy_amount"] = "missing"
    else:
        states["e31_subsidy_amount"] = "pass" if _money_equal(subsidy_apply, subsidy_calculated) else "reject"

    if not _source_ready(source_status, raw_sources, "drivingCars"):
        states["self_driving_amount"] = "missing"
    elif not driving:
        states["self_driving_amount"] = "pass"
    elif self_driving_theory is None:
        states["self_driving_amount"] = "missing"
    else:
        # 发票金额在 invoice enricher 中补入；仅有申请金额不能冒充有效票据金额。
        states["self_driving_amount"] = "missing"

    # The graph uses stable source-row aliases (r02..r37) so repeated reason
    # codes remain independent. Keep descriptive names for old fixtures.
    _copy_travel_rule_state_aliases(states)
    return states


def _copy_travel_rule_state_aliases(states: dict[str, str]) -> None:
    aliases = {
        "r02": "e38_city_transport_amount",
        "r03": "e42_taxi_serial",
        "r04": "e23_role_city_transport",
        "r06": "e30_station_vehicle",
        "r07": "e25_meal_meeting_subsidy",
        "r08": "e31_subsidy_amount",
        "r10": "self_driving_amount",
        "r18": "travel_monthly_train",
        "r27": "e17_recharge_card",
        "r28": "sys001_authenticity",
        "r29": "e09_saler_blacklist",
        "r30": "sys003_void",
        "r31": "sys004_red_flush",
        "r32": "e05_duplicate",
        "r33": "w39_travel_scene",
        "r34": "e01",
        "r35": "e02",
        "r36": "e33_year",
        "r37": "travel_tax_amount",
    }
    for alias, source in aliases.items():
        if source in states:
            states[alias] = states[source]


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
        "sourceAvailability": {},
        "sourceReady": False,
        "primaryInvoice": True,
        "raisedRuleCodes": [],
        "raisedRuleKeys": [],
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
    self_driving_apply = _sum_numbers(item.get("amount") for item in driving)
    self_driving_theory = _sum_numbers(
        (_number(item.get("miles")) or 0)
        * (_number(item.get("standard")) or 0)
        * (_number(item.get("rate")) or 1)
        for item in driving
    )
    meal_apply, meal_allowed = _business_fee_meal_amounts(
        business_fee_details, journeys, subsidies
    )

    source_status = dict(raw.get("sourceStatus") or {})
    source_availability = {
        key: _source_ready(source_status, raw, key)
        for key in (
            "businessFeeDetails", "journeys", "airTickets", "trainTickets",
            "hotels", "cityTransports", "drivingCars", "travelSubsidies",
            "otherTransports", "otherExpenses",
        )
    }
    rule_states = _build_receipt_rule_states(
        journeys=journeys,
        flight_orders=flight_orders,
        train_orders=train_orders,
        city_orders=city_orders,
        driving=driving,
        subsidies=subsidies,
        business_fee_details=business_fee_details,
        source_status=source_status,
        employee_role=post_type or "",
        city_apply=city_apply,
        city_standard=city_standard,
        city_invoice=city_invoice,
        station_apply=station_apply,
        station_allowed=station_allowed,
        subsidy_apply=subsidy_apply,
        subsidy_calculated=subsidy_calculated,
        self_driving_theory=self_driving_theory,
        raw_sources=raw,
    )

    expense_items: list[dict[str, Any]] = []
    expense_items.extend({**item, "expenseType": "flight"} for item in flight_orders)
    expense_items.extend({**item, "expenseType": "train"} for item in train_orders)
    expense_items.extend({**item, "expenseType": "hotel"} for item in hotel_orders)
    expense_items.extend({**item, "expenseType": "city_transport"} for item in city_orders)
    expense_items.extend({**item, "expenseType": "other_transport"} for item in other_transport_orders)
    expense_items.extend({**item, "expenseType": "self_driving"} for item in driving)
    expense_items.extend({**item, "expenseType": "subsidy"} for item in subsidies)
    expense_items.extend({**item, "expenseType": "travel_other"} for item in other_expenses)

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
        "selfDrivingApplyAmount": self_driving_apply,
        "selfDrivingTheoryAmount": self_driving_theory,
        # drivingCars.amount is the application/theoretical amount, not an
        # invoice total.  A valid invoice amount is populated only after OCR.
        "selfDrivingInvoiceAmount": None,
        "subsidyInfo": {
            "items": subsidies,
            "businessFeeDetails": business_fee_details,
            "applyAmount": subsidy_apply,
            "calculatedAmount": subsidy_calculated,
            "mealMeetingApplyAmount": meal_apply,
            "mealMeetingAllowedAmount": meal_allowed,
        },
        "baggageInfo": [item for item in other_expenses if item.get("typeCode") == "2001"],
        "taxInfo": {},
        "invoiceScene": None,
        "selfBoughtMonthlyTrain": None,
        "ruleStates": rule_states,
        "sourceStatus": source_status,
        "sourceAvailability": source_availability,
        "sourceReady": bool(source_availability) and all(source_availability.values()),
        "primaryInvoice": True,
        "raisedRuleCodes": [],
        "raisedRuleKeys": [],
        "messages": list(dict.fromkeys(messages)) or [],
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


def _ocr_text(ocr_data: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "goodsName", "summary", "remark", "invoiceType", "invoiceTypeName",
        "invoiceTypeCode", "invoiceKind", "flightNo", "flightno", "trainNo",
        "trainno", "shift", "airlineName", "airlineCode",
    ):
        value = ocr_data.get(key)
        if value is not None:
            values.append(str(value))
    for key in ("items", "invoiceDetails", "details"):
        values.extend(
            str(item.get("goodsName") or item.get("name") or item.get("summary") or "")
            for item in (ocr_data.get(key) or [])
            if isinstance(item, Mapping)
        )
    return " ".join(values).strip()


def _invoice_scene(ocr_data: Mapping[str, Any]) -> tuple[str, str | None]:
    return _classify_travel_scene(_ocr_text(ocr_data))


def _invoice_type_has(ocr_data: Mapping[str, Any], *codes: str) -> bool:
    text = " ".join(
        str(ocr_data.get(key) or "")
        for key in ("invoiceType", "invoiceTypeCode", "invoiceTypeName", "invoiceKind")
    )
    normalized = _normalized_text(text)
    return any(_normalized_text(code) in normalized for code in codes)


def _is_city_transport_invoice(ocr_data: Mapping[str, Any]) -> bool:
    state, scene = _invoice_scene(ocr_data)
    return scene == "city_transport" or _invoice_type_has(ocr_data, "1-009", "1-010", "1-013", "过路", "定额", "的士")


def _is_self_driving_invoice(ocr_data: Mapping[str, Any], travel_audit: Mapping[str, Any]) -> bool:
    state, scene = _invoice_scene(ocr_data)
    del state
    return scene == "self_driving" and bool(travel_audit.get("selfDrivingMileage"))


def _is_other_transport_invoice(ocr_data: Mapping[str, Any]) -> bool:
    state, scene = _invoice_scene(ocr_data)
    del state
    return scene == "other_transport"


def _is_baggage_invoice(ocr_data: Mapping[str, Any]) -> bool:
    state, scene = _invoice_scene(ocr_data)
    del state
    return scene == "baggage" or "运输服务*行李" in _ocr_text(ocr_data)


def _is_relevant_other_expense(ocr_data: Mapping[str, Any], type_code: str) -> bool:
    state, scene = _invoice_scene(ocr_data)
    del state
    return scene == TRAVEL_OTHER_EXPENSE_TYPES.get(type_code)


def _travel_source_ready(travel_audit: Mapping[str, Any], key: str, field: str) -> bool:
    availability = travel_audit.get("sourceAvailability")
    if isinstance(availability, Mapping) and key in availability:
        return bool(availability.get(key))
    source_status = travel_audit.get("sourceStatus")
    if isinstance(source_status, Mapping) and key in source_status:
        status = source_status.get(key)
        return isinstance(status, Mapping) and str(status.get("status") or "").upper() == "READY"
    # Direct unit/integration callers may provide normalized lists without the
    # optional source metadata.  Presence of the normalized field is then the
    # strongest available signal.
    return field in travel_audit


def _travel_window(travel_audit: Mapping[str, Any]) -> tuple[str | None, str | None]:
    starts: list[str] = []
    ends: list[str] = []
    for journey in travel_audit.get("travelSegments") or []:
        if not isinstance(journey, Mapping):
            continue
        start = _date_key(journey.get("departureTime"))
        end = _date_key(journey.get("arrivalTime")) or start
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    start = min(starts) if starts else _date_key(travel_audit.get("travelStartDate"))
    end = max(ends) if ends else _date_key(travel_audit.get("travelEndDate"))
    return start, end


def _date_in_travel_window(invoice_date: Any, travel_audit: Mapping[str, Any]) -> bool | None:
    day = _date_key(invoice_date)
    start, end = _travel_window(travel_audit)
    if not day or not start or not end:
        return None
    return start <= day <= end


def _matched_orders(
    ocr_data: Mapping[str, Any],
    orders: Sequence[Mapping[str, Any]],
    *,
    kind: str,
) -> list[dict[str, Any]]:
    return _match_orders(ocr_data, orders, kind=kind)


def _invoice_date_state(
    ocr_data: Mapping[str, Any],
    travel_audit: Mapping[str, Any],
    orders: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    source_key: str,
    source_field: str,
) -> str:
    invoice_date = _invoice_date(ocr_data)
    if not invoice_date:
        return "missing"
    if not _travel_source_ready(travel_audit, "journeys", "travelSegments") or not _travel_source_ready(
        travel_audit, source_key, source_field
    ):
        return "missing"
    matches = _matched_orders(ocr_data, orders, kind=kind)
    if matches:
        order_dates = {_date_key(item.get("date") or item.get("departureTime")) for item in matches}
        order_dates.discard(None)
        if order_dates and invoice_date not in order_dates:
            return "reject"
    in_window = _date_in_travel_window(invoice_date, travel_audit)
    if in_window is False:
        return "reject"
    if in_window is None and not matches:
        return "missing"
    return "pass"


def _invoice_person_state(
    ocr_data: Mapping[str, Any],
    travel_audit: Mapping[str, Any],
    orders: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    source_key: str,
    source_field: str,
) -> str:
    passenger_id = _value(ocr_data, "passengerId", "userId", "userid", "travellerId")
    passenger_name = _value(ocr_data, "passengerName", "buyerName", "travellerName", "userName")
    if not passenger_id and not passenger_name:
        return "missing"
    if not _travel_source_ready(travel_audit, source_key, source_field):
        return "missing"
    matches = _matched_orders(ocr_data, orders, kind=kind)
    if not matches:
        return "missing"
    comparable = [
        _same_person(passenger_id, passenger_name, item.get("passengerId"), item.get("passengerName"))
        for item in matches
    ]
    if any(value is True for value in comparable):
        return "pass"
    if any(value is None for value in comparable):
        return "missing"
    return "reject"


def _invoice_order_amount_state(
    ocr_data: Mapping[str, Any],
    travel_audit: Mapping[str, Any],
    orders: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    source_key: str,
    source_field: str,
) -> str:
    invoice_amount = _invoice_amount(ocr_data)
    if invoice_amount is None:
        return "missing"
    if not _travel_source_ready(travel_audit, source_key, source_field):
        return "missing"
    matches = _matched_orders(ocr_data, orders, kind=kind)
    if not matches:
        return "missing"
    allowed = _sum_numbers(item.get("amount") for item in matches)
    if allowed is None:
        return "missing"
    return "pass" if float(invoice_amount) <= float(allowed) + 0.01 else "reject"


def _invoice_other_expense_state(
    ocr_data: Mapping[str, Any],
    travel_audit: Mapping[str, Any],
    *,
    type_code: str,
) -> str:
    if not _is_relevant_other_expense(ocr_data, type_code):
        return "pass"
    invoice_amount = _invoice_amount(ocr_data)
    if invoice_amount is None:
        return "missing"
    if not _travel_source_ready(travel_audit, "otherExpenses", "baggageInfo"):
        return "missing"
    records = [
        item for item in (travel_audit.get("baggageInfo") or [])
        if str(item.get("typeCode") or "") == type_code
    ]
    if type_code != "2001":
        records = [
            item for item in (travel_audit.get("raw", {}).get("otherExpenses") or [])
            if str(_value(item, "typecode", "typeCode") or "") == type_code
        ] or records
    allowed = _sum_numbers(item.get("amount") for item in records)
    return "missing" if allowed is None else ("pass" if invoice_amount <= allowed + 0.01 else "reject")


def _resolve_monthly_train_state(ocr_data: Mapping[str, Any], train_orders: Sequence[Mapping[str, Any]]) -> bool | None:
    if not _is_rail_invoice(ocr_data):
        return False
    candidates = _match_orders(ocr_data, train_orders, kind="train")
    if not candidates:
        return None
    return any(str(item.get("source") or "").lower() == "monthly" for item in candidates)


def _explicit_invoice_state(ocr: Mapping[str, Any], *keys: str) -> str:
    """Map common OCR status fields to pass/reject/missing."""
    values = [_value(ocr, key) for key in keys]
    present = [value for value in values if value is not None and value != ""]
    if not present:
        return "missing"
    text = " ".join(str(value) for value in present).casefold()
    if any(token in text for token in ("作废", "void", "红冲", "red", "invalid")):
        return "reject"
    if any(value is True or str(value).casefold() in {"true", "1", "yes", "正常", "有效", "valid"} for value in present):
        return "pass"
    if any(value is False or str(value).casefold() in {"false", "0", "no", "否", "正常"} for value in present):
        return "pass"
    return "missing"


def _match_value_state(value: Any) -> str:
    """将公共字段匹配结果归一化为流程图状态。"""
    if value is None or value == "":
        return "missing"
    if isinstance(value, bool):
        return "pass" if value else "reject"
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "pass", "通过", "一致", "正常", "valid"}:
        return "pass"
    if text in {"false", "0", "no", "reject", "不一致", "异常", "invalid"}:
        return "reject"
    return "missing"


def _company_name_state(ocr: Mapping[str, Any], service_data: Mapping[str, Any]) -> str:
    explicit = _value(ocr, "isInvoiceHeaderMatch")
    if explicit is None:
        explicit = _value(service_data, "invoiceHeaderMatch")
    if explicit is not None:
        return _match_value_state(explicit)

    buyer = _text(ocr, "buyerName", "purchaserName", "purchaser", "orgName")
    audit_info = service_data.get("auditInfo")
    audit_info = audit_info if isinstance(audit_info, Mapping) else {}
    expected = _text(
        audit_info,
        "verifiUserCompanyName",
        "companyName",
        "companyFullName",
        "buyerName",
    )
    if not buyer or not expected:
        return "missing"
    return "pass" if _normalized_text(buyer) == _normalized_text(expected) else "reject"


def _company_tax_state(ocr: Mapping[str, Any], service_data: Mapping[str, Any]) -> str:
    explicit = _value(ocr, "isInvoiceTaxNumberMatch")
    if explicit is None:
        explicit = _value(service_data, "invoiceTaxNumberMatch")
    if explicit is not None:
        return _match_value_state(explicit)

    buyer_name = _text(ocr, "buyerName", "purchaserName", "purchaser", "orgName") or ""
    # 个人抬头通常没有企业税号，不触发 E02；有企业后缀但缺税号时转人工。
    company_markers = ("公司", "集团", "股份", "有限", "银行", "大学", "医院", "中心")
    if not any(marker in buyer_name for marker in company_markers):
        return "pass" if buyer_name else "missing"
    buyer_tax = _text(ocr, "buyerTaxNo", "buyerTaxNO", "purchaserTaxNo", "taxNo")
    audit_info = service_data.get("auditInfo")
    audit_info = audit_info if isinstance(audit_info, Mapping) else {}
    expected_tax = _text(audit_info, "companyTax", "taxNo", "companyTaxNo")
    if not buyer_tax or not expected_tax:
        return "missing"
    return "pass" if _normalized_text(buyer_tax) == _normalized_text(expected_tax) else "reject"


def _company_blacklist_state(ocr: Mapping[str, Any], service_data: Mapping[str, Any]) -> str:
    seller = _text(ocr, "salerName", "sellerName")
    blacklist = service_data.get("companyBlacklist")
    if not seller or not isinstance(blacklist, Sequence) or isinstance(blacklist, (str, bytes, bytearray)):
        return "missing"
    normalized_seller = _normalized_text(seller)
    for item in blacklist:
        if isinstance(item, Mapping):
            value = _value(item, "value", "name", "companyName", "salerName")
        else:
            value = item
        if normalized_seller == _normalized_text(value):
            return "reject"
    return "pass"


def _duplicate_invoice_state(ocr: Mapping[str, Any], service_data: Mapping[str, Any]) -> str:
    invoice_no = _text(ocr, "invoiceNo", "chequeNo", "serialNo")
    if not invoice_no:
        return "missing"
    history = service_data.get("invoiceUsageHistory")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes, bytearray)):
        return "missing"
    normalized = _normalized_text(invoice_no)
    historical_hit = False
    for item in history:
        if not isinstance(item, Mapping):
            continue
        historical = _text(item, "chequeNo", "invoiceNo", "serialNo")
        if historical and _normalized_text(historical) == normalized:
            historical_hit = True
            break
    receipt_hit = bool(service_data.get("receiptInvoiceDuplicate"))
    return "reject" if historical_hit or receipt_hit else "pass"


def _year_state(ocr: Mapping[str, Any], service_data: Mapping[str, Any]) -> str:
    invoice_date = _date_key(_value(ocr, "invoiceDate", "date", "travelDate"))
    audit_info = service_data.get("auditInfo")
    audit_info = audit_info if isinstance(audit_info, Mapping) else {}
    submit_date = _date_key(_value(audit_info, "submitTime", "submitDate", "createTime"))
    if not invoice_date or not submit_date:
        return "missing"
    return "pass" if invoice_date[:4] == submit_date[:4] else "reject"


def _taxi_serial_state(service_data: Mapping[str, Any]) -> str:
    serial = service_data.get("taxiInvoiceSerial")
    if not isinstance(serial, Mapping):
        return "missing"
    if not serial.get("isTaxiInvoice"):
        return "pass"
    if serial.get("historyHit") or serial.get("batchHit"):
        return "reject"
    return "warning" if serial.get("lookupFailed") else "pass"


def _invoice_date_only_state(ocr: Mapping[str, Any], travel_audit: Mapping[str, Any], source_key: str) -> str:
    invoice_date = _invoice_date(ocr)
    if not invoice_date:
        return "missing"
    if not _travel_source_ready(travel_audit, "journeys", "travelSegments") or not _travel_source_ready(
        travel_audit, source_key, "travelSegments"
    ):
        return "missing"
    in_window = _date_in_travel_window(invoice_date, travel_audit)
    return "missing" if in_window is None else ("pass" if in_window else "reject")


def _baggage_airline_state(ocr: Mapping[str, Any], travel_audit: Mapping[str, Any]) -> str:
    if not _is_baggage_invoice(ocr):
        return "pass"
    if not _travel_source_ready(travel_audit, "airTickets", "flightOrders"):
        return "missing"
    airline = _normalized_text(_value(ocr, "airlineName", "airlineCode", "carrier", "carrierName"))
    if not airline:
        return "missing"
    matches = _matched_orders(ocr, travel_audit.get("flightOrders") or [], kind="flight")
    if not matches:
        return "missing"
    order_airlines = {
        _normalized_text(item.get("airlineName") or item.get("airlineCode"))
        for item in matches
    }
    order_airlines.discard("")
    if not order_airlines:
        return "missing"
    return "pass" if airline in order_airlines else "warning"


def _baggage_weight_state(ocr: Mapping[str, Any], travel_audit: Mapping[str, Any]) -> str:
    if not _is_baggage_invoice(ocr):
        return "pass"
    weight = _number(_value(ocr, "weight", "baggageWeight", "baggageweight"))
    if weight is None:
        return "missing"
    if not _travel_source_ready(travel_audit, "airTickets", "flightOrders"):
        return "missing"
    matches = _matched_orders(ocr, travel_audit.get("flightOrders") or [], kind="flight")
    if not matches:
        return "missing"
    allowed = _sum_numbers(
        _value(
            item.get("raw"),
            "baggageWeight", "freeBaggageWeight", "freeBaggage", "allowance", "weight",
        )
        for item in matches
    )
    if allowed is None:
        return "missing"
    return "pass" if float(weight) <= float(allowed) + 0.01 else "warning"


def travel_invoice_enricher(
    receipt_code: str,
    file_path: str,
    ocr_data: dict[str, Any],
    service_data: dict[str, Any],
) -> dict[str, Any]:
    """将当前发票 OCR 与差旅接口结果合并，并生成发票级规则状态。"""
    del receipt_code, file_path
    existing = service_data.get("travelAudit")
    travel_audit = dict(existing) if isinstance(existing, Mapping) else _empty_travel_audit()
    ocr = ocr_data if isinstance(ocr_data, Mapping) else {}
    states = dict(travel_audit.get("ruleStates") or {})

    scene_state, scene = _invoice_scene(ocr)
    states["w39_travel_scene"] = scene_state
    # 旧字段保留三态兼容：明确允许场景为 True，明确禁止场景为 False，
    # 未知/缺失场景不把 warning 误写成 False，避免调用方把“未知”当作已确认的
    # 禁止场景；真正的 W39 状态由 ruleStates.w39_travel_scene 承载。
    invoice_scene: bool | None = (
        True if scene_state == "pass" and scene not in {"forbidden", "unknown"}
        else False if scene_state == "warning" and scene == "forbidden"
        else None
    )

    city_invoice = _is_city_transport_invoice(ocr)
    other_transport_invoice = _is_other_transport_invoice(ocr)
    train_invoice = _is_rail_invoice(ocr) or scene == "train"
    flight_invoice = _is_flight_invoice(ocr) or scene == "flight"
    self_driving_invoice = _is_self_driving_invoice(ocr, travel_audit)
    baggage_invoice = _is_baggage_invoice(ocr)

    states["e20_city_transport_date"] = (
        _invoice_date_only_state(ocr, travel_audit, "cityTransports") if city_invoice else "pass"
    )
    states["e20_self_driving_date"] = (
        _invoice_date_only_state(ocr, travel_audit, "drivingCars") if self_driving_invoice else "pass"
    )
    states["e29_other_transport_passenger"] = (
        _invoice_person_state(
            ocr, travel_audit, travel_audit.get("transportOrders") or [],
            kind="other_transport", source_key="otherTransports", source_field="transportOrders",
        ) if other_transport_invoice else "pass"
    )
    states["e20_other_transport_date"] = (
        _invoice_date_state(
            ocr, travel_audit, travel_audit.get("transportOrders") or [],
            kind="other_transport", source_key="otherTransports", source_field="transportOrders",
        ) if other_transport_invoice else "pass"
    )
    states["e31_other_transport_amount"] = (
        _invoice_order_amount_state(
            ocr, travel_audit, travel_audit.get("transportOrders") or [],
            kind="other_transport", source_key="otherTransports", source_field="transportOrders",
        ) if other_transport_invoice else "pass"
    )
    states["e29_train_passenger"] = (
        _invoice_person_state(
            ocr, travel_audit, travel_audit.get("trainOrders") or [],
            kind="train", source_key="trainTickets", source_field="trainOrders",
        ) if train_invoice else "pass"
    )
    states["e20_train_date"] = (
        _invoice_date_state(
            ocr, travel_audit, travel_audit.get("trainOrders") or [],
            kind="train", source_key="trainTickets", source_field="trainOrders",
        ) if train_invoice else "pass"
    )
    if train_invoice:
        matches = _matched_orders(ocr, travel_audit.get("trainOrders") or [], kind="train")
        if not _travel_source_ready(travel_audit, "trainTickets", "trainOrders"):
            states["e32_train_seat"] = "missing"
        elif not matches:
            states["e32_train_seat"] = "missing"
        else:
            seat_states = [_seat_state(item.get("seat")) for item in matches]
            states["e32_train_seat"] = (
                "reject" if "reject" in seat_states else "pass" if "pass" in seat_states else "missing"
            )
    else:
        states["e32_train_seat"] = "pass"
    states["e31_train_amount"] = (
        _invoice_order_amount_state(
            ocr, travel_audit, travel_audit.get("trainOrders") or [],
            kind="train", source_key="trainTickets", source_field="trainOrders",
        ) if train_invoice else "pass"
    )
    states["e20_flight_date"] = (
        _invoice_date_state(
            ocr, travel_audit, travel_audit.get("flightOrders") or [],
            kind="flight", source_key="airTickets", source_field="flightOrders",
        ) if flight_invoice else "pass"
    )
    states["e31_vaccine_amount"] = _invoice_other_expense_state(ocr, travel_audit, type_code="2008")
    states["e31_network_card_amount"] = _invoice_other_expense_state(ocr, travel_audit, type_code="2007")
    states["e31_refund_change_amount"] = _invoice_other_expense_state(ocr, travel_audit, type_code="2004")
    states["w37_baggage_airline"] = _baggage_airline_state(ocr, travel_audit)
    states["w35_baggage_date"] = (
        _invoice_date_state(
            ocr, travel_audit, travel_audit.get("flightOrders") or [],
            kind="flight", source_key="airTickets", source_field="flightOrders",
        ) if baggage_invoice else "pass"
    )
    states["w38_baggage_weight"] = _baggage_weight_state(ocr, travel_audit)
    states["e31_baggage_amount"] = (
        _invoice_other_expense_state(ocr, travel_audit, type_code="2001") if baggage_invoice else "pass"
    )

    goods_text = _ocr_text(ocr)
    if not goods_text:
        states["e17_recharge_card"] = "missing"
    else:
        normalized_goods = _normalized_text(goods_text)
        states["e17_recharge_card"] = (
            "reject" if any(token in normalized_goods for token in ("充值卡", "预付卡", "储值卡")) else "pass"
        )

    verify_result = _value(ocr, "verifyResult", "verificationResult", "invoiceVerifyResult")
    states["sys001_authenticity"] = "missing" if verify_result is None else ("pass" if not verify_result else "reject")
    states["sys003_void"] = _explicit_invoice_state(
        ocr, "voidStatus", "isVoided", "invoiceStatus", "status", "voided"
    )
    states["sys004_red_flush"] = _explicit_invoice_state(
        ocr, "redFlushStatus", "isRedFlushed", "red冲", "redFlush", "invoiceStatus", "status"
    )
    states["e09_saler_blacklist"] = _company_blacklist_state(ocr, service_data)
    states["e05_duplicate"] = _duplicate_invoice_state(ocr, service_data)
    states["e01"] = _company_name_state(ocr, service_data)
    states["e02"] = _company_tax_state(ocr, service_data)
    states["e33_year"] = _year_state(ocr, service_data)
    states["e42_taxi_serial"] = _taxi_serial_state(service_data)

    tax_info = dict(travel_audit.get("taxInfo") or {})
    invoice_tax = _number(_value(ocr, "effectiveTaxAmount", "deductibleTaxAmount", "totalTaxAmount", "taxAmount"))
    if invoice_tax is not None:
        tax_info["invoiceDeductibleTax"] = invoice_tax
    form_tax = _number(_value(service_data, "formInputTax", "inputTaxAmount", "formTaxAmount"))
    if form_tax is None:
        audit_info = service_data.get("auditInfo")
        if isinstance(audit_info, Mapping):
            form_tax = _number(_value(audit_info, "formInputTax", "inputTaxAmount", "formTaxAmount"))
    if form_tax is not None:
        tax_info["formInputTax"] = form_tax
    invoice_tax_value = tax_info.get("invoiceDeductibleTax")
    form_tax_value = tax_info.get("formInputTax")
    states["travel_tax_amount"] = (
        "missing" if invoice_tax_value is None or form_tax_value is None
        else "pass" if _money_equal(invoice_tax_value, form_tax_value)
        else "warning"
    )

    monthly_train = _resolve_monthly_train_state(ocr, travel_audit.get("trainOrders") or [])
    travel_audit["selfBoughtMonthlyTrain"] = monthly_train
    states["travel_monthly_train"] = (
        "missing" if monthly_train is None else "reject" if monthly_train else "pass"
    )

    if travel_audit.get("selfDrivingMileage"):
        if not self_driving_invoice:
            states.setdefault("self_driving_amount", "missing")
        else:
            apply_amount = _number(travel_audit.get("selfDrivingApplyAmount"))
            theory_amount = _number(travel_audit.get("selfDrivingTheoryAmount"))
            invoice_amount = _invoice_amount(ocr)
            travel_audit["selfDrivingInvoiceAmount"] = invoice_amount
            states["self_driving_amount"] = (
                "missing" if apply_amount is None or theory_amount is None or invoice_amount is None
                else "reject_theory" if apply_amount > theory_amount
                else "reject_invoice" if apply_amount > invoice_amount
                else "pass"
            )

    # Stable source-row state aliases are the graph/application seam. They are
    # copied after all OCR-derived descriptive states have been calculated.
    _copy_travel_rule_state_aliases(states)
    states["r05"] = states.get("e20_city_transport_date", "missing")
    states["r09"] = states.get("e20_self_driving_date", "missing")
    states["r11"] = states.get("e29_other_transport_passenger", "missing")
    states["r12"] = states.get("e20_other_transport_date", "missing")
    states["r13"] = states.get("e31_other_transport_amount", "missing")
    states["r14"] = states.get("e29_train_passenger", "missing")
    states["r15"] = states.get("e20_train_date", "missing")
    states["r16"] = states.get("e32_train_seat", "missing")
    states["r17"] = states.get("e31_train_amount", "missing")
    states["r19"] = states.get("e20_flight_date", "missing")
    states["r20"] = states.get("e31_vaccine_amount", "missing")
    states["r21"] = states.get("e31_network_card_amount", "missing")
    states["r22"] = states.get("e31_refund_change_amount", "missing")
    states["r23"] = states.get("w37_baggage_airline", "missing")
    states["r24"] = states.get("w35_baggage_date", "missing")
    states["r25"] = states.get("w38_baggage_weight", "missing")
    states["r26"] = states.get("e31_baggage_amount", "missing")
    states["r33"] = states.get("w39_travel_scene", "missing")
    states["r34"] = states.get("e01", "missing")
    states["r35"] = states.get("e02", "missing")
    states["r36"] = states.get("e33_year", "missing")
    states["r37"] = states.get("travel_tax_amount", "missing")

    invoice_snapshot = {
        "invoiceNo": _value(ocr, "invoiceNo", "chequeNo", "serialNo"),
        "invoiceDate": _value(ocr, "invoiceDate", "date", "travelDate"),
        "invoiceAmount": _invoice_amount(ocr),
        "invoiceTax": invoice_tax,
        "passengerId": _value(ocr, "passengerId", "userId", "userid", "travellerId"),
        "passengerName": _value(ocr, "passengerName", "buyerName", "travellerName"),
        "flightNo": _value(ocr, "flightNo", "flightno"),
        "trainNo": _value(ocr, "trainNo", "trainno", "shift"),
        "goodsName": _value(ocr, "goodsName"),
        "invoiceType": _value(ocr, "invoiceType", "invoiceTypeCode", "invoiceTypeName"),
    }
    return {
        **travel_audit,
        "ruleStates": states,
        "taxInfo": tax_info,
        "invoiceScene": invoice_scene,
        "currentInvoice": invoice_snapshot,
        "primaryInvoice": travel_audit.get("primaryInvoice", True),
        "raisedRuleCodes": list(travel_audit.get("raisedRuleCodes") or []),
        "raisedRuleKeys": list(travel_audit.get("raisedRuleKeys") or []),
    }


__all__ = [
    "NOT_READY_MESSAGE",
    "build_travel_receipt_enricher",
    "normalize_travel_data",
    "travel_invoice_enricher",
    "travel_receipt_enricher",
]
