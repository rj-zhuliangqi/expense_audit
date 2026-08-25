"""Shared loader for profile-specific audit risk catalogs."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..paths import PROJECT_ROOT

_ALLOWED_RISK_LEVELS = frozenset({"blocking", "high", "medium_low"})


def _normalize_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_risk_level(value: Any, *, code: str, profile_label: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "block": "blocking",
        "blocked": "blocking",
        "阻断": "blocking",
        "high_risk": "high",
        "高风险": "high",
        "low": "medium_low",
        "medium": "medium_low",
        "medium_risk": "medium_low",
        "low_risk": "medium_low",
        "中低风险": "medium_low",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _ALLOWED_RISK_LEVELS:
        allowed = ", ".join(sorted(_ALLOWED_RISK_LEVELS))
        raise ValueError(
            f"invalid riskLevel for {profile_label} rule {code!r}: {value!r}; "
            f"expected one of {allowed}"
        )
    return normalized


@lru_cache(maxsize=32)
def _load_cached(path: str, profile_label: str) -> dict[str, dict[str, Any]]:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    rules = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(rules, dict):
        raise ValueError(
            f"{profile_label} audit risk config must contain an object at 'rules': {config_path}"
        )

    catalog: dict[str, dict[str, Any]] = {}
    for raw_code, raw_metadata in rules.items():
        code = _normalize_code(raw_code)
        if not code:
            raise ValueError(
                f"{profile_label} audit risk config contains an empty rule code: {config_path}"
            )
        if not isinstance(raw_metadata, dict):
            raise ValueError(
                f"{profile_label} audit risk metadata must be an object for {code}: {config_path}"
            )
        metadata = dict(raw_metadata)
        metadata["riskLevel"] = _normalize_risk_level(
            metadata.get("riskLevel"),
            code=code,
            profile_label=profile_label,
        )
        catalog[code] = metadata
    return catalog


def load_audit_risk_catalog(
    path: str | Path,
    *,
    profile_label: str,
) -> dict[str, dict[str, Any]]:
    """Load a profile's risk catalog once per process and return a safe copy.

    Relative paths are resolved from the project root.  Profile builders call
    this while constructing their cached ``ExpenseProfile``; changing a JSON
    file therefore takes effect after the worker/service is restarted.
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    loaded = _load_cached(str(resolved.resolve()), profile_label)
    return {code: dict(metadata) for code, metadata in loaded.items()}


__all__ = ["load_audit_risk_catalog"]
