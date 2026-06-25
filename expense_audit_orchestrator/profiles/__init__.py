from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..runtime_client import DEFAULT_GRAPH_PATH
from ..writeback_client import AUDIT_INFO_SAVE_PATH


ReceiptEnricher = Callable[[str, Mapping[str, Any]], Any]
InvoiceEnricher = Callable[[str, str, dict[str, Any], dict[str, Any]], Any]
ComplianceRule = Callable[[str, Mapping[str, Any]], bool]
AuditTravelsBuilder = Callable[[list[tuple[dict[str, Any], dict[str, Any]]], Mapping[str, Any]], list[dict[str, Any]]]
FormBuilder = Callable[[list[tuple[dict[str, Any], dict[str, Any]]], Mapping[str, Any]], list[dict[str, Any]]]


def _default_compliance(goods_name: str, item: Mapping[str, Any]) -> bool:
    return True


@dataclass
class ExpenseProfile:
    name: str
    default_graph_path: Path | str | None = None
    receipt_enrichers: Mapping[str, ReceiptEnricher] = field(default_factory=dict)
    invoice_enrichers: Mapping[str, InvoiceEnricher] = field(default_factory=dict)
    compliance_rule: ComplianceRule = _default_compliance
    audit_travels_builder: AuditTravelsBuilder | None = None
    form_invoice_tax_views_builder: FormBuilder | None = None
    writeback_save_path: str = AUDIT_INFO_SAVE_PATH


_TELECOM_PROFILE: ExpenseProfile | None = None


def _build_telecom_profile() -> ExpenseProfile:
    from .telecom.data import load_telecom_list, telecom_receipt_enricher
    from .telecom.writeback import telecom_compliance_rule

    return ExpenseProfile(
        name="telecom",
        default_graph_path=DEFAULT_GRAPH_PATH,
        receipt_enrichers={"telecom_list": telecom_receipt_enricher(load_telecom_list())},
        compliance_rule=telecom_compliance_rule,
        writeback_save_path=AUDIT_INFO_SAVE_PATH,
    )


def get_profile(name: str) -> ExpenseProfile:
    global _TELECOM_PROFILE
    normalized = name.strip().lower()
    if normalized == "telecom":
        if _TELECOM_PROFILE is None:
            _TELECOM_PROFILE = _build_telecom_profile()
        return _TELECOM_PROFILE
    if normalized == "travel":
        raise NotImplementedError(
            "travel profile is not registered yet; fill profiles/travel/ implementation before enabling"
        )
    raise ValueError(f"unknown expense profile: {name!r}")


__all__ = [
    "AuditTravelsBuilder",
    "ComplianceRule",
    "ExpenseProfile",
    "FormBuilder",
    "InvoiceEnricher",
    "ReceiptEnricher",
    "get_profile",
]
