import json
import os
from pathlib import Path
from typing import Any, Protocol

import httpx


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GRAPH_PATH = ROOT / "graph-latest-0623-1202.json"
DEFAULT_GRAPH_RUNTIME_URL = "http://127.0.0.1:8090"


class GraphRuntimeClient(Protocol):
    def evaluate(
        self,
        *,
        prepared_input: dict[str, Any],
        graph_path: Path | str | None = None,
        graph_content: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        ...


def resolve_graph_runtime_url(graph_runtime_url: str | None = None) -> str:
    candidate = graph_runtime_url if graph_runtime_url is not None else os.getenv("GRAPH_RUNTIME_URL")
    normalized = candidate.strip() if candidate is not None else ""
    return normalized or DEFAULT_GRAPH_RUNTIME_URL


def _normalize_graph_source(
    graph_path: Path | str | None,
    graph_content: dict[str, Any] | str | None,
) -> tuple[Path | str | None, dict[str, Any] | str | None]:
    if graph_path is not None and graph_content is not None:
        raise ValueError("graph_path and graph_content cannot be set together")

    if graph_path is None and graph_content is None:
        return DEFAULT_GRAPH_PATH, None

    return graph_path, graph_content


def _load_graph_content(graph_path: Path | str) -> dict[str, Any] | str:
    raw_content = Path(graph_path).read_text(encoding="utf-8")
    try:
        return json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content


def _classify_timeout(exc: httpx.TimeoutException) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect timed out"
    if isinstance(exc, httpx.ReadTimeout):
        return "read timed out"
    if isinstance(exc, httpx.WriteTimeout):
        return "write timed out"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool timed out"
    return "timed out"


def _extract_receipt_code(prepared_input: dict[str, Any]) -> str:
    receipt = prepared_input.get("receipt") or {}
    return receipt.get("code", "unknown") if isinstance(receipt, dict) else "unknown"


class HttpGraphRuntimeClient:
    DEFAULT_TIMEOUT = 120.0

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def evaluate(
        self,
        *,
        prepared_input: dict[str, Any],
        graph_path: Path | str | None = None,
        graph_content: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        resolved_graph_path, resolved_graph_content = _normalize_graph_source(graph_path, graph_content)
        payload: dict[str, Any] = {
            "preparedInput": prepared_input,
            "includePreparedInput": True,
            "graphContent": (
                resolved_graph_content
                if resolved_graph_content is not None
                else _load_graph_content(resolved_graph_path or DEFAULT_GRAPH_PATH)
            ),
        }

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(f"{self._base_url}/api/v1/graph-runtime/evaluations", json=payload)
        except httpx.TimeoutException as exc:
            timeout_type = _classify_timeout(exc)
            raise RuntimeError(
                f"graph runtime {timeout_type} after {self._timeout}s: "
                f"receipt_code={_extract_receipt_code(prepared_input)}, "
                f"base_url={self._base_url}"
            ) from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"graph runtime connection refused: "
                f"base_url={self._base_url}, detail={exc}"
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError(f"graph runtime request failed: {response.status_code} {response.text}")

        try:
            return response.json()
        except Exception as exc:
            raise ValueError(f"graph runtime returned invalid json: {exc}") from exc


def create_graph_runtime_client(graph_runtime_url: str | None = None) -> GraphRuntimeClient:
    return HttpGraphRuntimeClient(resolve_graph_runtime_url(graph_runtime_url))
