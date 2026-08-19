import inspect
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from expense_audit_orchestrator import audit_client
from expense_audit_orchestrator.observability import get_logger


from expense_audit_orchestrator.paths import DEFAULT_OCR_PATH, PROJECT_ROOT

ROOT = PROJECT_ROOT

_logger = get_logger("data_prep")

InvoiceFileProvider = Callable[[str], str]
OCRProvider = Callable[..., dict[str, Any]]
AuditInfoProvider = Callable[[str], dict[str, Any]]
CompanyBlacklistProvider = Callable[[], list[dict[str, Any]]]
CompanyListProvider = Callable[[], list[dict[str, Any]]]
ExpenseInvoiceTypesProvider = Callable[[str], list[dict[str, Any]]]
InvoiceInfoProvider = Callable[[str, str, str | None], list[dict[str, Any]]]
AuditInvoiceFilesProvider = Callable[[str, int], list[dict[str, Any]]]
AuditInvoiceFileInfoProvider = Callable[[str], list[dict[str, Any]]]
FieldMappingsProvider = Callable[[str], list[dict[str, Any]]]
ReceiptEnricher = Callable[[str, Mapping[str, Any]], Any]
DataEnricher = Callable[[str, str, dict[str, Any], dict[str, Any]], Any]
# 企查查企业信息查询器：输入企业名称，返回 dict(含 establishDate/address) 或 None（失败/未找到）
EnterpriseInfoProvider = Callable[[str], dict[str, Any] | None]


def get_invoice_file_from_server(receipt_code: str) -> str:
    """模拟从文件服务器获取发票文件路径或流"""
    _logger.info(
        "获取发票文件路径",
        extra={"receipt_code": receipt_code, "event": "data_prep.invoice_file", "file_path": "/storage/pdf/2026/001.pdf"},
    )
    return "/storage/pdf/2026/001.pdf"


def call_ocr_service(file_path: str, ocr_sample_path: Path | str = DEFAULT_OCR_PATH) -> dict[str, Any]:
    """模拟调用 OCR 识别能力，并清洗转化为标准的业务结构"""
    _logger.info("OCR 识别发票", extra={"event": "data_prep.ocr", "file_path": file_path})
    time.sleep(0.5)

    with Path(ocr_sample_path).open("r", encoding="utf-8") as source:
        return json.load(source)


