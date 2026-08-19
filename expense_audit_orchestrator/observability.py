"""可观测性模块：结构化 JSON 日志 + 每执行一份 JSON 产物文件。

设计目标：让一次单据审计的全过程可排查——
- stdout 输出结构化 JSON 日志（journalctl 可查、可 grep、带 receipt_code/run_id）；
- 每执行一份 JSON 产物文件（output/logs/<receiptCode>/<runId>.json），
  含每个节点的输入/输出/耗时（zen trace）、各决策表结果、LLM 调用摘要；
- run_id 串联 worker → graph_runtime → node_gateway → LLM。

不引入新依赖，仅用 stdlib logging + json。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from expense_audit_orchestrator.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
DEFAULT_LOG_DIR = ROOT / "output" / "logs"

# 会被 logger.info(..., extra={...}) 设置到 LogRecord 上的关联字段。
# 任何 extra 里传入的字段也会原样写入 JSON。
_CORRELATION_KEYS = ("receipt_code", "run_id", "invoice_key", "event")

# stdlib LogRecord 内置属性，不当作 extra 输出。
_RESERVED_LOGRECORD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)

_CONFIGURED = False

# 跨调用栈的关联上下文（receipt_code/run_id/invoice_key），供 RunContextFilter 自动盖到日志上。
_run_ctx: ContextVar[Mapping[str, Any]] = ContextVar("expense_audit_run_ctx", default={})


class RunContextFilter(logging.Filter):
    """把 _run_ctx 里的 receipt_code/run_id/invoice_key 盖到每条 LogRecord 上（若记录没自带）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _run_ctx.get()
        for key in _CORRELATION_KEYS:
            if getattr(record, key, None) is None:
                value = ctx.get(key)
                if value is not None:
                    setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """每条日志输出一行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _CORRELATION_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 合并其余 extra 字段
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_ATTRS:
                continue
            if key in _CORRELATION_KEYS:
                continue
            if key in payload:
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str | None = None) -> None:
    """配置 root logger：单个 stdout StreamHandler + JsonFormatter。幂等。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved_level = _resolve_level(level)
    root = logging.getLogger()
    root.setLevel(resolved_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(resolved_level)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunContextFilter())
    root.addHandler(handler)

    # 第三方库噪声降到 WARNING，保持 stdout 为纯应用 JSON 日志
    for noisy in ("uvicorn.access", "pika", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def _resolve_level(level: str | None) -> int:
    candidate = (level or os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    numeric = logging.getLevelName(candidate)
    return numeric if isinstance(numeric, int) else logging.INFO


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.propagate = True
    return logger


@contextmanager
def run_context(*, receipt_code: str | None, run_id: str | None, invoice_key: str | None = None):
    """在 with 块内，所有日志自动带上 receipt_code/run_id/invoice_key。"""
    ctx: dict[str, Any] = {}
    if receipt_code is not None:
        ctx["receipt_code"] = receipt_code
    if run_id is not None:
        ctx["run_id"] = run_id
    if invoice_key is not None:
        ctx["invoice_key"] = invoice_key
    token = _run_ctx.set(ctx)
    try:
        yield
    finally:
        _run_ctx.reset(token)


def new_run_id() -> str:
    return uuid.uuid4().hex


def resolve_log_dir(log_dir: Path | str | None = None) -> Path:
    candidate = log_dir if log_dir is not None else os.getenv("LOG_DIR") or DEFAULT_LOG_DIR
    return Path(candidate)


def _safe_log_dir(log_dir: Path | str | None) -> Path | None:
    candidate = log_dir if log_dir is not None else os.getenv("LOG_DIR")
    return Path(candidate) if candidate else None


def _utc_now_isoformat() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_duration_ms(started_at: str | None, finished_at: str | None) -> int | None:
    if not started_at or not finished_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        finish = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    delta = (finish - start).total_seconds()
    if delta < 0:
        return None
    return int(delta * 1000)


def _summarize_keys(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return list(value.keys())
    if isinstance(value, dict):
        return list(value.keys())
    return []


def _extract_llm_calls(trace: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """从 zen trace 里扫出各 LLM 调用节点的结果摘要（trace 按 nodeId 索引）。

    只收录「真正的 LLM 调用节点」——其 output 含 raw_content 或非空 llm_result。
    （部分 expressionNode 会把上游 llm_status 透传下来，不算独立调用。）
    """
    calls: list[dict[str, Any]] = []
    if not isinstance(trace, Mapping):
        return calls
    for node_id, node in trace.items():
        if not isinstance(node, Mapping):
            continue
        output = node.get("output")
        if not isinstance(output, Mapping):
            continue
        if "llm_status" not in output:
            continue
        raw_content = output.get("raw_content")
        llm_result = output.get("llm_result")
        # 真正的 LLM 调用节点会带 raw_content 或非空 llm_result；透传节点两者皆无。
        if not (raw_content is not None or llm_result is not None):
            continue
        calls.append(
            {
                "nodeId": node_id,
                "nodeName": node.get("name"),
                "llmStatus": output.get("llm_status"),
                "error": output.get("error_message"),
                "rawContentLength": len(raw_content) if isinstance(raw_content, str) else None,
                "llmResult": llm_result,
            }
        )
    return calls


def write_invoice_artifact(
    *,
    receipt_code: str,
    run_id: str,
    invoice_result: Mapping[str, Any],
    log_dir: Path | str | None = None,
) -> Path:
    """写 output/logs/<receiptCode>/<runId>.json，返回路径。"""
    resolved_log_dir = resolve_log_dir(log_dir)
    target_dir = resolved_log_dir / _sanitize_receipt_code(receipt_code)
    target_dir.mkdir(parents=True, exist_ok=True)

    runtime_result = invoice_result.get("runtimeResult") or {}
    trace = runtime_result.get("trace") if isinstance(runtime_result, Mapping) else None
    prepared_input = invoice_result.get("preparedInput") or {}
    service_data = prepared_input.get("serviceData") if isinstance(prepared_input, Mapping) else None
    context = prepared_input.get("context") if isinstance(prepared_input, Mapping) else None
    ocr_envelope = (
        service_data.get("ocrEnvelope")
        if isinstance(service_data, Mapping)
        else None
    )

    decision_output = invoice_result.get("decisionOutput") or {}

    artifact = {
        "schemaVersion": 1,
        "receiptCode": receipt_code,
        "runId": run_id,
        "invoiceKey": invoice_result.get("invoiceKey"),
        "invoiceFile": invoice_result.get("invoiceFile"),
        "startedAt": invoice_result.get("startedAt"),
        "finishedAt": invoice_result.get("finishedAt"),
        "durationMs": _iso_duration_ms(
            invoice_result.get("startedAt"), invoice_result.get("finishedAt")
        ),
        "executionStatus": invoice_result.get("executionStatus"),
        "decisionStatus": invoice_result.get("decisionStatus"),
        "errorMessage": invoice_result.get("errorMessage"),
        "preparedInputSummary": {
            "contextKeys": _summarize_keys(context),
            "serviceDataKeys": _summarize_keys(service_data),
            "ocrEnvelopeKeys": _summarize_keys(ocr_envelope),
        },
        "decisionOutput": decision_output,
        "perNodeTrace": trace if isinstance(trace, dict) else {},
        "performance": runtime_result.get("performance") if isinstance(runtime_result, Mapping) else None,
        "llmCalls": _extract_llm_calls(trace),
    }

    target_file = target_dir / f"{_sanitize_token(run_id)}.json"
    target_file.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return target_file


def build_invoice_result_log_sink(
    log_dir: Path | str | None = None,
) -> Callable[[str, Mapping[str, Any]], None]:
    """构造一个 InvoiceResultSink：写产物文件 + 发结构化日志。"""
    resolved_log_dir = resolve_log_dir(log_dir)
    logger = get_logger("invoice_result_sink")

    def sink(receipt_code: str, invoice_result: Mapping[str, Any]) -> None:
        run_id = str(invoice_result.get("runId") or "")
        invoice_key = str(invoice_result.get("invoiceKey") or "")
        runtime_result = invoice_result.get("runtimeResult") or {}
        trace = runtime_result.get("trace") if isinstance(runtime_result, Mapping) else None
        artifact_path: Path | None = None
        try:
            artifact_path = write_invoice_artifact(
                receipt_code=receipt_code,
                run_id=run_id or new_run_id(),
                invoice_result=invoice_result,
                log_dir=resolved_log_dir,
            )
        except Exception:
            logger.exception(
                "invoice artifact write failed",
                extra={
                    "receipt_code": receipt_code,
                    "run_id": run_id,
                    "invoice_key": invoice_key,
                    "event": "invoice.artifact.failed",
                },
            )

        logger.info(
            "invoice evaluated",
            extra={
                "receipt_code": receipt_code,
                "run_id": run_id,
                "invoice_key": invoice_key,
                "event": "invoice.evaluated",
                "check_status": invoice_result.get("decisionStatus"),
                "execution_status": invoice_result.get("executionStatus"),
                "duration_ms": _iso_duration_ms(
                    invoice_result.get("startedAt"), invoice_result.get("finishedAt")
                ),
                "node_count": len(trace) if isinstance(trace, dict) else 0,
                "performance": runtime_result.get("performance") if isinstance(runtime_result, Mapping) else None,
                "artifact_path": str(artifact_path) if artifact_path else None,
                "error": invoice_result.get("errorMessage"),
            },
        )

    return sink


def write_llm_call_artifact(
    *,
    receipt_code: str | None,
    run_id: str | None,
    call_seq: int,
    payload: Mapping[str, Any],
    log_dir: Path | str | None = None,
) -> Path | None:
    """node_gateway 侧：写 output/logs/<receiptCode>/llm-<runId>-<seq>.json。

    receipt_code/run_id 缺失或未配置 LOG_DIR 时返回 None（仅走 stdout）。
    """
    resolved_log_dir = _safe_log_dir(log_dir)
    if resolved_log_dir is None:
        return None
    if not receipt_code or not run_id:
        return None

    target_dir = resolved_log_dir / _sanitize_receipt_code(receipt_code)
    target_dir.mkdir(parents=True, exist_ok=True)

    record = {"schemaVersion": 1, "receiptCode": receipt_code, "runId": run_id, "callSeq": call_seq, **dict(payload)}
    target_file = target_dir / f"llm-{_sanitize_token(run_id)}-{call_seq}.json"
    target_file.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return target_file


def _sanitize_receipt_code(receipt_code: str) -> str:
    # 防止路径穿越 / 非法文件名字符
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in receipt_code)
    return safe or "unknown"


def _sanitize_token(token: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in token)
    return safe or "unknown"
