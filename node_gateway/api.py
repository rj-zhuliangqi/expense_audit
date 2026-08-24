import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError


from expense_audit_orchestrator.paths import PROJECT_ROOT
NODE_GATEWAY_LLM_EVALUATE_PATH = "/api/v1/node-gateway/llm/evaluate"


def _load_project_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)


class EvaluateLlmRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    prompt: str = Field(min_length=1)
    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    model: str | None = None
    temperature: float = 0
    max_retries: int | None = Field(default=None, alias="maxRetries")
    run_id: str | None = Field(default=None, alias="runId")
    receipt_code: str | None = Field(default=None, alias="receiptCode")
    invoice_key: str | None = Field(default=None, alias="invoiceKey")


class EvaluateLlmResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    llm_status: str = Field(alias="llmStatus")
    llm_result: dict[str, Any] | None = Field(default=None, alias="llmResult")
    raw_content: str | None = Field(default=None, alias="rawContent")
    error_message: str | None = Field(default=None, alias="errorMessage")
    error_type: str | None = Field(default=None, alias="errorType")
    attempts: int | None = None
    max_retries: int | None = Field(default=None, alias="maxRetries")
    upstream_status: int | None = Field(default=None, alias="upstreamStatus")


def _strip_markdown_json_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```json"):
        text = text[len("```json") :].strip()
    elif text.startswith("```"):
        text = text[len("```") :].strip()

    if text.endswith("```"):
        text = text[:-3].strip()

    return text


def _resolve_retry_count(requested: int | None) -> int:
    if isinstance(requested, int):
        return max(0, min(requested, 5))

    raw_retry = os.getenv("LLM_MAX_RETRIES", "2").strip()
    try:
        parsed = int(raw_retry)
    except ValueError:
        parsed = 2
    return max(0, min(parsed, 5))


def _resolve_retry_backoff_seconds() -> float:
    raw_backoff = os.getenv("LLM_RETRY_BACKOFF_SECONDS", "0.5").strip()
    try:
        parsed = float(raw_backoff)
    except ValueError:
        parsed = 0.5
    if parsed < 0:
        return 0.0
    return min(parsed, 30.0)


