import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE_GATEWAY_LLM_EVALUATE_PATH = "/api/v1/node-gateway/llm/evaluate"


def _load_project_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)


class EvaluateLlmRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    prompt: str = Field(min_length=1)
    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    model: str | None = None
    temperature: float = 0


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
        base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        default_model = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()

        if not api_key:
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
            return {
                "llmStatus": "error",
                "errorMessage": f"request exception: {exc}",
                "llmResult": None,
                "rawContent": None,
            }

        if resp.status_code < 200 or resp.status_code >= 300:
            return {
                "llmStatus": "error",
                "errorMessage": f"llm request failed: {resp.status_code} {resp.text}",
                "llmResult": None,
                "rawContent": None,
            }

        try:
            data = resp.json()
        except Exception as exc:
            return {
                "llmStatus": "error",
                "errorMessage": f"invalid llm response json: {exc}",
                "llmResult": None,
                "rawContent": resp.text,
            }

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            return {
                "llmStatus": "error",
                "errorMessage": "empty model content",
                "llmResult": None,
                "rawContent": None,
            }

        cleaned = _strip_markdown_json_fence(content)
        try:
            parsed = json.loads(cleaned)
        except Exception as exc:
            return {
                "llmStatus": "error",
                "errorMessage": f"model output is not valid JSON: {exc}",
                "llmResult": None,
                "rawContent": content,
            }

        return {
            "llmStatus": "success",
            "llmResult": parsed,
            "rawContent": content,
            "errorMessage": None,
        }


def create_app() -> FastAPI:
    app = FastAPI(title="Node Gateway Service", version="1.0.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    register_node_gateway_routes(app)
    return app