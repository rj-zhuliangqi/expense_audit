from .client import GraphRuntimeClient, HttpGraphRuntimeClient, LocalGraphRuntimeClient


def create_runtime_service(graph_runtime_url: str | None = None) -> GraphRuntimeClient:
    if graph_runtime_url is not None:
        return HttpGraphRuntimeClient(graph_runtime_url)

    return LocalGraphRuntimeClient()


def create_receipt_audit_service(*args, **kwargs):
    raise RuntimeError("graph_runtime no longer owns receipt orchestration; use expense_audit_orchestrator")