"""Normalize the EOR flag shared by graph execution and writeback.

The audit service exposes a one-character source value for ``IsEor`` while
callers and older payloads may use booleans or differently-cased JSON keys.
Keep the conversion in one place so all expense profiles use identical
semantics. The normalized value is used internally by graph/E31 logic; it is
not a top-level field of the result writeback DTO.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


EOR_PROFILES = frozenset({"telecom", "personal_transport", "entertainment"})


def normalize_is_eor_value(value: Any) -> str | None:
    """Return the database/API representation of an EOR flag.

    ``form_masterinfo.IsEor`` is a one-character field.  Do not pass Python
    booleans or the strings ``"true"``/``"false"`` to the writeback API.
    Unknown values remain ``None`` so they cannot silently become a false EOR
    decision.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return None

    normalized = str(value).strip().lower()
    if normalized in {"1", "true"}:
        return "1"
    if normalized in {"0", "false"}:
        return "0"
    return None


def resolve_is_eor_value(
    audit_info: Mapping[str, Any] | None,
    *,
    default: str | None = None,
) -> str | None:
    """Read ``isEor`` from canonical and legacy key spellings.

    Some upstream/legacy payloads use ``IsEor``, ``isEOR`` or ``is_eor``.
    Prefer the canonical key, then scan case/underscore-insensitively for the
    aliases.  A default is only applied when no valid value is present.
    """
    if not isinstance(audit_info, Mapping):
        return default

    preferred_keys = ("isEor", "IsEor", "isEOR", "is_eor")
    for key in preferred_keys:
        if key in audit_info:
            normalized = normalize_is_eor_value(audit_info.get(key))
            if normalized is not None:
                return normalized

    for key, value in audit_info.items():
        normalized_key = "".join(character.lower() for character in str(key) if character.isalnum())
        if normalized_key != "iseor":
            continue
        normalized = normalize_is_eor_value(value)
        if normalized is not None:
            return normalized

    return default


def is_eor_profile(expense_profile: str | None) -> bool:
    """Return whether the profile has the EOR-specific E31 behavior."""
    normalized = (expense_profile or "").strip().lower().replace("-", "_")
    return normalized in EOR_PROFILES
