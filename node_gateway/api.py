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


class EvaluateLlmResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    llm_status: str = Field(alias="llmStatus")
    llm_result: dict[str, Any] | None = Field(default=None, alias="llmResult")
    raw_content: str | None = Field(default=None, alias="rawContent")
    error_message: str | None = Field(default=None, alias="errorMessage")


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

    raw_retry = os.getenv("LLM_MAX_RETRIES", "1").strip()
    try:
        parsed = int(raw_retry)
    except ValueError:
        parsed = 1
    return max(0, min(parsed, 5))


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
            }

        _load_project_env()
        api_key = os.getenv("LLM_API_KEY", "").strip()
        base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
        default_model = os.getenv("LLM_MODEL", "").strip()

        if not api_key:
            return {
                "llmStatus": "error",
                "errorMessage": "missing env LLM_API_KEY",
                "llmResult": None,
                "rawContent": None,
            }

        if not base_url:
            return {
                "llmStatus": "error",
                "errorMessage": "missing env LLM_BASE_URL",
                "llmResult": None,
                "rawContent": None,
            }

        if not default_model:
            return {
                "llmStatus": "error",
                "errorMessage": "missing env LLM_MODEL",
                "llmResult": None,
                "rawContent": None,
            }

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

        max_retries = _resolve_retry_count(request.max_retries)
        last_error: str | None = None
        last_raw_content: str | None = None

        for attempt in range(max_retries + 1):
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
                last_error = f"request exception: {exc}"
                if attempt < max_retries:
                    continue
                break

            if resp.status_code < 200 or resp.status_code >= 300:
                last_error = f"llm request failed: {resp.status_code} {resp.text}"
                if attempt < max_retries:
                    continue
                break

            try:
                data = resp.json()
            except Exception as exc:
                last_error = f"invalid llm response json: {exc}"
                last_raw_content = resp.text
                if attempt < max_retries:
                    continue
                break

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                last_error = "empty model content"
                last_raw_content = None
                if attempt < max_retries:
                    continue
                break

            cleaned = _strip_markdown_json_fence(content)
            try:
                parsed = json.loads(cleaned)
            except Exception as exc:
                last_error = f"model output is not valid JSON: {exc}"
                last_raw_content = content
                if attempt < max_retries:
                    continue
                break

            validation_error = _validate_llm_result(parsed)
            if validation_error:
                last_error = f"invalid llm result format: {validation_error}"
                last_raw_content = content
                if attempt < max_retries:
                    continue
                break

            return {
                "llmStatus": "success",
                "llmResult": parsed,
                "rawContent": content,
                "errorMessage": None,
            }

        return {
            "llmStatus": "error",
            "errorMessage": f"{last_error}; retries={max_retries}",
            "llmResult": None,
            "rawContent": last_raw_content,
        }


def create_app() -> FastAPI:
    app = FastAPI(title="Node Gateway Service", version="1.0.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    register_node_gateway_routes(app)
    return app