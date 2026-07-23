from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Personal transport (个人交通费) profile data enricher — placeholder.
# 个人交通费当前无费用类型专属的离线资产/在线 fetch 需求（规则判断在图内节点完成）。
# 若后续需要个人交通费专属数据（如员工通勤标准、车辆登记清单等），在此实现 enricher 注入 serviceData。


def personal_transport_receipt_enricher(
    receipt_code: str, service_data: Mapping[str, Any]
) -> dict[str, Any]:
    """个人交通费收据级 enricher：当前无专属数据，返回空 dict。

    保留插槽以便后续注入个人交通费专属数据，避免新增数据时改动底座代码。
    """
    return {}


__all__ = ["personal_transport_receipt_enricher"]
