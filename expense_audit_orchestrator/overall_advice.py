"""核销单级整体建议（Overall Suggestion）。

在 orchestrator 跑完所有发票后、回写组装前，把全单问题摘要喂给 LLM，
生成一条给报销人的「这张单子整体该怎么改」的结论性建议。

容错是硬约束：任何 LLM / 网络 / 解析失败都降级为返回 None，
绝不打断审计/回写主链路（与 bootstrap 的 safe-sink 同原则）。

LLM 调用复用 node_gateway 的 /api/v1/node-gateway/llm/evaluate（默认 8091），
orchestrator 侧用同步 httpx（与 runtime_client / kingdee_ocr 同约定）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx
from dotenv import load_dotenv

from .observability import get_logger, new_run_id, run_context
from .writeback import _extract_rule_results, _normalize_rule_distinguish_result


DEFAULT_NODE_GATEWAY_URL = "http://127.0.0.1:8091"
DEFAULT_OVERALL_ADVICE_TIMEOUT = 30.0
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_PROMPT_DIR: Path = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT_FILENAME = "receipt_overall_advice.system.md"
USER_PROMPT_FILENAME = "receipt_overall_advice.user.md"
NODE_GATEWAY_LLM_EVALUATE_PATH = "/api/v1/node-gateway/llm/evaluate"

# 只把这些状态的问题纳入摘要；pass 不出现。
_PROBLEM_STATUSES = frozenset({"reject", "failed", "warning"})
# 摘要最多保留多少行，避免 prompt 爆炸。
_MAX_DIGEST_LINES = 30

_logger = get_logger("overall_advice")

ROOT = Path(__file__).resolve().parent.parent


def _load_project_env() -> None:
    load_dotenv(ROOT / ".env", override=False)


class OverallAdviceProvider(Protocol):
    """生成核销单级整体建议的可调用对象。返回 None 表示不输出该字段。"""

    def __call__(
        self,
        receipt_code: str,
        invoice_results: Sequence[Mapping[str, Any]],
        *,
        receipt_context: Mapping[str, Any] | None = None,
    ) -> str | None: ...


class NoopOverallAdviceProvider:
    """关闭时使用：恒返回 None，不调用任何外部服务。"""

    def __call__(
        self,
        receipt_code: str,
        invoice_results: Sequence[Mapping[str, Any]],
        *,
        receipt_context: Mapping[str, Any] | None = None,
    ) -> str | None:
        return None


def _load_prompt(path: Path, placeholders: Mapping[str, str]) -> str:
    """读 prompt 文件并做 {{placeholder}} 简单替换。"""
    text = path.read_text(encoding="utf-8")
    for key, value in placeholders.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _strip_markdown_json_fence(content: str) -> str:
    """去掉模型可能包裹的 ```json / ``` 围栏。与 node_gateway 同实现。"""
    text = content.strip()
    if text.startswith("```json"):
        text = text[len("```json"):].strip()
    elif text.startswith("```"):
        text = text[len("```"):].strip()
    if text.endswith("```"):
        text = text[: -len("```")].strip()
    return text


def _build_problems_digest(
    invoice_results: Sequence[Mapping[str, Any]],
) -> str:
    """把全单 reject/failed/warning 的问题汇总成紧凑文本。

    全通过 / 无发票时返回空串（调用方仍会调 LLM 让其给正面确认）。
    """
    lines: list[str] = []
    for invoice_result in invoice_results:
        if not isinstance(invoice_result, Mapping):
            continue
        invoice_key = str(invoice_result.get("invoiceKey") or "")
        decision_output = invoice_result.get("decisionOutput") or {}
        if not isinstance(decision_output, Mapping):
            continue
        for rule_result in _extract_rule_results(decision_output):
            status = _normalize_rule_distinguish_result(
                rule_result.get("distinguish_result") or rule_result.get("distinguishResult")
            )
            if status not in _PROBLEM_STATUSES:
                continue
            reason_code = rule_result.get("reason_code") or rule_result.get("reasonCode") or "?"
            message = (rule_result.get("message") or "").strip()
            suggestion = (rule_result.get("suggestion") or "").strip()
            line = f"- 发票 {invoice_key} [{reason_code} {status}] {message}"
            if suggestion:
                line += f" | 建议: {suggestion}"
            lines.append(line)
            if len(lines) >= _MAX_DIGEST_LINES:
                break
        if len(lines) >= _MAX_DIGEST_LINES:
            break

    if not lines:
        return ""

    if len(lines) == _MAX_DIGEST_LINES:
        lines.append("... (其余问题已省略)")
    return "\n".join(lines)


def _extract_suggestion(response_json: Mapping[str, Any]) -> str | None:
    """从 node_gateway 响应里稳健取出 suggestion 字符串。

    优先 llmResult.suggestion；否则回退到 rawContent 去 fence 后 JSON parse。
    """
    if str(response_json.get("llmStatus") or "").lower() != "success":
        return None

    llm_result = response_json.get("llmResult")
    if not isinstance(llm_result, Mapping):
        # 回退：rawContent 可能是带/不带 fence 的 JSON 文本
        raw = response_json.get("rawContent")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(_strip_markdown_json_fence(raw))
            except Exception:
                return None
            if isinstance(parsed, Mapping):
                llm_result = parsed
        if not isinstance(llm_result, Mapping):
            return None

    suggestion = llm_result.get("suggestion")
    if isinstance(suggestion, str) and suggestion.strip():
        return suggestion.strip()
    return None


class LlmOverallAdviceProvider:
    """调用 node_gateway 生成核销单整体建议。任何失败降级为 None。"""

    def __init__(
        self,
        *,
        node_gateway_url: str,
        model: str,
        prompt_dir: Path | str = DEFAULT_PROMPT_DIR,
        timeout: float = DEFAULT_OVERALL_ADVICE_TIMEOUT,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._endpoint = f"{node_gateway_url.rstrip('/')}{NODE_GATEWAY_LLM_EVALUATE_PATH}"
        self._model = model
        self._prompt_dir = Path(prompt_dir)
        self._timeout = timeout
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=self._timeout))

    def __call__(
        self,
        receipt_code: str,
        invoice_results: Sequence[Mapping[str, Any]],
        *,
        receipt_context: Mapping[str, Any] | None = None,
    ) -> str | None:
        run_id = new_run_id()
        try:
            system_prompt = _load_prompt(
                self._prompt_dir / SYSTEM_PROMPT_FILENAME, placeholders={}
            )
            user_prompt = _load_prompt(
                self._prompt_dir / USER_PROMPT_FILENAME,
                placeholders={
                    "receiptCode": receipt_code,
                    "invoiceCount": str(len(invoice_results)),
                    "problemsDigest": _build_problems_digest(invoice_results),
                },
            )
        except Exception:
            _logger.exception(
                "overall advice prompt load failed",
                extra={
                    "receipt_code": receipt_code,
                    "run_id": run_id,
                    "event": "overall_advice.prompt_failed",
                },
            )
            return None

        payload: dict[str, Any] = {
            "prompt": user_prompt,
            "systemPrompt": system_prompt,
            "model": self._model,
            "temperature": 0,
            "runId": run_id,
            "receiptCode": receipt_code,
        }

        with run_context(receipt_code=receipt_code, run_id=run_id, invoice_key=None):
            try:
                with self._client_factory() as client:
                    response = client.post(
                        self._endpoint,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
            except httpx.HTTPError as exc:
                _logger.exception(
                    "overall advice llm call failed",
                    extra={
                        "receipt_code": receipt_code,
                        "run_id": run_id,
                        "event": "overall_advice.failed",
                        "error": str(exc),
                    },
                )
                return None

            if response.status_code < 200 or response.status_code >= 300:
                _logger.warning(
                    "overall advice llm non-2xx",
                    extra={
                        "receipt_code": receipt_code,
                        "run_id": run_id,
                        "event": "overall_advice.failed",
                        "status_code": response.status_code,
                    },
                )
                return None

            try:
                response_json = response.json()
            except Exception as exc:
                _logger.exception(
                    "overall advice llm invalid json",
                    extra={
                        "receipt_code": receipt_code,
                        "run_id": run_id,
                        "event": "overall_advice.failed",
                        "error": str(exc),
                    },
                )
                return None

        suggestion = _extract_suggestion(response_json)
        if suggestion is None:
            _logger.info(
                "overall advice produced no suggestion",
                extra={
                    "receipt_code": receipt_code,
                    "run_id": run_id,
                    "event": "overall_advice.empty",
                    "llm_status": response_json.get("llmStatus"),
                },
            )
            return suggestion

        _logger.info(
            "overall advice generated",
            extra={
                "receipt_code": receipt_code,
                "run_id": run_id,
                "event": "overall_advice.success",
                "model": self._model,
                "suggestion_length": len(suggestion),
            },
        )
        return suggestion


def _resolve_bool_env(env_name: str, *, default: bool) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def create_overall_advice_provider_from_env() -> OverallAdviceProvider:
    """从环境变量构造整体建议 provider。关闭时返回 Noop。

    不在构造期连网或校验文件，prompt 缺失等延迟到调用期由外层兜住。
    """
    _load_project_env()
    if not _resolve_bool_env("OVERALL_ADVICE_ENABLED", default=True):
        return NoopOverallAdviceProvider()

    node_gateway_url = (os.getenv("NODE_GATEWAY_URL") or DEFAULT_NODE_GATEWAY_URL).strip()
    model = (os.getenv("OVERALL_ADVICE_MODEL") or os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL).strip()
    prompt_dir = Path(os.getenv("OVERALL_ADVICE_PROMPT_DIR") or DEFAULT_PROMPT_DIR)
    timeout_raw = (os.getenv("OVERALL_ADVICE_TIMEOUT") or "").strip()
    try:
        timeout = float(timeout_raw) if timeout_raw else DEFAULT_OVERALL_ADVICE_TIMEOUT
    except ValueError:
        timeout = DEFAULT_OVERALL_ADVICE_TIMEOUT

    return LlmOverallAdviceProvider(
        node_gateway_url=node_gateway_url,
        model=model,
        prompt_dir=prompt_dir,
        timeout=timeout,
    )


__all__ = [
    "LlmOverallAdviceProvider",
    "NoopOverallAdviceProvider",
    "OverallAdviceProvider",
    "create_overall_advice_provider_from_env",
]
