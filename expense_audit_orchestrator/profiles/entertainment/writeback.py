from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Entertainment (业务招待费) writeback strategy — placeholder.
# 招待费回写当前使用通用合规判断（默认放行），auditTravels/formInvoiceTaxViews 为空。
# 若后续需要招待费专属合规规则或行程明细，在此实现。


def entertainment_compliance_rule(goods_name: str, item: Mapping[str, Any]) -> bool:
    """招待费合规判断：当前默认放行（合规规则在图内 LLM 节点完成）。

    保留插槽以便后续实现招待费专属回写合规判断。
    """
    return True


__all__ = ["entertainment_compliance_rule"]
