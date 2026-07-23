import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .application import ReceiptResultSink
from .audit_client import (
    DEFAULT_AUDIT_SERVICE_URL,
    _build_auth_headers,
    _get_service_error_message,
    _is_success_payload,
)
from .observability import get_logger


_logger = get_logger("writeback")
from .writeback import (
    AuditTravelsBuilder,
    ComplianceRule,
    FormBuilder,
    assemble_result_audit_info,
)


AUDIT_INFO_SAVE_PATH = "/api/audit-service/audit/audit-info-save"
DEFAULT_WRITEBACK_TIMEOUT = 30.0
DEFAULT_WRITEBACK_MAX_RETRIES = 2
DEFAULT_WRITEBACK_RETRY_BACKOFF_SECONDS = 0.5
WRITEBACK_TIMEOUT_ENV = "WRITEBACK_TIMEOUT"
WRITEBACK_MAX_RETRIES_ENV = "WRITEBACK_MAX_RETRIES"
WRITEBACK_RETRY_BACKOFF_SECONDS_ENV = "WRITEBACK_RETRY_BACKOFF_SECONDS"


class AuditInfoWritebackClient:
    def __init__(
        self,
        *,
        service_url: str = DEFAULT_AUDIT_SERVICE_URL,
        timeout: float = DEFAULT_WRITEBACK_TIMEOUT,
        save_path: str = AUDIT_INFO_SAVE_PATH,
    ) -> None:
        self._service_url = service_url.rstrip("/")
        self._timeout = timeout
        self._save_path = save_path if save_path.startswith("/") else f"/{save_path}"

    def save_result_audit_info(
        self,
        payload: Mapping[str, Any],
        *,
        save_path: str | None = None,
    ) -> dict[str, Any]:
        # 动态路由模式下，save_path 可按单据的 profile 决定；默认用构造时的 self._save_path
        resolved_save_path = self._save_path
        if save_path is not None:
            resolved_save_path = save_path if save_path.startswith("/") else f"/{save_path}"
        endpoint = f"{self._service_url}{resolved_save_path}"
        _logger.info("回写稽核结果", extra={"event": "writeback.save", "endpoint": endpoint})

        response_payload = self._post_payload(endpoint, payload)

        if not _is_success_payload(response_payload):
            raise ValueError(_get_service_error_message(response_payload, "回写稽核结果"))

        if not isinstance(response_payload, dict):
            raise ValueError("回写稽核结果 service returned invalid payload")

        return response_payload

    def _post_payload(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
        max_retries = _resolve_int_env(WRITEBACK_MAX_RETRIES_ENV, DEFAULT_WRITEBACK_MAX_RETRIES)
        backoff_seconds = _resolve_float_env(
            WRITEBACK_RETRY_BACKOFF_SECONDS_ENV,
            DEFAULT_WRITEBACK_RETRY_BACKOFF_SECONDS,
        )
        request_payload = dict(payload)
        used_advice_fallback = False

        for attempt in range(max_retries + 1):
            request = Request(
                endpoint,
                data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
                headers=_build_auth_headers({"Content-Type": "application/json"}),
                method="POST",
            )

            try:
                with urlopen(request, timeout=self._timeout) as response:
                    return json.load(response)
            except HTTPError as exc:
                response_text = exc.read().decode("utf-8", errors="replace").strip()

                if (
                    not used_advice_fallback
                    and exc.code >= 500
                    and "aiAuditAdvice" in request_payload
                ):
                    used_advice_fallback = True
                    request_payload = {
                        key: value
                        for key, value in request_payload.items()
                        if key != "aiAuditAdvice"
                    }
                    _logger.warning(
                        "回写服务端异常，移除 aiAuditAdvice 后重试",
                        extra={
                            "event": "writeback.retry.fallback",
                            "endpoint": endpoint,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "status_code": exc.code,
                        },
                    )
                    continue

                if attempt < max_retries and _is_retryable_writeback_error(exc):
                    retry_delay = backoff_seconds * (2 ** attempt)
                    _logger.warning(
                        "回写接口请求超时或临时失败，准备重试",
                        extra={
                            "event": "writeback.retry",
                            "endpoint": endpoint,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "retry_delay_seconds": retry_delay,
                            "timeout_seconds": self._timeout,
                            "error_type": type(exc).__name__,
                            "status_code": exc.code,
                        },
                    )
                    if retry_delay > 0:
                        time.sleep(retry_delay)
                    continue

                if response_text:
                    raise ValueError(f"回写稽核结果 HTTP {exc.code}: {response_text}") from exc
                raise ValueError(f"回写稽核结果 HTTP {exc.code}") from exc
            except (URLError, TimeoutError) as exc:
                if attempt >= max_retries:
                    raise

                retry_delay = backoff_seconds * (2 ** attempt)
                _logger.warning(
                    "回写接口请求超时或临时失败，准备重试",
                    extra={
                        "event": "writeback.retry",
                        "endpoint": endpoint,
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "retry_delay_seconds": retry_delay,
                        "timeout_seconds": self._timeout,
                        "error_type": type(exc).__name__,
                    },
                )
                if retry_delay > 0:
                    time.sleep(retry_delay)

        raise RuntimeError("unreachable")


def _resolve_float_env(env_name: str, default: float) -> float:
    raw_value = (os.getenv(env_name) or "").strip()
    if not raw_value:
        return default

    try:
        return float(raw_value)
    except ValueError:
        _logger.warning(
            "invalid writeback float env, using default",
            extra={"event": "writeback.env.invalid", "env": env_name, "value": raw_value, "default": default},
        )
        return default


def _resolve_int_env(env_name: str, default: int) -> int:
    raw_value = (os.getenv(env_name) or "").strip()
    if not raw_value:
        return default

    try:
        return int(raw_value)
    except ValueError:
        _logger.warning(
            "invalid writeback int env, using default",
            extra={"event": "writeback.env.invalid", "env": env_name, "value": raw_value, "default": default},
        )
        return default


def _is_retryable_writeback_error(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code in {408, 429, 500, 502, 503, 504}

    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            return True
        if isinstance(reason, OSError):
            return True
        return False

    return isinstance(exc, TimeoutError)


WritebackStrategy = Callable[..., dict[str, Any]]


def build_writeback_payload(
    receipt_result: Mapping[str, Any],
    *,
    compliance_rule: ComplianceRule | None = None,
    audit_travels_builder: AuditTravelsBuilder | None = None,
    form_invoice_tax_views_builder: FormBuilder | None = None,
) -> dict[str, Any]:
    prepared_receipt = _build_prepared_receipt_from_result(receipt_result)
    kwargs: dict[str, Any] = {}
    if compliance_rule is not None:
        kwargs["compliance_rule"] = compliance_rule
    if audit_travels_builder is not None:
        kwargs["audit_travels_builder"] = audit_travels_builder
    if form_invoice_tax_views_builder is not None:
        kwargs["form_invoice_tax_views_builder"] = form_invoice_tax_views_builder
    return assemble_result_audit_info(prepared_receipt, receipt_result, **kwargs)


def build_receipt_writeback_sink(
    client: AuditInfoWritebackClient,
    *,
    compliance_rule: ComplianceRule | None = None,
    audit_travels_builder: AuditTravelsBuilder | None = None,
    form_invoice_tax_views_builder: FormBuilder | None = None,
) -> ReceiptResultSink:
    def sink(receipt_result: dict[str, Any]) -> None:
        payload = build_writeback_payload(
            receipt_result,
            compliance_rule=compliance_rule,
            audit_travels_builder=audit_travels_builder,
            form_invoice_tax_views_builder=form_invoice_tax_views_builder,
        )
        client.save_result_audit_info(payload)

    return sink


def build_receipt_writeback_file_sink(
    output_dir: Path | str,
    *,
    compliance_rule: ComplianceRule | None = None,
    audit_travels_builder: AuditTravelsBuilder | None = None,
    form_invoice_tax_views_builder: FormBuilder | None = None,
) -> ReceiptResultSink:
    resolved_output_dir = Path(output_dir)

    def sink(receipt_result: dict[str, Any]) -> None:
        receipt_code = str(receipt_result.get("receiptCode") or "unknown")
        payload = build_writeback_payload(
            receipt_result,
            compliance_rule=compliance_rule,
            audit_travels_builder=audit_travels_builder,
            form_invoice_tax_views_builder=form_invoice_tax_views_builder,
        )
        output_file = resolved_output_dir / f"{receipt_code}.writeback-payload.json"
        _export_json_payload(payload, output_file, "writeback payload")

    return sink


def _build_prepared_receipt_from_result(receipt_result: Mapping[str, Any]) -> dict[str, Any]:
    invoice_preparations: list[dict[str, Any]] = []
    for invoice_result in receipt_result.get("invoiceResults") or []:
        if not isinstance(invoice_result, Mapping):
            continue
        invoice_preparations.append(
            {
                "invoiceKey": str(invoice_result.get("invoiceKey") or ""),
                "invoiceFile": _resolve_mapping(invoice_result.get("invoiceFile")),
                "preparedInput": _resolve_mapping(invoice_result.get("preparedInput")),
            }
        )

    return {
        "receiptCode": str(receipt_result.get("receiptCode") or ""),
        "serviceData": _resolve_mapping(receipt_result.get("serviceData")),
        "invoicePreparations": invoice_preparations,
    }


def _resolve_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _export_json_payload(payload: dict[str, Any], output_path: Path, label: str) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    _logger.info("exported payload", extra={"event": "writeback.exported", "label": label, "output_path": str(output_path)})
    return output_path


__all__ = [
    "AUDIT_INFO_SAVE_PATH",
    "AuditInfoWritebackClient",
    "build_receipt_writeback_file_sink",
    "build_receipt_writeback_sink",
    "build_writeback_payload",
]