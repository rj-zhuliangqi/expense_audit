"""Synchronize the travel audit rules snapshot from Feishu.

The generated CSV is the offline source of truth consumed by the travel graph
builder.  This command only reads Feishu through ``lark-cli``; it never writes
back to the spreadsheet.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from expense_audit_orchestrator.paths import TRAVEL_RULE_SOURCE

FEISHU_WIKI_URL = "https://ruijie.feishu.cn/wiki/DAgTwSx1tifIJfkbiF3ci8hrn7f?from=from_copylink"
SHEET_NAME = "Sheet1"
SHEET_RANGE = "A1:J200"
EXPECTED_HEADERS = [
    "费用类型",
    "审核时看什么",
    "费控需要提供的接口",
    "规则标准",
    "员工端显示报错问题",
    "制度索引",
    "问题分类",
    "优化后具体问题说明",
    "优化动作分类",
    "优化后建议",
]
# Keep the original Feishu B:J columns for auditability and add stable
# normalized aliases used by the offline builder/application seam.
NORMALIZED_FIELDS = [
    "audit_content",
    "data_dependency",
    "rule_condition",
    "reason_code_source",
    "policiesIndex",
    "problem_category",
    "message",
    "optimization_action_category",
    "employeeSuggestionTips",
]
OUTPUT_FIELDS = [
    "source_row",
    *EXPECTED_HEADERS,
    "rule_key",
    "reason_code",
    *NORMALIZED_FIELDS,
]


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"lark-cli returned non-JSON output: {completed.stdout[:500]}"
        ) from exc
    if not payload.get("ok", False):
        raise RuntimeError(payload.get("error", {}).get("message", "lark-cli request failed"))
    return payload


def _read_sheet() -> tuple[list[str], list[dict[str, str]]]:
    node = _run_json([
        "lark-cli",
        "wiki",
        "+node-get",
        "--node-token",
        FEISHU_WIKI_URL,
        "--as",
        "user",
        "--format",
        "json",
    ])
    spreadsheet_token = node["data"].get("obj_token")
    if not spreadsheet_token:
        raise RuntimeError("Feishu wiki node did not resolve to a spreadsheet token")

    sheet = _run_json([
        "lark-cli",
        "sheets",
        "+cells-get",
        "--spreadsheet-token",
        spreadsheet_token,
        "--sheet-name",
        SHEET_NAME,
        "--range",
        SHEET_RANGE,
        "--as",
        "user",
        "--format",
        "json",
        "--max-chars",
        "500000",
    ])
    ranges = sheet["data"].get("ranges") or []
    if not ranges:
        raise RuntimeError("Feishu sheet returned no ranges")
    range_data = ranges[0]
    cells = range_data.get("cells") or []
    row_indices = range_data.get("row_indices") or []
    col_indices = range_data.get("col_indices") or []
    if not cells:
        raise RuntimeError("Feishu sheet returned no cells")

    def cell_value(cell: Any) -> str:
        if not isinstance(cell, dict):
            return ""
        value = cell.get("value")
        return "" if value is None else str(value)

    headers = [cell_value(cell) for cell in cells[0]]
    if headers != EXPECTED_HEADERS:
        raise RuntimeError(f"Unexpected travel sheet headers: {headers!r}")

    rows: list[dict[str, str]] = []
    for row_index, row in zip(row_indices[1:], cells[1:]):
        values = [cell_value(row[index]) if index < len(row) else "" for index in range(len(headers))]
        if not any(values):
            continue
        rows.append({header: value for header, value in zip(headers, values)} | {"source_row": str(row_index)})
    return headers, rows


def _slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z一-龥]+", "_", value).strip("_")
    return value.lower() or "rule"


def _normalize_codes(text: str) -> str:
    # Code text is embedded in the employee-facing error column.  Prefer the
    # final replacement code when a row says "旧 code 改为 新 code".
    replacements = re.findall(r"(?:改为|修改为)\s*([A-Za-z]+-?\d+)", text)
    if replacements:
        preferred = replacements[-1]
        if preferred == "E34" and "E42" in text:
            preferred = "E42"
        if preferred == "E33" and "E41" in text:
            preferred = "E41"
        if preferred == "W36" and "E39" in text:
            preferred = "E39"
        if "E42" in text and "出租车" in text:
            preferred = "E42"
        if "E39" in text and "增值税" in text:
            preferred = "E39"
        return preferred

    codes = re.findall(r"(?<![A-Za-z0-9])(sys-\d{3}|[EW]\d{2})(?![A-Za-z0-9])", text)
    if "E34" in codes and "E42" in text:
        codes = [code for code in codes if code != "E34"]
        codes.append("E42")
    if "E39" in codes and "W36" in codes and "增值税" in text:
        codes = [code for code in codes if code != "W36"]
    if not codes:
        raise RuntimeError(f"Could not parse reason code from: {text!r}")
    return "|".join(dict.fromkeys(codes))


def normalize_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in rows:
        audit_content = row["审核时看什么"]
        reason_source = row["员工端显示报错问题"]
        normalized.append({
            "source_row": row["source_row"],
            **{header: row[header] for header in EXPECTED_HEADERS},
            "rule_key": f"travel_r{int(row['source_row']):02d}_{_slug(audit_content)}",
            "reason_code": _normalize_codes(reason_source),
            "audit_content": audit_content,
            "data_dependency": row["费控需要提供的接口"],
            "rule_condition": row["规则标准"],
            "reason_code_source": reason_source,
            "policiesIndex": row["制度索引"],
            "problem_category": row["问题分类"],
            "message": row["优化后具体问题说明"],
            "optimization_action_category": row["优化动作分类"],
            "employeeSuggestionTips": row["优化后建议"],
        })
    return normalized


def write_snapshot(rows: list[dict[str, str]], output: Path = TRAVEL_RULE_SOURCE) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=TRAVEL_RULE_SOURCE)
    args = parser.parse_args(argv)
    _headers, rows = _read_sheet()
    normalized = normalize_rows(rows)
    if len(normalized) != 36:
        raise RuntimeError(f"Expected 36 travel rules, found {len(normalized)}")
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    write_snapshot(normalized, output)
    print(f"Wrote {len(normalized)} travel rules to {output}")


if __name__ == "__main__":
    main()
