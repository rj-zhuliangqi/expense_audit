from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Travel writeback strategy — placeholder.
# Filled in when 差旅 compliance rules and auditTravels builders are finalized.


def travel_compliance_rule(goods_name: str, item: Mapping[str, Any]) -> bool:
    raise NotImplementedError(
        "travel_compliance_rule is not implemented; define 差旅 compliance rules before enabling the travel profile"
    )


def travel_audit_travels_builder(
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    service_data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raise NotImplementedError(
        "travel_audit_travels_builder is not implemented; "
        "wire 差旅 itinerary→auditTravels mapping before enabling the travel profile"
    )


__all__ = ["travel_audit_travels_builder", "travel_compliance_rule"]
