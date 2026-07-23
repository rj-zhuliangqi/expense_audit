from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Entertainment (业务招待费) profile data enricher — placeholder.
# 招待费当前无费用类型专属的离线资产/在线 fetch 需求（规则判断在图内 LLM 节点完成）。
# 若后续需要招待人数标准表、接待对象清单等专属数据，在此实现 enricher 注入 serviceData。


def entertainment_receipt_enricher(receipt_code: str, service_data: Mapping[str, Any]) -> dict[str, Any]:
    """招待费收据级 enricher：当前无专属数据，返回空 dict。

    保留插槽以便后续注入招待费专属数据（如接待人数标准、礼品清单等），
    避免新增数据时改动底座代码。
    """
    return {}


__all__ = ["entertainment_receipt_enricher"]
