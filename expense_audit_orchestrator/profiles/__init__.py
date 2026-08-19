from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..paths import (
    DEFAULT_GRAPH_PATH,
    OFFICIAL_GRAPH_PATHS,
    PROJECT_ROOT,
    ROOT,
    resolve_project_path,
)
from ..writeback_client import AUDIT_INFO_SAVE_PATH


# 各费用类型的默认图路径（ROOT 为项目根目录，与 runtime_client.DEFAULT_GRAPH_PATH 同级）
# 图路径支持通过 .env 环境变量覆盖，避免每次换图都要改代码：
#   TELECOM_GRAPH_PATH / PERSONAL_TRANSPORT_GRAPH_PATH / TRAVEL_GRAPH_PATH / ENTERTAINMENT_GRAPH_PATH
# 留空或未设置时用代码内默认值；相对路径以项目根目录 ROOT 为基准解析。
TELECOM_GRAPH_PATH_ENV = "TELECOM_GRAPH_PATH"
PERSONAL_TRANSPORT_GRAPH_PATH_ENV = "PERSONAL_TRANSPORT_GRAPH_PATH"
TRAVEL_GRAPH_PATH_ENV = "TRAVEL_GRAPH_PATH"
ENTERTAINMENT_GRAPH_PATH_ENV = "ENTERTAINMENT_GRAPH_PATH"


def _resolve_graph_path(env_key: str, default: Path | None) -> Path | None:
    """从环境变量读取图路径，留空则用代码内默认值。

    相对路径以项目根目录为基准解析；空字符串视为未设置（用默认值）。
    """
    raw = (os.getenv(env_key) or "").strip()
    if not raw:
        return default
    return resolve_project_path(raw, default)


PERSONAL_TRANSPORT_GRAPH_PATH = _resolve_graph_path(
    PERSONAL_TRANSPORT_GRAPH_PATH_ENV, OFFICIAL_GRAPH_PATHS["personal_transport"]
)
# 差旅执行图默认使用差旅专属图，仍可通过 .env 的 TRAVEL_GRAPH_PATH 覆盖。
TRAVEL_GRAPH_PATH: Path | None = _resolve_graph_path(
    TRAVEL_GRAPH_PATH_ENV, OFFICIAL_GRAPH_PATHS["travel"]
)
ENTERTAINMENT_GRAPH_PATH = _resolve_graph_path(
    ENTERTAINMENT_GRAPH_PATH_ENV, OFFICIAL_GRAPH_PATHS["entertainment"]
)


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
    audit_rule_catalog: Mapping[str, Mapping[str, Any]] | None = None
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


def _build_telecom_profile(
    asset_dir: Path | str | None = None,
    service_url: str | None = None,
) -> ExpenseProfile:
    from .telecom.data import (
        load_telecom_list,
        resolve_telecom_csv_path,
        telecom_receipt_enricher,
    )
    from .telecom.writeback import telecom_compliance_rule

    csv_path = resolve_telecom_csv_path(asset_dir)
    return ExpenseProfile(
        name="telecom",
        default_graph_path=_resolve_graph_path(TELECOM_GRAPH_PATH_ENV, DEFAULT_GRAPH_PATH),
        receipt_enrichers={"telecom_list": telecom_receipt_enricher(load_telecom_list(csv_path))},
        compliance_rule=telecom_compliance_rule,
        writeback_save_path=AUDIT_INFO_SAVE_PATH,
    )


def _build_travel_profile(
    asset_dir: Path | str | None = None,
    service_url: str | None = None,
) -> ExpenseProfile:
    """差旅（travel）profile。

    差旅与个人交通费是两个独立费用类型：差旅涉及行程预订/里程标准等业务数据，
    个人交通费（personal_transport）走 graph-latest-personal-transport-0722.json。
    差旅数据准备使用 travel profile 内的专属接口聚合器，
    差旅图路径仍可通过 .env 的 TRAVEL_GRAPH_PATH 覆盖。
    """
    from .travel.data import (
        build_travel_receipt_enricher,
        travel_invoice_enricher,
    )
    from .travel.writeback import (
        travel_audit_travels_builder,
        travel_compliance_rule,
        travel_form_invoice_tax_views_builder,
    )

    return ExpenseProfile(
        name="travel",
        default_graph_path=TRAVEL_GRAPH_PATH,
        receipt_enrichers={
            "travelAudit": build_travel_receipt_enricher(service_url=service_url),
        },
        invoice_enrichers={"travelAudit": travel_invoice_enricher},
        compliance_rule=travel_compliance_rule,
        audit_travels_builder=travel_audit_travels_builder,
        form_invoice_tax_views_builder=travel_form_invoice_tax_views_builder,
        writeback_save_path=AUDIT_INFO_SAVE_PATH,
    )


