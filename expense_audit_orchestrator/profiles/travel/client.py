"""差旅费专属服务接口客户端。

差旅接口只在 travel profile 内聚合，通用 ``audit_client`` 仍只维护通用发票/核销接口。
底层 HTTP、认证、超时和重试复用 audit_client 的已有实现，避免复制一套传输逻辑。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from expense_audit_orchestrator import audit_client
from expense_audit_orchestrator.observability import get_logger


_logger = get_logger("travel_client")

TRAVEL_API_PREFIX = "/api/audit-service/audit"

_ENDPOINTS: dict[str, str] = {
    "businessFeeDetails": "bussness-fee-details",
    "journeys": "travel-journey-manage",
    "airTickets": "air-tickets",
    "trainTickets": "train-tickets",
    "hotels": "hotels",
    "cityTransports": "city-transports",
    "drivingCars": "driving-cars",
    "travelSubsidies": "travel-subsidies",
    "otherTransports": "other-transports",
    "otherExpenses": "travel-other-expenses",
}

RequestList = Callable[[str, str, float | None], list[dict[str, Any]]]


def _default_service_url() -> str:
    # 允许独立 worker 通过 AUDIT_SERVICE_URL 配置差旅接口；显式构造参数优先。
    return (os.getenv("AUDIT_SERVICE_URL") or audit_client.DEFAULT_AUDIT_SERVICE_URL).strip() or audit_client.DEFAULT_AUDIT_SERVICE_URL


@dataclass
class TravelApiClient:
    """差旅接口客户端。

    ``request_list`` 仅用于测试注入；生产环境使用现有 audit_client 的统一 HTTP 实现。
    """

    service_url: str | None = None
    timeout: float | None = None
    request_list: RequestList | None = None

    def _request(self, key: str, instance_code: str) -> list[dict[str, Any]]:
        endpoint_name = _ENDPOINTS[key]
        path = f"{TRAVEL_API_PREFIX}/{endpoint_name}/{quote(instance_code, safe='')}"
        description = f"差旅{key}"
        if self.request_list is not None:
            return self.request_list(path, description, self.timeout)

        data = audit_client._fetch_service_data(  # noqa: SLF001
            path,
            service_url=self.service_url or _default_service_url(),
            timeout=self.timeout,
            description=description,
            headers=audit_client._build_auth_headers(),  # noqa: SLF001
        )
        return audit_client._expect_list_or_single_mapping_payload(data, description)  # noqa: SLF001

    def fetch_business_fee_details(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("businessFeeDetails", instance_code)

    def fetch_journeys(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("journeys", instance_code)

    def fetch_air_tickets(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("airTickets", instance_code)

    def fetch_train_tickets(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("trainTickets", instance_code)

    def fetch_hotels(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("hotels", instance_code)

    def fetch_city_transports(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("cityTransports", instance_code)

    def fetch_driving_cars(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("drivingCars", instance_code)

    def fetch_travel_subsidies(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("travelSubsidies", instance_code)

    def fetch_other_transports(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("otherTransports", instance_code)

    def fetch_travel_other_expenses(self, instance_code: str) -> list[dict[str, Any]]:
        return self._request("otherExpenses", instance_code)

    def fetch_other_expenses(self, instance_code: str) -> list[dict[str, Any]]:
        """兼容接口能力方案中的简短方法名。"""
        return self.fetch_travel_other_expenses(instance_code)

    def fetch_all(self, instance_code: str) -> dict[str, Any]:
        """获取全部差旅数据，单个接口失败时只降级对应数据。"""
        result: dict[str, Any] = {key: [] for key in _ENDPOINTS}
        source_status: dict[str, dict[str, str]] = {}
        for key in _ENDPOINTS:
            endpoint = f"{TRAVEL_API_PREFIX}/{_ENDPOINTS[key]}/{{instanceCode}}"
            try:
                result[key] = self._request(key, instance_code)
                source_status[key] = {
                    "status": "READY",
                    "endpoint": endpoint,
                    "message": "",
                }
            except Exception as exc:  # 外部接口按字段组降级，不阻断整单准备
                result[key] = []
                source_status[key] = {
                    "status": "NOT_READY",
                    "endpoint": endpoint,
                    "message": f"接口调用失败，当前规则按通过处理：{exc}",
                }
                _logger.warning(
                    "差旅接口调用失败，降级为空列表",
                    extra={
                        "event": "travel_client.fallback",
                        "instance_code": instance_code,
                        "source_key": key,
                        "error": str(exc),
                    },
                )
        result["sourceStatus"] = source_status
        return result


__all__ = ["TRAVEL_API_PREFIX", "TravelApiClient"]
