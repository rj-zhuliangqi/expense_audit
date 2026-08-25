"""个人交通费稽核点风险等级配置加载。

风险等级属于业务配置，不应该散落在汇总代码的 ``if Wxx`` 分支中。
配置文件在 profile 首次加载时读取并缓存到内存；进程生命周期内不会为每张
核销单重复读取磁盘。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...paths import PROJECT_ROOT

DEFAULT_AUDIT_RISK_CONFIG_PATH = Path(__file__).with_name("audit_risk_levels.json")
_ALLOWED_RISK_LEVELS = frozenset({"blocking", "high", "medium_low"})


def _normalize_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_risk_level(value: Any, *, code: str) -> str:
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
            f"invalid riskLevel for personal transport rule {code!r}: {value!r}; "
            f"expected one of {allowed}"
        )
    return normalized


@lru_cache(maxsize=8)
def _load_cached(path: str) -> dict[str, dict[str, Any]]:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    rules = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(rules, dict):
        raise ValueError(
            f"personal transport audit risk config must contain an object at 'rules': {config_path}"
        )

    catalog: dict[str, dict[str, Any]] = {}
    for raw_code, raw_metadata in rules.items():
        code = _normalize_code(raw_code)
        if not code:
            raise ValueError(f"personal transport audit risk config contains an empty rule code: {config_path}")
        if not isinstance(raw_metadata, dict):
            raise ValueError(
                f"personal transport audit risk metadata must be an object for {code}: {config_path}"
            )
        metadata = dict(raw_metadata)
        metadata["riskLevel"] = _normalize_risk_level(metadata.get("riskLevel"), code=code)
        catalog[code] = metadata
    return catalog


def load_personal_transport_audit_risk_catalog(
    path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load and cache the personal-transport audit risk catalog.

    Relative paths are resolved from the project root.  The returned mapping is
    copied so callers cannot mutate the cached configuration accidentally.
    """
    resolved = Path(path) if path is not None else DEFAULT_AUDIT_RISK_CONFIG_PATH
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    loaded = _load_cached(str(resolved.resolve()))
    return {code: dict(metadata) for code, metadata in loaded.items()}


__all__ = [
    "DEFAULT_AUDIT_RISK_CONFIG_PATH",
    "load_personal_transport_audit_risk_catalog",
]
