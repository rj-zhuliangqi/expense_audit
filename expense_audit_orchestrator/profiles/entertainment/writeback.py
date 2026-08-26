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


entertainment_audit_rule_catalog: dict[str, dict[str, str]] = {
    "E01": {"auditContent": "检查发票购买方公司名称与核销单财务体系映射公司名称是否一致"},
    "E02": {"auditContent": "检查发票购买方纳税人识别号与核销单财务体系映射纳税人识别号是否一致"},
    "E35": {"auditContent": "检查发票票种是否符合业务招待费报销范围"},
    "E33": {"auditContent": "检查使用发票是否为当年发票"},
    "E09": {"auditContent": "检查发票是否已被核销"},
    "E05": {"auditContent": "检查发票是否被其他核销单重复使用"},
    "E15": {"auditContent": "检查是否有员工本人费用"},
    "W33": {"auditContent": "检查礼品数量与接待人数的合理性"},
    "W34": {"auditContent": "检查本核销单及跨核销单发票号码是否连续或近似（差值≤10）"},
    "E42": {"auditContent": "检查本核销单内出租车发票号码是否存在连号"},
    "E36": {"auditContent": "检查发票内容是否含禁止核销内容"},
    "E17": {"auditContent": "检查发票内容是否包含充值卡、预付卡或预存类项目"},
    "W31": {"auditContent": "检查使用的发票销货方是否为高风险发票"},
}


__all__ = ["entertainment_compliance_rule", "entertainment_audit_rule_catalog"]
