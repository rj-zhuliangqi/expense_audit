import base64
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode, urlparse

import httpx
from dotenv import load_dotenv


from expense_audit_orchestrator.paths import PROJECT_ROOT
DEFAULT_KINGDEE_OCR_USER_TYPE = "UserName"
DEFAULT_KINGDEE_OCR_LANGUAGE = "zh-CN"
DEFAULT_KINGDEE_OCR_BILL_TYPE = "er_dailyreimbursebill"
DEFAULT_KINGDEE_OCR_VERIFY_FLAG = "1"
DEFAULT_KINGDEE_OCR_TIMEOUT = 30.0
_BASE64_FILE_PREFIX = "base64://"

_SUCCESS_ERROR_CODES = {"", "0", "0000"}
_FILE_TYPE_BY_SUFFIX = {
    ".pdf": "1",
    ".bmp": "2",
    ".gif": "2",
    ".jpeg": "2",
    ".jpg": "2",
    ".png": "2",
    ".tif": "2",
    ".tiff": "2",
    ".webp": "2",
    ".ofd": "4",
    ".doc": "5",
    ".docx": "5",
    ".xls": "6",
    ".xlsx": "6",
    ".ppt": "7",
    ".pptx": "7",
    ".txt": "8",
    ".xml": "9",
}
_ALLOWED_FILE_TYPES = frozenset(_FILE_TYPE_BY_SUFFIX.values())
_OCR_RESULT_HINTS = {
    "amount",
    "buyerTaxNo",
    "invoiceDate",
    "invoiceNo",
    "invoiceType",
    "items",
    "orgName",
}


def _get_string_value(data: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _normalize_expire_at(raw_value: Any) -> float:
    if isinstance(raw_value, (int, float)):
        expire_at = float(raw_value)
        if expire_at > 1_000_000_000_000:
            expire_at /= 1000.0
        return max(expire_at - 60.0, 0.0)
    return 0.0


def _build_url(base_url: str, path: str, query_params: Mapping[str, str] | None = None) -> str:
    suffix = ""
    if query_params:
        suffix = f"?{urlencode(query_params)}"
    return f"{base_url.rstrip('/')}{path}{suffix}"


def _resolve_company_info(
    audit_info: Mapping[str, Any] | None,
    company_list: Sequence[Mapping[str, Any]] | None,
) -> dict[str, str]:
    normalized_audit_info = audit_info or {}
    normalized_company_list = company_list or []
    company_name = _get_string_value(
        normalized_audit_info,
        "verifiUserCompanyName",
        "companyName",
        "companyFullName",
    )

    if company_name is not None:
        for company in normalized_company_list:
            if company_name in {
                _get_string_value(company, "cName"),
                _get_string_value(company, "cShortName"),
            }:
                tax_no = _get_string_value(company, "companyTax", "taxNo")
                if tax_no is not None:
                    return {"name": company_name, "taxNo": tax_no}

        audit_tax_no = _get_string_value(normalized_audit_info, "companyTax", "taxNo")
        if audit_tax_no is not None:
            return {"name": company_name, "taxNo": audit_tax_no}

    for company in normalized_company_list:
        resolved_name = _get_string_value(company, "cName", "cShortName")
        resolved_tax_no = _get_string_value(company, "companyTax", "taxNo")
        if resolved_name is not None and resolved_tax_no is not None:
            return {"name": resolved_name, "taxNo": resolved_tax_no}

    return {}


def _extract_file_suffix(source: str | None) -> str | None:
    if source is None:
        return None

    normalized = source.strip()
    if not normalized or normalized.startswith(_BASE64_FILE_PREFIX):
        return None

    candidate_path = normalized
    if normalized.startswith(("http://", "https://")):
        candidate_path = urlparse(normalized).path

    suffix = Path(candidate_path).suffix.lower()
    if not suffix:
        return None

    return suffix


def _resolve_file_type(
    file_path: str,
    configured_value: str | None = None,
    *,
    file_name: str | None = None,
) -> str:
    normalized_configured_value = None
    if configured_value is not None and configured_value.strip():
        normalized_configured_value = configured_value.strip()
        if normalized_configured_value not in _ALLOWED_FILE_TYPES:
            allowed_values = ", ".join(sorted(_ALLOWED_FILE_TYPES))
            raise ValueError(f"kingdee file type override must be one of: {allowed_values}")

    for source in (file_name, file_path):
        suffix = _extract_file_suffix(source)
        if suffix is None:
            continue

        file_type = _FILE_TYPE_BY_SUFFIX.get(suffix)
        if file_type is None:
            raise ValueError(f"kingdee file type unsupported for suffix: {suffix}")
        return file_type

    if normalized_configured_value is not None:
        return normalized_configured_value

    raise ValueError("kingdee file type could not be determined from file suffix")


def _extract_error_code(payload: Mapping[str, Any]) -> str | None:
    error_code = _get_string_value(payload, "errorCode")
    if error_code is not None:
        return error_code

    data = payload.get("data")
    if isinstance(data, Mapping):
        return _get_string_value(data, "errorCode")

    return None


def _extract_error_message(payload: Mapping[str, Any]) -> str | None:
    error_message = _get_string_value(payload, "errorDesc", "message")
    if error_message is not None:
        return error_message

    data = payload.get("data")
    if isinstance(data, Mapping):
        return _get_string_value(data, "errorDesc", "message")

    return None


def _ensure_success(payload: Mapping[str, Any], action: str) -> None:
    error_code = _extract_error_code(payload)
    if error_code not in (None, *_SUCCESS_ERROR_CODES):
        raise ValueError(f"kingdee {action} failed: {_extract_error_message(payload) or error_code}")

    status = payload.get("status")
    if status is False:
        raise ValueError(f"kingdee {action} failed: {_extract_error_message(payload) or 'status=false'}")

    state = _get_string_value(payload, "state")
    if state is not None and state.lower() not in {"success", "true"}:
        raise ValueError(f"kingdee {action} failed: {_extract_error_message(payload) or state}")


def _extract_dict_candidates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add_candidate(value: Any) -> None:
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    candidates.append(item)

    add_candidate(payload)
    data = payload.get("data")
    add_candidate(data)

    if isinstance(data, Mapping):
        for key in ("ocrResult", "result", "ocr", "recognitionResult", "invoice", "invoiceInfo"):
            add_candidate(data.get(key))

    for key in ("ocrResult", "result", "ocr", "recognitionResult"):
        add_candidate(payload.get(key))

    return candidates


def _extract_ocr_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    candidates = _extract_dict_candidates(payload)
    for candidate in candidates:
        if _OCR_RESULT_HINTS.intersection(candidate):
            return candidate

    for candidate in candidates:
        if candidate:
            return candidate

    raise ValueError("kingdee recognition response missing ocr result")


def _load_project_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)