def _compact_error_text(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _is_retryable_status(status_code: int) -> bool:
    # 4xx 仅重试请求超时和限流；认证、权限、请求参数错误重试没有意义。
    return status_code in {408, 425, 429, 500, 502, 503, 504}


def _is_retryable_exception(error: Exception) -> bool:
    # 只对网络传输/超时类异常重试；配置错误、非法 URL 等确定性错误直接返回。
    return isinstance(error, httpx.TransportError)


def _build_error_message(
    *,
    error_type: str,
    detail: str | None,
    attempts: int,
    max_retries: int,
    upstream_status: int | None = None,
    request: EvaluateLlmRequest | None = None,
) -> str:
    parts = [
        "LLM调用失败",
        f"error_type={error_type}",
        f"attempts={attempts}",
        f"retries={max_retries}",
    ]
    if upstream_status is not None:
        parts.append(f"upstream_status={upstream_status}")
    if request is not None:
        request_context = ", ".join(
            f"{name}={value}"
            for name, value in (
                ("runId", request.run_id),
                ("receiptCode", request.receipt_code),
                ("invoiceKey", request.invoice_key),
            )
            if value
        )
        if request_context:
            parts.append(request_context)
    compact_detail = _compact_error_text(detail)
    if compact_detail:
        parts.append(f"detail={compact_detail}")
    return "; ".join(parts)


def _is_number_like(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            float(text)
        except ValueError:
            return False
        return True
    return False


def _validate_llm_result(result: Any) -> str | None:
    if not isinstance(result, Mapping):
        return "llmResult must be a JSON object"

    # 对常用字段做轻量类型约束，不限制业务字段扩展。
    if "passed" in result and not isinstance(result.get("passed"), bool):
        return "llmResult.passed must be a boolean"
    if "finalAmount" in result and not _is_number_like(result.get("finalAmount")):
        return "llmResult.finalAmount must be a number"
    if "isInvoiceYearMatched" in result and not isinstance(result.get("isInvoiceYearMatched"), bool):
        return "llmResult.isInvoiceYearMatched must be a boolean"
    if "remarkPhone" in result and not isinstance(result.get("remarkPhone"), str):
        return "llmResult.remarkPhone must be a string"
    if "matchReason" in result and not isinstance(result.get("matchReason"), str):
        return "llmResult.matchReason must be a string"

    return None


async def _parse_llm_request(raw_request: Request) -> tuple[EvaluateLlmRequest | None, str | None]:
    payload: dict[str, Any] = {}
    body = await raw_request.body()

    if body:
        text = body.decode("utf-8", errors="ignore").strip()
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None

            if isinstance(parsed, dict):
                payload.update(parsed)
            else:
                form_payload = parse_qs(text, keep_blank_values=True)
                payload.update({key: values[-1] for key, values in form_payload.items() if values})

    if raw_request.query_params:
        for key, value in raw_request.query_params.items():
            payload.setdefault(key, value)

    try:
        return EvaluateLlmRequest.model_validate(payload), None
    except ValidationError as exc:
        return None, f"invalid request body: {exc.errors()}"


def register_node_gateway_routes(app: FastAPI) -> None:
    @app.post(NODE_GATEWAY_LLM_EVALUATE_PATH, response_model=EvaluateLlmResponse)
    async def evaluate_llm(raw_request: Request) -> dict[str, Any]:
        request, validation_error = await _parse_llm_request(raw_request)
        if request is None:
            return {
                "llmStatus": "error",
                "errorMessage": validation_error,
                "llmResult": None,
                "rawContent": None,
                "errorType": "invalid_request",
                "attempts": 0,
            }

        _load_project_env()
        api_key = os.getenv("LLM_API_KEY", "").strip()
        base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
        default_model = os.getenv("LLM_MODEL", "").strip()
        max_retries = _resolve_retry_count(request.max_retries)

        def configuration_error(message: str) -> dict[str, Any]:
            return {
                "llmStatus": "error",
                "errorMessage": _build_error_message(
                    error_type="configuration_error",
                    detail=message,
                    attempts=0,
                    max_retries=max_retries,
                    request=request,
                ),
                "llmResult": None,
                "rawContent": None,
                "errorType": "configuration_error",
                "attempts": 0,
                "maxRetries": max_retries,
            }

        if not api_key:
            return configuration_error("missing env LLM_API_KEY")

        if not base_url:
            return configuration_error("missing env LLM_BASE_URL")

        if not default_model:
            return configuration_error("missing env LLM_MODEL")

        system_prompt = request.system_prompt or "You are an audit assistant. Return JSON "

        payload = {
            "model": default_model,
            "temperature": request.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.prompt},
            ],
        }

        last_error: str | None = None
        last_error_type = "unknown_error"
        last_raw_content: str | None = None
        last_upstream_status: int | None = None
        attempts = 0
        retry_backoff_seconds = _resolve_retry_backoff_seconds()

        for attempt in range(max_retries + 1):
            attempts = attempt + 1
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {api_key}",
                        },
                        json=payload,
                    )
            except Exception as exc:
                last_error_type = "request_exception"
                last_upstream_status = None
                last_error = f"request exception ({type(exc).__name__}): {_compact_error_text(exc) or 'no details'}"
                if attempt < max_retries and _is_retryable_exception(exc):
                    await asyncio.sleep(min(retry_backoff_seconds * (2**attempt), 30.0))
                    continue
                break

            last_upstream_status = resp.status_code
            if resp.status_code < 200 or resp.status_code >= 300:
                last_error_type = "upstream_http_error"
                last_upstream_status = resp.status_code
                last_error = f"llm request failed: {resp.status_code} {_compact_error_text(resp.text) or 'empty response'}"
                if attempt < max_retries and _is_retryable_status(resp.status_code):
                    await asyncio.sleep(min(retry_backoff_seconds * (2**attempt), 30.0))
                    continue
                break

            try:
                data = resp.json()
            except Exception as exc:
                last_error_type = "invalid_response_json"
                last_error = f"invalid llm response json ({type(exc).__name__}): {_compact_error_text(exc) or 'no details'}"
                last_raw_content = resp.text
                if attempt < max_retries:
                    await asyncio.sleep(min(retry_backoff_seconds * (2**attempt), 30.0))
                    continue
                break

            try:
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            except (AttributeError, IndexError, KeyError, TypeError) as exc:
                last_error_type = "invalid_response_shape"
                last_error = f"invalid llm response shape ({type(exc).__name__}): {_compact_error_text(exc) or 'missing choices.message.content'}"
                last_raw_content = _compact_error_text(resp.text)
                if attempt < max_retries:
                    await asyncio.sleep(min(retry_backoff_seconds * (2**attempt), 30.0))
                    continue
                break

            if not content:
                last_error_type = "empty_model_content"
                last_error = "empty model content"
                last_raw_content = None
                if attempt < max_retries:
                    await asyncio.sleep(min(retry_backoff_seconds * (2**attempt), 30.0))
                    continue
                break

            if not isinstance(content, str):
                last_error_type = "invalid_model_content"
                last_error = f"model content must be a string, got {type(content).__name__}"
                last_raw_content = _compact_error_text(content)
                if attempt < max_retries:
                    await asyncio.sleep(min(retry_backoff_seconds * (2**attempt), 30.0))
                    continue
                break

            cleaned = _strip_markdown_json_fence(content)
            try:
                parsed = json.loads(cleaned)
            except Exception as exc:
                last_error_type = "invalid_model_json"
                last_error = f"model output is not valid JSON: {exc}"
                last_raw_content = content
                if attempt < max_retries:
                    await asyncio.sleep(min(retry_backoff_seconds * (2**attempt), 30.0))
                    continue
                break

            validation_error = _validate_llm_result(parsed)
            if validation_error:
                last_error_type = "invalid_result_format"
                last_error = f"invalid llm result format: {validation_error}"
                last_raw_content = content
                if attempt < max_retries:
                    await asyncio.sleep(min(retry_backoff_seconds * (2**attempt), 30.0))
                    continue
                break

            return {
                "llmStatus": "success",
                "llmResult": parsed,
                "rawContent": content,
                "errorMessage": None,
                "errorType": None,
                "attempts": attempts,
                "maxRetries": max_retries,
            }

        return {
            "llmStatus": "error",
            "errorMessage": _build_error_message(
                error_type=last_error_type,
                detail=last_error,
                attempts=attempts,
                max_retries=max_retries,
                upstream_status=last_upstream_status,
                request=request,
            ),
            "llmResult": None,
            "rawContent": last_raw_content,
            "errorType": last_error_type,
            "attempts": attempts,
            "maxRetries": max_retries,
            "upstreamStatus": last_upstream_status,
        }


def create_app() -> FastAPI:
    app = FastAPI(title="Node Gateway Service", version="1.0.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    register_node_gateway_routes(app)
    return app
