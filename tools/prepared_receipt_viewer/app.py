"""Local-only viewer for prepared receipt invoice inputs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PREPARED_DIR = PROJECT_ROOT / "output" / "worker-debug" / "prepared"
PREPARED_RECEIPT_DIR = Path(os.getenv("PREPARED_RECEIPT_DIR", DEFAULT_PREPARED_DIR))
RECEIPT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _receipt_path(receipt_code: str, prepared_dir: Path) -> Path:
    if not RECEIPT_CODE_PATTERN.fullmatch(receipt_code):
        raise HTTPException(status_code=400, detail="核销单号格式不合法")

    root = prepared_dir.expanduser().resolve()
    target = (root / f"{receipt_code}.prepared-receipt.json").resolve()
    if target.parent != root:
        raise HTTPException(status_code=400, detail="核销单号格式不合法")
    return target


def _load_prepared_inputs(path: Path) -> list[Any]:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="未找到对应的核销单数据")

    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="核销单 JSON 格式无效") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="读取核销单数据失败") from exc

    preparations = payload.get("invoicePreparations") if isinstance(payload, dict) else None
    if not isinstance(preparations, list):
        raise HTTPException(status_code=422, detail="核销单缺少有效的 invoicePreparations 数组")

    return [
        preparation["preparedInput"]
        for preparation in preparations
        if isinstance(preparation, dict) and "preparedInput" in preparation
    ]


def create_app(prepared_dir: Path | str | None = None) -> FastAPI:
    data_dir = Path(prepared_dir) if prepared_dir is not None else PREPARED_RECEIPT_DIR
    application = FastAPI(title="Prepared Receipt Viewer")

    @application.get("/api/receipts/{receipt_code}", response_model=list[Any])
    def get_prepared_inputs(receipt_code: str) -> list[Any]:
        return _load_prepared_inputs(_receipt_path(receipt_code, data_dir))

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return application


app = create_app()
