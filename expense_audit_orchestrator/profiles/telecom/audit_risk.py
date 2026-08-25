"""通讯费稽核点风险等级配置加载。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..audit_risk import load_audit_risk_catalog

DEFAULT_AUDIT_RISK_CONFIG_PATH = Path(__file__).with_name("audit_risk_levels.json")


def load_telecom_audit_risk_catalog(
    path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load and cache the telecom audit risk catalog."""
    return load_audit_risk_catalog(
        path or DEFAULT_AUDIT_RISK_CONFIG_PATH,
        profile_label="telecom",
    )


__all__ = ["DEFAULT_AUDIT_RISK_CONFIG_PATH", "load_telecom_audit_risk_catalog"]
