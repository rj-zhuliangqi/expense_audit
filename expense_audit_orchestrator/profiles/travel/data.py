from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Travel (差旅) profile data enricher — placeholder.
# 差旅与个人交通费是独立费用类型：差旅需行程预订/里程标准等业务数据。
# Filled in when 差旅 services are wired: fetch itinerary by instanceCode and
# load mileage-standard offline asset, returning keys merged into serviceData.


def travel_receipt_enricher(receipt_code: str, service_data: Mapping[str, Any]) -> dict[str, Any]:
    raise NotImplementedError(
        "travel_receipt_enricher is not implemented; "
        "wire 差旅 itinerary fetch and mileage-standard asset before enabling the travel profile"
    )


__all__ = ["travel_receipt_enricher"]
