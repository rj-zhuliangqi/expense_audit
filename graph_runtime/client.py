from pathlib import Path
from typing import Any, Protocol

import httpx

from .application import evaluate_prepared_input
from .core import DEFAULT_GRAPH_PATH, load_decision, load_decision_from_content


class GraphRuntimeClient(Protocol):
    def evaluate(
        self,
        *,
        prepared_input: dict[str, Any],
        graph_path: Path | str | None = None,
        graph_content: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        ...


def _normalize_graph_source(
    graph_path: Path | str | None,
    graph_content: dict[str, Any] | str | None,
) -> tuple[Path | str | None, dict[str, Any] | str | None]:
    if graph_path is not None and graph_content is not None:
        raise ValueError("graph_path and graph_content cannot be set together")

    if graph_path is None and graph_content is None:
        return DEFAULT_GRAPH_PATH, None

    return graph_path, graph_content


class LocalGraphRuntimeClient:
    def evaluate(
        self,
        *,
        prepared_input: dict[str, Any],
        graph_path: Path | str | None = None,
        graph_content: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        resolved_graph_path, resolved_graph_content = _normalize_graph_source(graph_path, graph_content)
        decision_engine = (
            load_decision_from_content(resolved_graph_content)
            if resolved_graph_content is not None
            else load_decision(resolved_graph_path or DEFAULT_GRAPH_PATH)
        )
        return evaluate_prepared_input(decision_engine, prepared_input)


class HttpGraphRuntimeClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
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
        }
        if resolved_graph_content is not None:
            payload["graphContent"] = resolved_graph_content
        else:
            payload["graphPath"] = str(resolved_graph_path or DEFAULT_GRAPH_PATH)

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(f"{self._base_url}/api/v1/graph-runtime/evaluations", json=payload)

        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError(f"graph runtime request failed: {response.status_code} {response.text}")

        try:
            return response.json()
        except Exception as exc:
            raise ValueError(f"graph runtime returned invalid json: {exc}") from exc