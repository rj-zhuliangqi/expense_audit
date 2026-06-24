from typing import Any, Self

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .bootstrap import create_runtime_service


class EvaluateGraphResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    receipt_code: str | None = Field(default=None, alias="receiptCode")
    check_status: str = Field(alias="checkStatus")
    message: str | None = None
    decision_output: dict[str, Any] = Field(alias="decisionOutput")
    prepared_input: dict[str, Any] | None = Field(default=None, alias="preparedInput")
    rule_input: dict[str, Any] | None = Field(default=None, alias="ruleInput")


class EvaluateGraphRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    graph_path: str | None = Field(default=None, alias="graphPath")
    graph_content: dict[str, Any] | str | None = Field(default=None, alias="graphContent")
    prepared_input: dict[str, Any] = Field(alias="preparedInput")
    include_prepared_input: bool = Field(default=False, alias="includePreparedInput")

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.graph_path is None and self.graph_content is None:
            raise ValueError("one of graphPath or graphContent is required")

        if self.graph_path is not None and self.graph_content is not None:
            raise ValueError("graphPath and graphContent cannot be set together")

        return self


def _strip_prepared_input(result: dict[str, Any], include_prepared_input: bool) -> dict[str, Any]:
    response_payload = dict(result)
    if not include_prepared_input:
        response_payload["preparedInput"] = None
        response_payload["ruleInput"] = None
    else:
        response_payload["ruleInput"] = response_payload.get("ruleInput") or response_payload.get("preparedInput")
    return response_payload


def register_runtime_routes(app: FastAPI) -> None:
    runtime_client = create_runtime_service()

    @app.post("/api/v1/graph-runtime/evaluations", response_model=EvaluateGraphResponse)
    async def evaluate_graph(request: EvaluateGraphRequest) -> dict[str, Any]:
        try:
            result = runtime_client.evaluate(
                graph_path=request.graph_path,
                graph_content=request.graph_content,
                prepared_input=request.prepared_input,
            )
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"unexpected server error: {exc}") from exc

        return _strip_prepared_input(result, request.include_prepared_input)


def create_app() -> FastAPI:
    app = FastAPI(title="Graph Runtime Service", version="1.0.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    register_runtime_routes(app)
    return app