def _build_personal_transport_profile(
    asset_dir: Path | str | None = None,
    service_url: str | None = None,
) -> ExpenseProfile:
    """个人交通费（personal_transport）profile。

    对应执行图 graph-latest-personal-transport-0722.json（可通过 .env 的
    PERSONAL_TRANSPORT_GRAPH_PATH 覆盖）。合规规则在图内节点完成，回写使用默认放行策略。
    """
    from .personal_transport.data import (
        build_taxi_invoice_serial_enricher,
        personal_transport_invoice_type_enricher,
        personal_transport_receipt_enricher,
    )
    from .personal_transport.writeback import personal_transport_compliance_rule

    return ExpenseProfile(
        name="personal_transport",
        default_graph_path=PERSONAL_TRANSPORT_GRAPH_PATH,
        receipt_enrichers={"personal_transport_data": personal_transport_receipt_enricher},
        invoice_enrichers={
            "personalTransportInvoiceType": personal_transport_invoice_type_enricher,
            "taxiInvoiceSerial": build_taxi_invoice_serial_enricher(service_url=service_url),
        },
        compliance_rule=personal_transport_compliance_rule,
        writeback_save_path=AUDIT_INFO_SAVE_PATH,
    )


def _build_entertainment_profile(
    asset_dir: Path | str | None = None,
    service_url: str | None = None,
) -> ExpenseProfile:
    from .entertainment.data import (
        build_e15_invoice_type_enricher,
        build_entertainment_invoice_serial_enricher,
        build_entertainment_receipt_enricher,
    )
    from .entertainment.writeback import entertainment_compliance_rule
    from .entertainment.writeback import entertainment_audit_rule_catalog

    return ExpenseProfile(
        name="entertainment",
        default_graph_path=ENTERTAINMENT_GRAPH_PATH,
        receipt_enrichers={
            "entertainment_data": build_entertainment_receipt_enricher(service_url=service_url),
        },
        invoice_enrichers={
            "e15InvoiceType": build_e15_invoice_type_enricher(),
            "entertainmentInvoiceSerial": build_entertainment_invoice_serial_enricher(
                service_url=service_url,
            ),
        },
        compliance_rule=entertainment_compliance_rule,
        audit_rule_catalog=entertainment_audit_rule_catalog,
        writeback_save_path=AUDIT_INFO_SAVE_PATH,
    )


# 内置 profile 自注册
register_profile("telecom", _build_telecom_profile)
register_profile("travel", _build_travel_profile)
register_profile("personal_transport", _build_personal_transport_profile)
register_profile("entertainment", _build_entertainment_profile)


def get_profile(
    name: str,
    *,
    telecom_asset_dir: Path | str | None = None,
    asset_dir: Path | str | None = None,
    service_url: str | None = None,
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
        kwargs: dict[str, Any] = {"asset_dir": resolved_asset_dir}
        if normalized in {"travel", "personal_transport", "entertainment"}:
            kwargs["service_url"] = service_url
        return builder(**kwargs)

    # 显式服务地址只用于当前实例，不能污染默认 profile 缓存。
    if service_url is not None and normalized in {"travel", "personal_transport", "entertainment"}:
        return builder(service_url=service_url)

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

    def resolve(
        self,
        ei_code: str | None,
        *,
        service_url: str | None = None,
    ) -> ExpenseProfile:
        if not ei_code:
            raise UnknownExpenseTypeError("eiCode is empty or None, cannot route to expense profile")
        normalized = ei_code.strip()
        profile_name = self.ei_code_map.get(normalized)
        if profile_name is None:
            raise UnknownExpenseTypeError(
                f"eiCode {normalized!r} is not mapped to any expense profile; "
                f"add it to ei_code_map.json to enable routing"
            )
        return get_profile(profile_name, service_url=service_url)

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
    "ENTERTAINMENT_GRAPH_PATH_ENV",
    "ExpenseProfile",
    "FormBuilder",
    "InvoiceEnricher",
    "PERSONAL_TRANSPORT_GRAPH_PATH",
    "PERSONAL_TRANSPORT_GRAPH_PATH_ENV",
    "ProfileResolver",
    "ReceiptEnricher",
    "TELECOM_GRAPH_PATH_ENV",
    "TRAVEL_GRAPH_PATH",
    "TRAVEL_GRAPH_PATH_ENV",
    "UnknownExpenseTypeError",
    "UnknownProfileError",
    "create_profile_resolver_from_env",
    "get_profile",
    "register_profile",
]
