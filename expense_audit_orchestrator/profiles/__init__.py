from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..runtime_client import DEFAULT_GRAPH_PATH, ROOT
from ..writeback_client import AUDIT_INFO_SAVE_PATH


# 各费用类型的默认图路径（ROOT 为项目根目录，与 runtime_client.DEFAULT_GRAPH_PATH 同级）
TRAVEL_GRAPH_PATH = ROOT / "graph-latest-travel-0722.json"
ENTERTAINMENT_GRAPH_PATH = ROOT / "graph-latest-entertainment-0722.json"


ReceiptEnricher = Callable[[str, Mapping[str, Any]], Any]
InvoiceEnricher = Callable[[str, str, dict[str, Any], dict[str, Any]], Any]
ComplianceRule = Callable[[str, Mapping[str, Any]], bool]
AuditTravelsBuilder = Callable[[list[tuple[dict[str, Any], dict[str, Any]]], Mapping[str, Any]], list[dict[str, Any]]]
FormBuilder = Callable[[list[tuple[dict[str, Any], dict[str, Any]]], Mapping[str, Any]], list[dict[str, Any]]]


class UnknownProfileError(ValueError):
    """请求的 expense profile 未在注册表中注册。"""


class UnknownExpenseTypeError(ValueError):
    """eiCode 无法映射到任何已注册的 expense profile。

    由 ``ProfileResolver.resolve`` 抛出，供 worker 捕获后标记任务失败。
    """


def _default_compliance(goods_name: str, item: Mapping[str, Any]) -> bool:
    return True


@dataclass
class ExpenseProfile:
    name: str
    default_graph_path: Path | str | None = None
    receipt_enrichers: Mapping[str, ReceiptEnricher] = field(default_factory=dict)
    invoice_enrichers: Mapping[str, InvoiceEnricher] = field(default_factory=dict)
    compliance_rule: ComplianceRule = _default_compliance
    audit_travels_builder: AuditTravelsBuilder | None = None
    form_invoice_tax_views_builder: FormBuilder | None = None
    writeback_save_path: str = AUDIT_INFO_SAVE_PATH


# ---------------------------------------------------------------------------
# Profile 注册表：字典注册 + 懒加载缓存
# ---------------------------------------------------------------------------

# name -> builder(asset_dir=None) -> ExpenseProfile；builder 首次调用后缓存
_PROFILE_BUILDERS: dict[str, Callable[..., ExpenseProfile]] = {}
_PROFILE_CACHE: dict[str, ExpenseProfile] = {}


def register_profile(
    name: str,
    builder: Callable[..., ExpenseProfile],
) -> None:
    """注册一个 expense profile builder。

    新增费用类型时，在对应 ``profiles/<type>/`` 模块导入时调用本函数完成自注册。
    builder 签名 ``builder(asset_dir: Path | str | None = None) -> ExpenseProfile``，
    首次 ``get_profile`` 调用时执行并缓存。
    """
    normalized = name.strip().lower()
    _PROFILE_BUILDERS[normalized] = builder
    # 清除可能存在的旧缓存，确保下次 get_profile 重新构建
    _PROFILE_CACHE.pop(normalized, None)


def _build_telecom_profile(asset_dir: Path | str | None = None) -> ExpenseProfile:
    from .telecom.data import (
        load_telecom_list,
        resolve_telecom_csv_path,
        telecom_receipt_enricher,
    )
    from .telecom.writeback import telecom_compliance_rule

    csv_path = resolve_telecom_csv_path(asset_dir)
    return ExpenseProfile(
        name="telecom",
        default_graph_path=DEFAULT_GRAPH_PATH,
        receipt_enrichers={"telecom_list": telecom_receipt_enricher(load_telecom_list(csv_path))},
        compliance_rule=telecom_compliance_rule,
        writeback_save_path=AUDIT_INFO_SAVE_PATH,
    )


def _build_travel_profile(asset_dir: Path | str | None = None) -> ExpenseProfile:
    from .travel.data import travel_receipt_enricher
    from .travel.writeback import travel_audit_travels_builder, travel_compliance_rule

    return ExpenseProfile(
        name="travel",
        default_graph_path=TRAVEL_GRAPH_PATH,
        receipt_enrichers={"travel_data": travel_receipt_enricher},
        compliance_rule=travel_compliance_rule,
        audit_travels_builder=travel_audit_travels_builder,
        writeback_save_path=AUDIT_INFO_SAVE_PATH,
    )