def _get_string_value(data: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _resolve_accounting_code(
    ocr_data: Mapping[str, Any],
    audit_info: Mapping[str, Any],
    company_list: list[dict[str, Any]],
) -> str | None:
    accounting_code = _get_string_value(ocr_data, "accountingCode")
    if accounting_code is not None:
        return accounting_code

    accounting_code = _get_string_value(audit_info, "accountingCode")
    if accounting_code is not None:
        return accounting_code

    company_tax = _get_string_value(ocr_data, "buyerTaxNo", "buyerTaxNO") or _get_string_value(
        audit_info,
        "companyTax",
        "taxNo",
    )
    if company_tax is not None:
        for company in company_list:
            if company_tax == _get_string_value(company, "companyTax", "taxNo"):
                accounting_code = _get_string_value(company, "accountingCode", "cCode", "ccode")
                if accounting_code is not None:
                    return accounting_code

    company_name = _get_string_value(ocr_data, "buyerName", "orgName") or _get_string_value(
        audit_info,
        "verifiUserCompanyName",
        "companyName",
        "companyFullName",
    )
    if company_name is not None:
        for company in company_list:
            if company_name in {
                _get_string_value(company, "companyName"),
                _get_string_value(company, "cName", "cname"),
                _get_string_value(company, "cShortName", "cshortName"),
            }:
                accounting_code = _get_string_value(company, "accountingCode", "cCode", "ccode")
                if accounting_code is not None:
                    return accounting_code

    if company_list:
        first_company = company_list[0]
        return _get_string_value(first_company, "accountingCode", "cCode", "ccode")

    return None


def _build_base64_file_path(file_base64: str) -> str:
    return f"base64://{file_base64}"


def _resolve_invoice_file_path(
    audit_invoice_file_info: Mapping[str, Any] | None,
) -> str:
    normalized_info = audit_invoice_file_info or {}
    file_base64 = _get_string_value(normalized_info, "fileBase64", "base64")
    if file_base64 is not None:
        return _build_base64_file_path(file_base64)

    file_url = _get_string_value(normalized_info, "fileUrl", "filePath", "downloadUrl", "url")
    if file_url is not None:
        return file_url

    raise ValueError("audit invoice file info must contain fileBase64 or fileUrl")


def _resolve_invoice_file_name(invoice_file: Mapping[str, Any]) -> str | None:
    file_name = _get_string_value(invoice_file, "fileName")
    if file_name is not None:
        return file_name

    audit_invoice_file = invoice_file.get("auditInvoiceFile")
    if isinstance(audit_invoice_file, Mapping):
        file_name = _get_string_value(audit_invoice_file, "fileName", "name")
        if file_name is not None:
            return file_name

    audit_invoice_file_info = invoice_file.get("auditInvoiceFileInfo")
    if isinstance(audit_invoice_file_info, Mapping):
        return _get_string_value(audit_invoice_file_info, "fileName", "name")

    return None


def _collect_audit_invoice_file_info(
    audit_invoice_files: list[dict[str, Any]],
    audit_invoice_file_info_provider: AuditInvoiceFileInfoProvider,
) -> list[dict[str, Any]]:
    audit_invoice_file_info: list[dict[str, Any]] = []
    seen_fids: set[str] = set()

    for audit_invoice_file in audit_invoice_files:
        fid = _get_string_value(audit_invoice_file, "fid")
        if fid is None or fid in seen_fids:
            continue

        seen_fids.add(fid)
        audit_invoice_file_info.extend(audit_invoice_file_info_provider(fid))

    return audit_invoice_file_info


def _build_invoice_files(
    receipt_code: str,
    audit_invoice_files: list[dict[str, Any]],
    audit_invoice_file_info: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not audit_invoice_files:
        raise ValueError(f"audit invoice files are required for receipt: {receipt_code}")

    file_info_by_fid: dict[str, dict[str, Any]] = {}
    for file_info in audit_invoice_file_info:
        fid = _get_string_value(file_info, "fid")
        if fid is None or fid in file_info_by_fid:
            continue
        file_info_by_fid[fid] = file_info

    invoice_files: list[dict[str, Any]] = []
    for audit_invoice_file in audit_invoice_files:
        fid = _get_string_value(audit_invoice_file, "fid")
        if fid is None:
            raise ValueError("audit invoice file is missing fid")

        file_info = file_info_by_fid.get(fid)
        if file_info is None:
            raise ValueError(f"audit invoice file info missing for fid: {fid}")

        invoice_files.append(
            {
                "invoiceKey": fid,
                "fid": fid,
                "filePath": _resolve_invoice_file_path(file_info),
                "auditInvoiceFile": audit_invoice_file,
                "auditInvoiceFileInfo": file_info,
            }
        )

    return invoice_files


def _resolve_ocr_provider_kwargs(
    ocr_provider: OCRProvider,
    *,
    receipt_code: str,
    audit_info: Mapping[str, Any],
    company_list: list[dict[str, Any]],
    file_name: str | None = None,
) -> dict[str, Any]:
    try:
        signature = inspect.signature(ocr_provider)
    except (TypeError, ValueError):
        return {}

    candidate_kwargs = {
        "receipt_code": receipt_code,
        "audit_info": audit_info,
        "company_list": company_list,
    }
    if file_name is not None:
        candidate_kwargs["file_name"] = file_name
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return candidate_kwargs

    return {
        name: value
        for name, value in candidate_kwargs.items()
        if name in signature.parameters
    }


def _extract_ocr_envelope(ocr_result: Mapping[str, Any]) -> dict[str, Any] | None:
    recognition = ocr_result.get("recognition")
    if not isinstance(recognition, Mapping):
        return None

    normalized = recognition.get("normalized")
    if not isinstance(normalized, Mapping):
        return None

    return deepcopy(dict(ocr_result))


def _extract_normalized_ocr_data(ocr_result: Mapping[str, Any]) -> dict[str, Any]:
    recognition = ocr_result.get("recognition")
    if isinstance(recognition, Mapping):
        normalized = recognition.get("normalized")
        if isinstance(normalized, Mapping):
            return dict(normalized)

    return dict(ocr_result)


def _resolve_first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                return dict(item)
    return {}


def _join_goods_names(items: Any) -> str:
    """将 OCR 明细中的商品名称按发票内容格式拼接。

    OCR 的 ``items`` 可能缺失、包含非对象元素，或存在空的
    ``goodsName``。数据准备阶段统一处理这些情况，避免流程图和回写
    层各自实现一套拼接逻辑。
    """
    if not isinstance(items, list):
        return ""

    goods_names: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        value = item.get("goodsName")
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            goods_names.append(normalized)
    return "、".join(goods_names)


def _normalize_ocr_goods_name(ocr_data: Mapping[str, Any]) -> dict[str, Any]:
    """把 OCR 明细中的 goodsName 汇总为单据级 goodsName。

    发票内容的唯一来源是 ``items[*].goodsName``。这里不读取或回退到
    ``contents``、``invoiceContents``、``description`` 等其他字段，避免
    OCR 字段混用后难以定位数据问题。
    """
    normalized = dict(ocr_data)
    # goodsName 是明细字段；准备数据中的单据级 goodsName 只允许由 items 生成。
    # 同时移除历史上容易被误认为“发票内容”的别名，避免后续流程误读
    # contents/invoiceContents/description；原始 OCR 仍保留在 ocrEnvelope 中用于审计追溯。
    for legacy_key in ("goodsName", "contents", "invoiceContents", "description", "filtered_items"):
        normalized.pop(legacy_key, None)
    joined_goods_names = _join_goods_names(normalized.get("items"))
    if joined_goods_names:
        normalized["goodsName"] = joined_goods_names
    return normalized


def build_rule_input(
    receipt_code: str,
    ocr_data: dict[str, Any],
    *,
    file_path: str,
    service_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_service_data = dict(service_data or {})
    audit_info = dict(normalized_service_data.get("auditInfo") or {})
    current_invoice_info = _resolve_first_mapping(normalized_service_data.get("currentInvoiceInfo"))
    current_audit_invoice_file = dict(normalized_service_data.get("currentAuditInvoiceFile") or {})
    instance_code = _get_string_value(audit_info, "instanceCode") or receipt_code
    instance_com_code = _get_string_value(audit_info, "instanceComCode")
    invoice_file_id = _get_string_value(current_audit_invoice_file, "aifid", "afiid")
    invoice_info_id = _get_string_value(current_invoice_info, "aiiid")
    # 发票内容唯一来自 OCR items[*].goodsName；这里使用数据准备阶段生成的
    # 单据级 goodsName，不从 contents 等其他字段回退。
    normalized_ocr_data = _normalize_ocr_goods_name(ocr_data)
    return {
        **normalized_ocr_data,
        "instance_code": instance_code,
        "instanceComCode": instance_com_code,
        "invoice_file_id": invoice_file_id,
        "invoice_info_id": invoice_info_id,
        "receipt": {
            "code": receipt_code,
            "filePath": file_path,
        },
        "serviceData": normalized_service_data,
        "context": _build_context(normalized_service_data, receipt_code, audit_info),
    }


# LLM 网关地址统一从 .env 的 NODE_GATEWAY_URL 读取，与 overall_advice.py 共用同一配置。
# 在 build_rule_input 阶段就注入 context.llmGatewayUrl，确保存盘的 prepared-receipt.json
# 自带网关地址，方便 Gorules Editor 等离线工具模拟运行时无需环境变量即可调用 LLM。
_NODE_GATEWAY_LLM_EVALUATE_PATH = "/api/v1/node-gateway/llm/evaluate"


def _resolve_llm_gateway_url() -> str | None:
    """读取 NODE_GATEWAY_URL 环境变量，拼接完整 LLM evaluate 端点。

    若未配置则返回 None — 此时 context 中不写 llmGatewayUrl 字段，
    application.py 的 _inject_run_context 会作为 fallback 兜底。
    """
    load_dotenv(ROOT / ".env", override=False)
    raw_url = (os.getenv("NODE_GATEWAY_URL") or "").strip()
    if not raw_url:
        return None
    return f"{raw_url.rstrip('/')}{_NODE_GATEWAY_LLM_EVALUATE_PATH}"


def _build_context(
    normalized_service_data: dict[str, Any],
    receipt_code: str,
    audit_info: Mapping[str, Any],
) -> dict[str, Any]:
    """构建 preparedInput.context，包含 serviceData / receiptCode / employee 以及可选的 llmGatewayUrl。"""
    context: dict[str, Any] = {
        "serviceData": normalized_service_data,
        "receiptCode": receipt_code,
        "employee": _build_employee_context(audit_info),
    }
    llm_gateway_url = _resolve_llm_gateway_url()
    if llm_gateway_url:
        context["llmGatewayUrl"] = llm_gateway_url
    return context


def _build_employee_context(audit_info: Mapping[str, Any]) -> dict[str, Any]:
    """从 auditInfo 中提取员工信息，供图内节点使用。

    优先取 verifiUserName/verifiUserId/verifiUserPhone 等字段；
    无法获取时返回空 dict（而非假数据），避免误导规则判断。
    """
    if not isinstance(audit_info, Mapping):
        return {}
    employee: dict[str, Any] = {}
    name = _get_string_value(audit_info, "verifiUserName")
    if name:
        employee["name"] = name
    staff_no = _get_string_value(audit_info, "verifiStaffNo")
    if staff_no:
        employee["staffNo"] = staff_no
    user_id = _get_string_value(audit_info, "verifiUserId")
    if user_id:
        employee["userId"] = user_id
    phone = _get_string_value(audit_info, "verifiUserPhone")
    if phone:
        employee["phone"] = phone
    return employee


def _lookup_enterprise_info(
    service_data: dict[str, Any],
    provider: EnterpriseInfoProvider,
    saler_name: str | None,
) -> dict[str, Any] | None:
    """调用企查查 provider 查询企业工商信息，注入 serviceData.salerCompanyInfo。

    查询成功时写入包含 establishDate / address 的 dict；失败时写入 null，
    确保当前核销单内后续发票不再重复查询。
    """
    company_name = saler_name or ""
    if not company_name:
        service_data["salerCompanyInfo"] = None
        return None

    try:
        result = provider(company_name)
    except Exception:
        _logger.warning(
            "企查查企业信息查询异常，降级为无数据",
            extra={
                "event": "enterprise_info.provider_error",
                "company_name": company_name,
            },
            exc_info=True,
        )
        service_data["salerCompanyInfo"] = None
        return None

    if result is not None and isinstance(result, dict) and result.get("establishDate"):
        service_data["salerCompanyInfo"] = result
        _logger.info(
            "企查查企业信息已注入",
            extra={
                "event": "enterprise_info.injected",
                "company_name": company_name,
                "establish_date": result.get("establishDate"),
            },
        )
        return result
    else:
        service_data["salerCompanyInfo"] = None
        _logger.info(
            "企查查未查到企业信息或缺少注册日期，设为 null",
            extra={
                "event": "enterprise_info.not_found",
                "company_name": company_name,
            },
        )
        return None


@dataclass(slots=True)
class ReceiptDataPreparer:
    """规则执行前的数据准备层，统一编排文件、OCR 和多个上游业务服务。"""

    invoice_file_provider: InvoiceFileProvider = get_invoice_file_from_server
    ocr_provider: OCRProvider = call_ocr_service
    audit_info_provider: AuditInfoProvider = audit_client.fetch_audit_info
    company_blacklist_provider: CompanyBlacklistProvider = audit_client.fetch_company_blacklist
    company_list_provider: CompanyListProvider = audit_client.fetch_company_list
    expense_invoice_types_provider: ExpenseInvoiceTypesProvider = audit_client.fetch_expense_invoice_types
    invoice_info_provider: InvoiceInfoProvider = audit_client.fetch_invoice_info
    audit_invoice_files_provider: AuditInvoiceFilesProvider = audit_client.fetch_audit_invoice_files
    audit_invoice_file_info_provider: AuditInvoiceFileInfoProvider = audit_client.fetch_audit_invoice_file_info
    field_mappings_provider: FieldMappingsProvider = audit_client.fetch_field_mappings
    receipt_enrichers: Mapping[str, ReceiptEnricher] = field(default_factory=dict)
    extra_enrichers: Mapping[str, DataEnricher] = field(default_factory=dict)
    qichacha_provider: EnterpriseInfoProvider | None = None

    def prepare_receipt_context(
        self,
        receipt_code: str,
        *,
        receipt_enrichers_override: Mapping[str, ReceiptEnricher] | None = None,
    ) -> dict[str, Any]:
        _logger.info("开始聚合单据级外部数据", extra={"receipt_code": receipt_code, "event": "data_prep.aggregate.start"})
        audit_info = self.audit_info_provider(receipt_code)
        company_blacklist = self.company_blacklist_provider()
        company_list = self.company_list_provider()
        instance_code = _get_string_value(audit_info, "instanceCode") or receipt_code
        audit_invoice_files = self.audit_invoice_files_provider(instance_code, 0)
        audit_invoice_file_info = _collect_audit_invoice_file_info(
            audit_invoice_files,
            self.audit_invoice_file_info_provider,
        )
        truthcheck_field_mappings = {
            "bill": self.field_mappings_provider("bill"),
            "item": self.field_mappings_provider("item"),
        }

        service_data: dict[str, Any] = {
            "auditInfo": audit_info,
            "companyBlacklist": company_blacklist,
            "companyList": company_list,
            "expenseInvoiceTypes": self.expense_invoice_types_provider(
                _get_string_value(audit_info, "eiCode") or ""
            )
            if _get_string_value(audit_info, "eiCode") is not None
            else [],
            "auditInvoiceFiles": audit_invoice_files,
            "auditInvoiceFileInfo": audit_invoice_file_info,
            "truthCheckFieldMappings": truthcheck_field_mappings,
        }
        # 动态路由模式下，enricher 由 resolver 选中的 profile 决定，通过 override 传入
        active_enrichers = receipt_enrichers_override if receipt_enrichers_override is not None else self.receipt_enrichers
        for enricher_name, enricher in active_enrichers.items():
            service_data[enricher_name] = enricher(receipt_code, dict(service_data))
        invoice_files = _build_invoice_files(
            receipt_code,
            audit_invoice_files,
            audit_invoice_file_info,
        )

        _logger.info(
            "已完成单据级服务聚合",
            extra={"receipt_code": receipt_code, "event": "data_prep.aggregate.done", "service_keys": list(service_data.keys())},
        )
        return {
            "receiptCode": receipt_code,
            "serviceData": service_data,
            "invoiceFiles": invoice_files,
            "enterpriseInfoCache": {},
        }

    def prepare_invoice_input(
        self,
        receipt_code: str,
        invoice_file: Mapping[str, Any],
        receipt_context: Mapping[str, Any] | None = None,
        ocr_sample_path: Path | str = DEFAULT_OCR_PATH,
        *,
        include_current_invoice_metadata: bool = True,
        extra_enrichers_override: Mapping[str, DataEnricher] | None = None,
    ) -> dict[str, Any]:
        normalized_receipt_context = dict(receipt_context or self.prepare_receipt_context(receipt_code))
        receipt_service_data = dict(normalized_receipt_context.get("serviceData") or {})
        enterprise_info_cache = normalized_receipt_context.get("enterpriseInfoCache")
        if not isinstance(enterprise_info_cache, dict):
            enterprise_info_cache = {}
        audit_info = dict(receipt_service_data.get("auditInfo") or {})
        company_list = list(receipt_service_data.get("companyList") or [])
        file_path = _get_string_value(invoice_file, "filePath")
        if file_path is None:
            raise ValueError("invoice file path is required")
        file_name = _resolve_invoice_file_name(invoice_file)

        ocr_result = self.ocr_provider(
            file_path,
            ocr_sample_path,
            **_resolve_ocr_provider_kwargs(
                self.ocr_provider,
                receipt_code=receipt_code,
                audit_info=audit_info,
                company_list=company_list,
                file_name=file_name,
            ),
        )
        if not isinstance(ocr_result, Mapping):
            raise ValueError("ocr provider must return a mapping")

        ocr_envelope = _extract_ocr_envelope(ocr_result)
        ocr_data = _normalize_ocr_goods_name(_extract_normalized_ocr_data(ocr_result))
        cheque_no = _get_string_value(ocr_data, "chequeNo", "invoiceNo", "serialNo")
        instance_code = _get_string_value(audit_info, "instanceCode") or receipt_code
        accounting_code = _resolve_accounting_code(ocr_data, audit_info, company_list)
        if accounting_code is not None and _get_string_value(ocr_data, "accountingCode") is None:
            ocr_data = {**ocr_data, "accountingCode": accounting_code}

        generated_invoice_info_id = str(uuid.uuid4())
        generated_atcrid = str(uuid.uuid4())
        current_audit_invoice_file = dict(invoice_file.get("auditInvoiceFile") or {})
        current_audit_invoice_file_info = dict(invoice_file.get("auditInvoiceFileInfo") or {})
        invoice_usage_history: list[dict[str, Any]] = []
        if cheque_no is not None:
            try:
                invoice_usage_history = self.invoice_info_provider(cheque_no, instance_code, accounting_code)
            except Exception as exc:
                # 发票占用信息用于辅助校验，外部接口抖动时降级为空，避免整单失败。
                _logger.warning(
                    "发票占用信息查询失败，降级为空列表",
                    extra={
                        "event": "data_prep.invoice_info.fallback",
                        "receipt_code": receipt_code,
                        "instance_code": instance_code,
                        "cheque_no": cheque_no,
                        "accounting_code": accounting_code,
                        "error": str(exc),
                    },
                )

        service_data: dict[str, Any] = {
            **receipt_service_data,
            "invoiceUsageHistory": invoice_usage_history,
            "currentInvoiceInfo": {
                "aiiid": generated_invoice_info_id,
                "atcrid": generated_atcrid,
                "miInstanceCode": instance_code,
                "miApplyUserId": _get_string_value(audit_info, "verifiUserId"),
                "miApplyUserName": _get_string_value(audit_info, "verifiUserName"),
                "createTime": _get_string_value(current_audit_invoice_file, "createTime"),
            },
        }
        if include_current_invoice_metadata:
            service_data["currentAuditInvoiceFile"] = current_audit_invoice_file
            service_data["currentAuditInvoiceFileInfo"] = current_audit_invoice_file_info
        if ocr_envelope is not None:
            service_data["ocrEnvelope"] = ocr_envelope
        active_extra_enrichers = (
            extra_enrichers_override
            if extra_enrichers_override is not None
            else self.extra_enrichers
        )
        for service_name, enricher in active_extra_enrichers.items():
            service_data[service_name] = enricher(receipt_code, file_path, ocr_data, dict(service_data))

        # 企查查企业信息查询：OCR 完成后按当前发票的销方公司名称查询企业工商信息，
        # 将原始注册日期/地址注入该张发票的 serviceData，供图中规则使用。
        # 同一核销单内按公司名称复用结果，包括 None 失败结果；企查查接口不接受税号查询。
        if self.qichacha_provider is not None and "salerCompanyInfo" not in receipt_service_data:
            saler_name = _get_string_value(ocr_data, "salerName")
            if saler_name:
                if saler_name in enterprise_info_cache:
                    service_data["salerCompanyInfo"] = enterprise_info_cache[saler_name]
                else:
                    enterprise_info_cache[saler_name] = _lookup_enterprise_info(
                        service_data,
                        self.qichacha_provider,
                        saler_name,
                    )

        return build_rule_input(
            receipt_code=receipt_code,
            ocr_data=ocr_data,
            file_path=file_path,
            service_data=service_data,
        )

    def prepare(
        self,
        receipt_code: str,
        ocr_sample_path: Path | str = DEFAULT_OCR_PATH,
    ) -> dict[str, Any]:
        receipt_context = self.prepare_receipt_context(receipt_code)
        return self.prepare_invoice_input(
            receipt_code,
            receipt_context["invoiceFiles"][0],
            receipt_context,
            ocr_sample_path,
            include_current_invoice_metadata=False,
        )