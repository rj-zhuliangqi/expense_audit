import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from dotenv import dotenv_values, load_dotenv


DEFAULT_AUDIT_SERVICE_URL = "http://127.0.0.1:8080"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_INVOICE_FILE_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "User-Agent": "PostmanRuntime-ApipostRuntime/1.1.0",
}
AUDIT_INVOICE_FILE_SYSID_ENV = "AUDIT_INVOICE_FILE_SYSID"
AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET_ENV = "AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET"
_PROJECT_ENV_LOADED = False


def fetch_audit_info(
    instance_code: str,
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """调用核销单查询接口，补齐规则执行前需要的业务上下文。"""
    data = _fetch_service_data(
        f"/api/audit-service/audit/info/{quote(instance_code, safe='')}",
        service_url=service_url,
        timeout=timeout,
        description="核销单信息",
        headers=_build_auth_headers(),
    )
    if not isinstance(data, dict):
        raise ValueError("audit info service returned invalid payload")

    return data


def fetch_company_blacklist(
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """获取供应商黑名单数据。"""
    data = _fetch_service_data(
        "/api/audit-service/audit/company-black-list",
        service_url=service_url,
        timeout=timeout,
        description="供应商黑名单",
        headers=_build_auth_headers(),
    )
    return _expect_list_payload(data, "company blacklist")


def fetch_company_list(
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """获取财务公司清单。"""
    data = _fetch_service_data(
        "/api/audit-service/audit/company-list",
        service_url=service_url,
        timeout=timeout,
        description="财务公司清单",
        headers=_build_auth_headers(),
    )
    return _expect_list_payload(data, "company list")


def fetch_expense_invoice_types(
    ei_code: str,
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """按费用项编码获取允许的发票类型。"""
    paths = [
        "/api/audit-service/audit/company-list",
        "/api/audit-service/audit/companylist",
    ]
    last_http_error: HTTPError | None = None
    for path in paths:
        try:
            data = _fetch_service_data(
                path,
                service_url=service_url,
                timeout=timeout,
                description="费用项发票类型",
                query_params={"eiCode": ei_code},
                headers=_build_auth_headers(),
            )
            return _expect_list_payload(data, "expense invoice types")
        except HTTPError as exc:
            if exc.code != 404 or path == paths[-1]:
                raise
            last_http_error = exc

    if last_http_error is not None:
        raise last_http_error

    raise ValueError("expense invoice types service returned invalid payload")


def fetch_field_mappings(
    belong_table: str,
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """按所属表获取金蝶和费用字段映射。"""
    normalized_belong_table = belong_table.strip()
    data = _fetch_service_data(
        f"/api/audit-service/audit/fie-id-mapping/{quote(normalized_belong_table, safe='')}",
        service_url=service_url,
        timeout=timeout,
        description="字段映射",
        headers=_build_auth_headers(),
    )
    return _expect_list_payload(data, "field mappings")


def fetch_invoice_info(
    cheque_no: str,
    instance_code: str,
    accounting_code: str | None = None,
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """查询发票占用信息。"""
    data = _fetch_service_data(
        "/api/audit-service/audit/invoice-info",
        service_url=service_url,
        timeout=timeout,
        description="发票占用信息",
        query_params={
            "chequeNo": cheque_no,
            "instanceCode": instance_code,
            "accountingCode": accounting_code,
        },
        headers=_build_auth_headers(),
    )
    return _expect_list_payload(data, "invoice info")


def fetch_audit_invoice_files(
    instance_code: str,
    a_type: int = 0,
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
    headers: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """按核销单号获取关联的稽核发票文件列表。"""
    data = _fetch_service_data(
        "/api/audit-service/audit/invoice-file",
        service_url=service_url,
        timeout=timeout,
        description="稽核发票文件",
        query_params={
            "instanceCode": instance_code,
            "aType": a_type,
        },
        headers=_build_auth_headers(extra_headers=headers),
    )
    return _expect_list_payload(data, "audit invoice files")


def fetch_audit_invoice_file_info(
    fid: str,
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
    headers: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """按文件系统 fid 获取发票文件详情。"""
    data = _fetch_service_data(
        f"/api/audit-service/audit/invoice-file-info/{quote(fid, safe='')}",
        service_url=service_url,
        timeout=timeout,
        description="稽核发票文件详情",
        headers=_build_auth_headers(extra_headers=headers),
    )
    return _expect_list_or_single_mapping_payload(data, "audit invoice file info")


def fetch_audit_task_info_list(
    instance_code: str,
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
    headers: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """按核销单号获取稽核任务列表。"""
    data = _fetch_service_data(
        f"/api/audit-service/audit/task-info-list/{quote(instance_code, safe='')}",
        service_url=service_url,
        timeout=timeout,
        description="稽核任务列表",
        headers=_build_auth_headers(extra_headers=headers),
    )
    return _expect_list_payload(data, "audit task info list")


def update_audit_task_status(
    instance_code: str,
    *,
    new_status: int,
    system_identifier: int,
    service_url: str = DEFAULT_AUDIT_SERVICE_URL,
    timeout: float = 5.0,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """更新稽核任务状态。"""
    return _post_service_payload(
        "/api/audit-service/audit/task-status-update",
        service_url=service_url,
        timeout=timeout,
        description="稽核任务状态更新",
        payload={
            "instanceCode": instance_code,
            "newStatus": new_status,
            "systemIdentifier": system_identifier,
        },
        headers=_build_auth_headers({"Content-Type": "application/json", **(dict(headers) if headers else {})}),
    )


def _get_env_header_value(env_key: str) -> str | None:
    value = (os.getenv(env_key) or "").strip()
    return value or None


def _get_first_env_value(*env_keys: str) -> str | None:
    for env_key in env_keys:
        value = _get_env_header_value(env_key)
        if value is not None:
            return value
    return None


def _load_project_env() -> None:
    global _PROJECT_ENV_LOADED

    if _PROJECT_ENV_LOADED:
        return

    env_path = PROJECT_ROOT / ".env"
    env_values = dotenv_values(env_path)

    load_dotenv(env_path, override=False)

    for key, value in env_values.items():
        if value is None:
            continue

        current_value = os.getenv(key)
        if current_value is None or not current_value.strip():
            os.environ[key] = value

    _PROJECT_ENV_LOADED = True


def _current_timestamp_millis() -> int:
    return int(time.time() * 1000)


def _build_sign_server_auth(sysid: str, access_key_secret: str) -> str:
    timestamp = _current_timestamp_millis()
    digest = hashlib.md5(f"{sysid}{timestamp}{access_key_secret}".encode("utf-8")).hexdigest().upper()
    return f"{sysid}|{timestamp}|{digest}"


def _build_auth_headers(extra_headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """构建带认证信息的请求头。"""
    _load_project_env()
    resolved_headers = dict(DEFAULT_AUDIT_INVOICE_FILE_HEADERS)

    sysid = _get_first_env_value(AUDIT_INVOICE_FILE_SYSID_ENV, "SYS_ID")
    if sysid is not None:
        resolved_headers["sysid"] = sysid

    access_key_secret = _get_first_env_value(
        AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET_ENV,
        "AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET_ENV",
        "ACCESS_KEY_SECRET",
        "accessKeySecret",
    )
    if sysid is not None and access_key_secret is not None:
        resolved_headers["sign-server-auth"] = _build_sign_server_auth(sysid, access_key_secret)

    if extra_headers:
        for key, value in extra_headers.items():
            normalized_key = str(key).strip()
            normalized_value = str(value).strip()
            if normalized_key and normalized_value:
                resolved_headers[normalized_key] = normalized_value

    return resolved_headers


def _fetch_service_data(
    path: str,
    *,
    service_url: str,
    timeout: float,
    description: str,
    query_params: Mapping[str, str | int | None] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Any:
    query_string = ""
    if query_params:
        normalized_query = {
            key: value
            for key, value in query_params.items()
            if value is not None and str(value).strip()
        }
        if normalized_query:
            query_string = f"?{urlencode(normalized_query)}"

    endpoint = f"{service_url.rstrip('/')}{path}{query_string}"
    print(f"[核销单服务] 查询{description}: {endpoint}")

    request: str | Request = endpoint
    if headers:
        request = Request(endpoint, headers=dict(headers), method="GET")

    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)

    if not _is_success_payload(payload):
        raise ValueError(_get_service_error_message(payload, description))

    if "data" not in payload:
        raise ValueError(f"{description} service returned invalid payload")

    return payload["data"]


def _post_service_payload(
    path: str,
    *,
    service_url: str,
    timeout: float,
    description: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    endpoint = f"{service_url.rstrip('/')}{path}"
    print(f"[核销单服务] 提交{description}: {endpoint}")

    request = Request(
        endpoint,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        headers=dict(headers or {}),
        method="POST",
    )

    with urlopen(request, timeout=timeout) as response:
        response_payload = json.load(response)

    if not _is_success_payload(response_payload):
        raise ValueError(_get_service_error_message(response_payload, description))

    if not isinstance(response_payload, dict):
        raise ValueError(f"{description} service returned invalid payload")

    return response_payload


def _is_success_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False

    if "code" in payload:
        return str(payload.get("code")).strip() == "0"

    if "status" in payload:
        return str(payload.get("status")).strip() in {"0", "200"}

    return False


def _get_service_error_message(payload: Any, description: str) -> str:
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("err")
        if isinstance(message, str) and message.strip():
            return message

    return f"{description} service returned a failure response"


def _expect_list_payload(data: Any, service_name: str) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ValueError(f"{service_name} service returned invalid payload")
    return data


def _expect_list_or_single_mapping_payload(data: Any, service_name: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        return [data]

    raise ValueError(f"{service_name} service returned invalid payload")


__all__ = [
    "AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET_ENV",
    "AUDIT_INVOICE_FILE_SYSID_ENV",
    "DEFAULT_AUDIT_SERVICE_URL",
    "DEFAULT_AUDIT_INVOICE_FILE_HEADERS",
    "fetch_audit_info",
    "fetch_audit_invoice_file_info",
    "fetch_audit_invoice_files",
    "fetch_audit_task_info_list",
    "fetch_company_blacklist",
    "fetch_company_list",
    "fetch_expense_invoice_types",
    "fetch_field_mappings",
    "fetch_invoice_info",
    "update_audit_task_status",
]