def _build_entertainment_profile(asset_dir: Path | str | None = None) -> ExpenseProfile:
    from .entertainment.data import entertainment_receipt_enricher
    from .entertainment.writeback import entertainment_compliance_rule

    return ExpenseProfile(
        name="entertainment",
        default_graph_path=ENTERTAINMENT_GRAPH_PATH,
        receipt_enrichers={"entertainment_data": entertainment_receipt_enricher},
        compliance_rule=entertainment_compliance_rule,
        writeback_save_path=AUDIT_INFO_SAVE_PATH,
    )


# 内置 profile 自注册
register_profile("telecom", _build_telecom_profile)
register_profile("travel", _build_travel_profile)
register_profile("entertainment", _build_entertainment_profile)


def get_profile(
    name: str,
    *,
    telecom_asset_dir: Path | str | None = None,
    asset_dir: Path | str | None = None,
) -> ExpenseProfile:
    """按名称获取已注册的 expense profile（懒加载 + 缓存）。

    向后兼容：``telecom_asset_dir`` 参数仅对 telecom profile 生效（等价于 asset_dir）。
    新代码应使用 ``asset_dir``。
    """
    normalized = name.strip().lower()
    builder = _PROFILE_BUILDERS.get(normalized)
    if builder is None:
        raise UnknownProfileError(f"unknown expense profile: {name!r}")

    # telecom 保留 telecom_asset_dir 向后兼容
    resolved_asset_dir = asset_dir or telecom_asset_dir
    if resolved_asset_dir is not None:
        # 显式指定资产目录时，不使用缓存，每次重新构建（确保资产变更生效）
        return builder(asset_dir=resolved_asset_dir)

    if normalized not in _PROFILE_CACHE:
        _PROFILE_CACHE[normalized] = builder()
    return _PROFILE_CACHE[normalized]


# ---------------------------------------------------------------------------
# ProfileResolver：eiCode -> ExpenseProfile 路由
# ---------------------------------------------------------------------------

# 默认 eiCode 映射文件路径（包内）
DEFAULT_EI_CODE_MAP_PATH = Path(__file__).parent / "ei_code_map.json"
EI_CODE_MAP_PATH_ENV = "EI_CODE_MAP_PATH"


@dataclass
class ProfileResolver:
    """根据 eiCode 路由到对应的 ExpenseProfile。

    持有 eiCode -> profile name 的映射表，``resolve`` 时查表后调 ``get_profile``。
    eiCode 未命中时抛 ``UnknownExpenseTypeError``，供 worker 捕获后标记任务失败。
    """

    ei_code_map: Mapping[str, str]

    def resolve(self, ei_code: str | None) -> ExpenseProfile:
        if not ei_code:
            raise UnknownExpenseTypeError("eiCode is empty or None, cannot route to expense profile")
        normalized = ei_code.strip()
        profile_name = self.ei_code_map.get(normalized)
        if profile_name is None:
            raise UnknownExpenseTypeError(
                f"eiCode {normalized!r} is not mapped to any expense profile; "
                f"add it to ei_code_map.json to enable routing"
            )
        return get_profile(profile_name)

    @classmethod
    def from_map_file(cls, path: Path | str | None = None) -> "ProfileResolver":
        """从 JSON 文件加载 eiCode 映射表。

        文件格式：``{"EI001": "telecom", "EI002": "travel", ...}``，
        以 ``_`` 开头的键（如 ``_comment``）会被忽略。
        """
        resolved_path = Path(path) if path is not None else _resolve_ei_code_map_path()
        with resolved_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError(f"ei_code_map file must be a JSON object, got {type(raw).__name__}: {resolved_path}")
        ei_code_map = {str(k): str(v) for k, v in raw.items() if not str(k).startswith("_")}
        return cls(ei_code_map=ei_code_map)


def _resolve_ei_code_map_path() -> Path:
    env_path = os.getenv(EI_CODE_MAP_PATH_ENV)
    if env_path and env_path.strip():
        return Path(env_path.strip())
    return DEFAULT_EI_CODE_MAP_PATH


def create_profile_resolver_from_env() -> ProfileResolver:
    """从环境变量 ``EI_CODE_MAP_PATH`` 加载 ProfileResolver（默认用包内映射文件）。"""
    return ProfileResolver.from_map_file(_resolve_ei_code_map_path())


__all__ = [
    "AuditTravelsBuilder",
    "ComplianceRule",
    "DEFAULT_EI_CODE_MAP_PATH",
    "EI_CODE_MAP_PATH_ENV",
    "ENTERTAINMENT_GRAPH_PATH",
    "ExpenseProfile",
    "FormBuilder",
    "InvoiceEnricher",
    "ProfileResolver",
    "ReceiptEnricher",
    "TRAVEL_GRAPH_PATH",
    "UnknownExpenseTypeError",
    "UnknownProfileError",
    "create_profile_resolver_from_env",
    "get_profile",
    "register_profile",
]
