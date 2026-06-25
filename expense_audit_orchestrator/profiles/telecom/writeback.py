from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def telecom_compliance_rule(goods_name: str, item: Mapping[str, Any]) -> bool:
    return not goods_name or (
        "*电信服务*违约金" not in goods_name
        and "*电信服务*代收费" not in goods_name
    )


__all__ = ["telecom_compliance_rule"]
