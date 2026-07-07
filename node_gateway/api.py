import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from expense_audit_orchestrator.observability import (
    configure_logging,
    get_logger,
    write_llm_call_artifact,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE_GATEWAY_LLM_EVALUATE_PATH = "/api/v1/node-gateway/llm/evaluate"
_LLM_CALL_SEQ = 0


def _load_project_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)


class EvaluateLlmRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    prompt: str = Field(min_length=1)
    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    model: str | None = None
    temperature: float = 0
    run_id: str | None = Field(default=None, alias="runId")
    receipt_code: str | None = Field(default=None, alias="receiptCode")
    invoice_key: str | None = Field(default=None, alias="invoiceKey")


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
    logger = get_logger("node_gateway.llm")

    @app.post(NODE_GATEWAY_LLM_EVALUATE_PATH, response_model=EvaluateLlmResponse)
    async def evaluate_llm(raw_request: Request) -> dict[str, Any]:
        global _LLM_CALL_SEQ
        request, validation_error = await _parse_llm_request(raw_request)
        started_at = _utc_now_isoformat()
        t0 = time.monotonic()

        if request is None:
            logger.warning(
                "llm request invalid",
                extra={
                    "event": "llm.request.invalid",
                    "error": validation_error,
                },
            )
            return {
                "llmStatus": "error",
                "errorMessage": validation_error,
                "llmResult": None,
                "rawContent": None,
            }

        _load_project_env()
        api_key = os.getenv("LLM_API_KEY", "").strip()
        base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        default_model = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()

        correlation = {
            "receipt_code": request.receipt_code,
            "run_id": request.run_id,
            "invoice_key": request.invoice_key,
        }

        if not api_key:
            logger.warning(
                "llm request missing api key",
                extra={**correlation, "event": "llm.request", "error": "missing env LLM_API_KEY"},
            )
            return {
                "llmStatus": "error",
                "errorMessage": "missing env LLM_API_KEY",
                "llmResult": None,
                "rawContent": None,
            }

        system_prompt = request.system_prompt or "You are an audit assistant. Return JSON "
        model = request.model or default_model

        payload = {
            "model": model,
            "temperature": request.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.prompt},
            ],
        }

        logger.info(
            "llm request",
            extra={
                **correlation,
                "event": "llm.request",
                "model": model,
                "prompt_length": len(request.prompt),
                "has_system_prompt": request.system_prompt is not None,
            },
        )
        logger.debug(
            "llm prompt",
            extra={
                **correlation,
                "event": "llm.prompt",
                "system_prompt": request.system_prompt,
                "prompt": request.prompt,
            },
        )

        content: str | None = None
        parsed: Any = None
        status = "error"
        error_message: str | None = None

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
            error_message = f"request exception: {exc}"
            _log_llm_outcome(logger, correlation, model, status, error_message, t0, content)
            return {
                "llmStatus": "error",
                "errorMessage": error_message,
                "llmResult": None,
                "rawContent": None,
            }

        if resp.status_code < 200 or resp.status_code >= 300:
            error_message = f"llm request failed: {resp.status_code} {resp.text}"
            _log_llm_outcome(logger, correlation, model, status, error_message, t0, content)
            return {
                "llmStatus": "error",
                "errorMessage": error_message,
                "llmResult": None,
                "rawContent": None,
            }

        try:
            data = resp.json()
        except Exception as exc:
            error_message = f"invalid llm response json: {exc}"
            _log_llm_outcome(logger, correlation, model, status, error_message, t0, resp.text)
            return {
                "llmStatus": "error",
                "errorMessage": error_message,
                "llmResult": None,
                "rawContent": resp.text,
            }

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            error_message = "empty model content"
            _log_llm_outcome(logger, correlation, model, status, error_message, t0, content)
            return {
                "llmStatus": "error",
                "errorMessage": error_message,
                "llmResult": None,
                "rawContent": None,
            }

        cleaned = _strip_markdown_json_fence(content)
        try:
            parsed = json.loads(cleaned)
        except Exception as exc:
            error_message = f"model output is not valid JSON: {exc}"
            _log_llm_outcome(logger, correlation, model, status, error_message, t0, content, parsed)
            return {
                "llmStatus": "error",
                "errorMessage": error_message,
                "llmResult": None,
                "rawContent": content,
            }

        status = "success"
        _log_llm_outcome(logger, correlation, model, status, None, t0, content, parsed)

        _LLM_CALL_SEQ += 1
        _maybe_write_llm_artifact(
            receipt_code=request.receipt_code,
            run_id=request.run_id,
            call_seq=_LLM_CALL_SEQ,
            model=model,
            temperature=request.temperature,
            system_prompt=system_prompt,
            prompt=request.prompt,
            content=content,
            parsed=parsed,
            status=status,
            error_message=None,
            started_at=started_at,
        )

        return {
            "llmStatus": "success",
            "llmResult": parsed,
            "rawContent": content,
            "errorMessage": None,
        }


def _log_llm_outcome(
    logger,
    correlation: dict[str, Any],
    model: str,
    status: str,
    error_message: str | None,
    t0: float,
    content: str | None,
    parsed: Any = None,
) -> None:
    latency_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "llm response",
        extra={
            **correlation,
            "event": "llm.response",
            "llm_status": status,
            "model": model,
            "latency_ms": latency_ms,
            "error": error_message,
            "raw_content_length": len(content) if isinstance(content, str) else 0,
        },
    )
    logger.debug(
        "llm raw content",
        extra={
            **correlation,
            "event": "llm.raw_content",
            "raw_content": content,
            "llm_result": parsed,
        },
    )


def _maybe_write_llm_artifact(
    *,
    receipt_code: str | None,
    run_id: str | None,
    call_seq: int,
    model: str,
    temperature: float,
    system_prompt: str,
    prompt: str,
    content: str | None,
    parsed: Any,
    status: str,
    error_message: str | None,
    started_at: str,
) -> None:
    if not receipt_code or not run_id or not os.getenv("LOG_DIR"):
        return
    try:
        write_llm_call_artifact(
            receipt_code=receipt_code,
            run_id=run_id,
            call_seq=call_seq,
            payload={
                "model": model,
                "temperature": temperature,
                "systemPrompt": system_prompt,
                "prompt": prompt,
                "rawContent": content,
                "llmResult": parsed,
                "llmStatus": status,
                "errorMessage": error_message,
                "startedAt": started_at,
                "finishedAt": _utc_now_isoformat(),
            },
        )
    except Exception:
        get_logger("node_gateway.llm").exception(
            "llm artifact write failed",
            extra={"receipt_code": receipt_code, "run_id": run_id, "event": "llm.artifact.failed"},
        )


def _utc_now_isoformat() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Node Gateway Service", version="1.0.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    register_node_gateway_routes(app)
    return app