class KingdeeOCRProvider:
    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        account_id: str,
        tenant_id: str,
        user: str,
        user_type: str = DEFAULT_KINGDEE_OCR_USER_TYPE,
        language: str = DEFAULT_KINGDEE_OCR_LANGUAGE,
        bill_type: str = DEFAULT_KINGDEE_OCR_BILL_TYPE,
        verify_flag: str = DEFAULT_KINGDEE_OCR_VERIFY_FLAG,
        timeout: float = DEFAULT_KINGDEE_OCR_TIMEOUT,
        upload_file_type: str | None = None,
        recognition_file_type: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._app_id = app_id
        self._app_secret = app_secret
        self._account_id = account_id
        self._tenant_id = tenant_id
        self._user = user
        self._user_type = user_type
        self._language = language
        self._bill_type = bill_type
        self._verify_flag = verify_flag
        self._timeout = timeout
        self._upload_file_type = upload_file_type
        self._recognition_file_type = recognition_file_type
        self._app_token: str | None = None
        self._app_token_expire_at = 0.0
        self._access_token: str | None = None
        self._access_token_expire_at = 0.0

    def __call__(
        self,
        file_path: str,
        ocr_sample_path: Path | str | None = None,
        *,
        receipt_code: str | None = None,
        audit_info: Mapping[str, Any] | None = None,
        company_list: Sequence[Mapping[str, Any]] | None = None,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        del ocr_sample_path

        company_info = _resolve_company_info(audit_info, company_list)
        if not company_info:
            company_info = None
        started_at = datetime.now(timezone.utc).isoformat()
        file_base64 = self._read_file_base64(file_path)
        upload_file_type = _resolve_file_type(file_path, self._upload_file_type, file_name=file_name)
        recognition_file_type = _resolve_file_type(
            file_path,
            self._recognition_file_type or upload_file_type,
            file_name=file_name,
        )

        with httpx.Client(timeout=self._timeout) as client:
            app_token = self._get_app_token(client)
            access_token = self._get_access_token(client, app_token)
            file_down_url = self._upload_file(client, access_token, file_base64, upload_file_type)
            recognition_payload = self._recognition_check(
                client,
                access_token,
                file_down_url,
                recognition_file_type,
                company_info,
            )

        normalized_ocr_result = _extract_ocr_result(recognition_payload)
        finished_at = datetime.now(timezone.utc).isoformat()
        return {
            "provider": "kingdee",
            "request": {
                "receiptCode": receipt_code,
                "fileName": file_name,
                "filePath": file_path,
            },
            "upload": {
                "fileType": upload_file_type,
                "fileDownUrl": file_down_url,
            },
            "recognition": {
                "rawPayload": recognition_payload,
                "normalized": normalized_ocr_result,
            },
            "status": {
                "code": _extract_error_code(recognition_payload) or "200",
                "message": _extract_error_message(recognition_payload)
                or _get_string_value(normalized_ocr_result, "description", "validateMessage")
                or "success",
                "startedAt": started_at,
                "finishedAt": finished_at,
            },
        }

    def _read_file_base64(self, file_path: str) -> str:
        if file_path.startswith(_BASE64_FILE_PREFIX):
            payload = file_path.removeprefix(_BASE64_FILE_PREFIX).strip()
            if not payload:
                raise ValueError("kingdee base64 payload is empty")
            return payload

        path = Path(file_path)
        if path.exists():
            return base64.b64encode(path.read_bytes()).decode("ascii")

        if file_path.startswith(("http://", "https://")):
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(file_path)
            if response.status_code < 200 or response.status_code >= 300:
                raise ValueError(f"kingdee file download failed: {response.status_code} {response.text}")
            return base64.b64encode(response.content).decode("ascii")

        raise FileNotFoundError(f"ocr file not found: {file_path}")

    def _post_json(self, client: httpx.Client, url: str, payload: dict[str, Any], action: str) -> dict[str, Any]:
        response = client.post(url, json=payload)
        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError(f"kingdee {action} request failed: {response.status_code} {response.text}")

        try:
            normalized_payload = response.json()
        except Exception as exc:
            raise ValueError(f"kingdee {action} returned invalid json: {exc}") from exc

        if not isinstance(normalized_payload, dict):
            raise ValueError(f"kingdee {action} returned invalid payload")

        _ensure_success(normalized_payload, action)
        return normalized_payload

    def _get_app_token(self, client: httpx.Client) -> str:
        if self._app_token is not None and time.time() < self._app_token_expire_at:
            return self._app_token

        payload = self._post_json(
            client,
            _build_url(self._base_url, "/api/getAppToken.do"),
            {
                "appId": self._app_id,
                "appSecret": self._app_secret,
                "accountId": self._account_id,
                "tenantId": self._tenant_id,
                "language": self._language,
            },
            "get app token",
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("kingdee get app token returned invalid payload")

        app_token = _get_string_value(data, "appToken", "app_token")
        if app_token is None:
            raise ValueError("kingdee get app token returned no appToken")

        self._app_token = app_token
        self._app_token_expire_at = _normalize_expire_at(
            _get_string_value(data, "expireTime", "expire_time")
        )
        return app_token

    def _get_access_token(self, client: httpx.Client, app_token: str) -> str:
        if self._access_token is not None and time.time() < self._access_token_expire_at:
            return self._access_token

        payload = self._post_json(
            client,
            _build_url(self._base_url, "/api/login.do"),
            {
                "user": self._user,
                "usertype": self._user_type,
                "apptoken": app_token,
                "accountId": self._account_id,
            },
            "login",
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("kingdee login returned invalid payload")

        access_token = _get_string_value(data, "accessToken", "access_token")
        if access_token is None:
            raise ValueError("kingdee login returned no accessToken")

        self._access_token = access_token
        self._access_token_expire_at = _normalize_expire_at(
            _get_string_value(data, "expireTime", "expire_time")
        )
        return access_token

    def _upload_file(
        self,
        client: httpx.Client,
        access_token: str,
        file_base64: str,
        file_type: str,
    ) -> str:
        payload = self._post_json(
            client,
            _build_url(
                self._base_url,
                "/kapi/app/rim/message",
                {"access_token": access_token},
            ),
            {
                "data": {
                    "base64": file_base64,
                    "fileType": file_type,
                },
                "messageId": str(uuid.uuid4()),
                "messageType": "uploadFile",
            },
            "upload file",
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("kingdee upload file returned invalid payload")

        file_down_url = _get_string_value(data, "fileDownUrl")
        if file_down_url is None:
            raise ValueError("kingdee upload file returned no fileDownUrl")

        return file_down_url

    def _recognition_check(
        self,
        client: httpx.Client,
        access_token: str,
        file_down_url: str,
        file_type: str,
        company_info: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "data": {
                "billType": self._bill_type,
                "fileDownUrl": file_down_url,
                "fileType": file_type,
                "verifyFlag": self._verify_flag,
            },
            "messageId": str(uuid.uuid4()),
            "messageType": "recognitionCheck",
        }
        if company_info:
            payload["data"]["companyInfo"] = dict(company_info)
        return self._post_json(
            client,
            _build_url(
                self._base_url,
                "/kapi/app/rim/message",
                {"access_token": access_token},
            ),
            payload,
            "recognition check",
        )


def create_kingdee_ocr_provider_from_env() -> KingdeeOCRProvider:
    _load_project_env()

    required_env_keys = (
        "KINGDEE_OCR_BASE_URL",
        "KINGDEE_OCR_APP_ID",
        "KINGDEE_OCR_APP_SECRET",
        "KINGDEE_OCR_ACCOUNT_ID",
        "KINGDEE_OCR_TENANT_ID",
        "KINGDEE_OCR_USER",
    )
    resolved_env = {
        key: (os.getenv(key) or "").strip()
        for key in required_env_keys
    }
    missing_keys = [key for key, value in resolved_env.items() if not value]
    if missing_keys:
        missing_text = ", ".join(missing_keys)
        raise ValueError(f"kingdee ocr provider is required but missing env: {missing_text}")

    base_url = resolved_env["KINGDEE_OCR_BASE_URL"]
    app_id = resolved_env["KINGDEE_OCR_APP_ID"]
    app_secret = resolved_env["KINGDEE_OCR_APP_SECRET"]
    account_id = resolved_env["KINGDEE_OCR_ACCOUNT_ID"]
    tenant_id = resolved_env["KINGDEE_OCR_TENANT_ID"]
    user = resolved_env["KINGDEE_OCR_USER"]

    timeout_raw = (os.getenv("KINGDEE_OCR_TIMEOUT") or "").strip()
    timeout = DEFAULT_KINGDEE_OCR_TIMEOUT
    if timeout_raw:
        timeout = float(timeout_raw)

    return KingdeeOCRProvider(
        base_url=base_url,
        app_id=app_id,
        app_secret=app_secret,
        account_id=account_id,
        tenant_id=tenant_id,
        user=user,
        user_type=(os.getenv("KINGDEE_OCR_USER_TYPE") or DEFAULT_KINGDEE_OCR_USER_TYPE).strip(),
        language=(os.getenv("KINGDEE_OCR_LANGUAGE") or DEFAULT_KINGDEE_OCR_LANGUAGE).strip(),
        bill_type=(os.getenv("KINGDEE_OCR_BILL_TYPE") or DEFAULT_KINGDEE_OCR_BILL_TYPE).strip(),
        verify_flag=(os.getenv("KINGDEE_OCR_VERIFY_FLAG") or DEFAULT_KINGDEE_OCR_VERIFY_FLAG).strip(),
        timeout=timeout,
        upload_file_type=(os.getenv("KINGDEE_OCR_UPLOAD_FILE_TYPE") or "").strip() or None,
        recognition_file_type=(os.getenv("KINGDEE_OCR_RECOGNITION_FILE_TYPE") or "").strip() or None,
    )


__all__ = [
    "DEFAULT_KINGDEE_OCR_BILL_TYPE",
    "DEFAULT_KINGDEE_OCR_LANGUAGE",
    "DEFAULT_KINGDEE_OCR_TIMEOUT",
    "DEFAULT_KINGDEE_OCR_USER_TYPE",
    "DEFAULT_KINGDEE_OCR_VERIFY_FLAG",
    "KingdeeOCRProvider",
    "create_kingdee_ocr_provider_from_env",
]