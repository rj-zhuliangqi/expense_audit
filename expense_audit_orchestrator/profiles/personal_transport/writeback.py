from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Personal transport (个人交通费) writeback strategy — placeholder.
# 个人交通费回写当前使用通用合规判断（默认放行），auditTravels/formInvoiceTaxViews 为空。
# 合规规则在图内节点（如发票内容项目检查、充值卡检查等）完成，回写仅汇总结果。
# 若后续需要个人交通费专属回写合规判断，在此实现。


def personal_transport_compliance_rule(
    goods_name: str, item: Mapping[str, Any]
) -> bool:
    """个人交通费合规判断：当前默认放行（合规规则在图内节点完成）。

    保留插槽以便后续实现个人交通费专属回写合规判断。
    """
    return True


__all__ = ["personal_transport_compliance_rule"]
