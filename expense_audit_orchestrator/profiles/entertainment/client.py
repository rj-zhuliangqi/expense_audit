"""业务招待费专属接口客户端。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from expense_audit_orchestrator import audit_client


ENTERTAINMENT_API_PREFIX = "/api/audit-service/audit"
BUSINESS_FEE_DETAILS_ENDPOINT = "bussness-fee-details"
RequestList = Callable[[str, str, float | None], list[dict[str, Any]]]


def _default_service_url() -> str:
    return (
        os.getenv("AUDIT_SERVICE_URL") or audit_client.DEFAULT_AUDIT_SERVICE_URL
    ).strip() or audit_client.DEFAULT_AUDIT_SERVICE_URL


@dataclass
class EntertainmentApiClient:
    """业务招待费所需的核销单业务费用明细客户端。"""

    service_url: str | None = None
    timeout: float | None = None
    request_list: RequestList | None = None

    def fetch_business_fee_details(self, instance_code: str) -> list[dict[str, Any]]:
        path = (
            f"{ENTERTAINMENT_API_PREFIX}/{BUSINESS_FEE_DETAILS_ENDPOINT}/"
            f"{quote(instance_code, safe='')}"
        )
        description = "业务招待费业务费用明细"
        if self.request_list is not None:
            return self.request_list(path, description, self.timeout)

        data = audit_client._fetch_service_data(  # noqa: SLF001
            path,
            service_url=self.service_url or _default_service_url(),
            timeout=self.timeout,
            description=description,
            headers=audit_client._build_auth_headers(),  # noqa: SLF001
        )
        return audit_client._expect_list_or_single_mapping_payload(  # noqa: SLF001
            data, description
        )
