from __future__ import annotations

import csv
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core import ROOT


DEFAULT_OPERATOR_CITY_CSV_PATH = ROOT / "operator_city.csv"


def resolve_telecom_csv_path(asset_dir: Path | str | None = None) -> Path:
    if asset_dir is not None:
        return Path(asset_dir) / "operator_city.csv"
    env_path = os.getenv("TELECOM_OPERATOR_CITY_CSV")
    if env_path and env_path.strip():
        return Path(env_path.strip())
    return DEFAULT_OPERATOR_CITY_CSV_PATH


def load_telecom_list(csv_path: Path | str | None = None) -> list[list[str]]:
    resolved = Path(csv_path) if csv_path is not None else resolve_telecom_csv_path()
    telecom_list: list[list[str]] = []

    with resolved.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError("operator_city.csv does not contain headers")

        operator_key = "运营商" if "运营商" in reader.fieldnames else reader.fieldnames[0]
        city_key = "城市" if "城市" in reader.fieldnames else reader.fieldnames[1]

        for row in reader:
            operator = (row.get(operator_key) or "").strip()
            city = (row.get(city_key) or "").strip()
            if operator and city:
                telecom_list.append([operator, city])

    return telecom_list


def telecom_receipt_enricher(
    telecom_list: list[list[str]] | None = None,
) -> "callable":
    cached = load_telecom_list() if telecom_list is None else list(telecom_list)

    def enrich(receipt_code: str, service_data: Mapping[str, Any]) -> list[list[str]]:
        return cached

    return enrich


__all__ = [
    "DEFAULT_OPERATOR_CITY_CSV_PATH",
    "load_telecom_list",
    "resolve_telecom_csv_path",
    "telecom_receipt_enricher",
]
