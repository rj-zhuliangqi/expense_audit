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


def _cell_value(cell: Any) -> str:
    if not isinstance(cell, dict):
        return ""
    value = cell.get("value")
    return "" if value is None else str(value)


def _is_struck(style: Any) -> bool:
    if not isinstance(style, dict):
        return False
    line = str(style.get("font_line") or "").strip().lower()
    return line in {"line-through", "strikethrough", "strike-through"}


def _active_cell_text(cell: Any) -> str:
    """Return only rich-text fragments that are not struck through.

    Feishu keeps retired rule codes in the cell value and marks those
    fragments with ``font_line: line-through``.  Reading only ``value``
    therefore resurrects codes that are visually disabled.
    """
    if not isinstance(cell, dict):
        return ""
    rich_text = cell.get("rich_text")
    if isinstance(rich_text, list) and rich_text:
        parts: list[str] = []
        for fragment in rich_text:
            if not isinstance(fragment, dict) or _is_struck(fragment.get("style")):
                continue
            text = fragment.get("text")
            if text is not None:
                parts.append(str(text))
        return "".join(parts)
    if _is_struck(cell.get("cell_styles")):
        return ""
    return _cell_value(cell)


def _read_sheet() -> tuple[list[str], list[dict[str, Any]]]:
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

    headers = [_cell_value(cell) for cell in cells[0]]
    if headers != EXPECTED_HEADERS:
        raise RuntimeError(f"Unexpected travel sheet headers: {headers!r}")

    rows: list[dict[str, Any]] = []
    for row_index, row in zip(row_indices[1:], cells[1:]):
        cell_values = [row[index] if index < len(row) else {} for index in range(len(headers))]
        values = [_cell_value(cell) for cell in cell_values]
        if not any(values):
            continue
        row_values: dict[str, Any] = {
            header: value for header, value in zip(headers, values)
        }
        # E is the employee-facing error/code column.  Keep its complete raw
        # value in the B:J snapshot and retain the active rich-text projection
        # only as an internal synchronizer field.
        row_values["_active_reason_code_source"] = _active_cell_text(cell_values[4])
        row_values["source_row"] = str(row_index)
        rows.append(row_values)
    return headers, rows


def _slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z一-龥]+", "_", value).strip("_")
    return value.lower() or "rule"


_REASON_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:sys-\d{3}|[EW]\d{2}(?:-\d+)?))(?![A-Za-z0-9])"
)

def _normalize_codes(text: str) -> str:
    # Code text is embedded in the employee-facing error column.  Prefer the
    # final replacement code when a row says "旧 code 改为 新 code".
    replacements = re.findall(
        r"(?:改为|修改为)\s*((?:sys-\d{3}|[EW]\d{2}(?:-\d+)?))",
        text,
    )
    if replacements:
        preferred = replacements[-1]
        if preferred == "E34" and "E42" in text:
            preferred = "E42"
        if preferred == "E33" and "E41" in text:
            preferred = "E41"
        if "E42" in text and "出租车" in text:
            preferred = "E42"
        return preferred

    codes = _REASON_CODE_RE.findall(text)
    if "E34" in codes and "E42" in text:
        codes = [code for code in codes if code != "E34"]
        codes.append("E42")
    if "E39" in codes and "W36" in codes and "增值税" in text:
        # The latest Feishu row strikes through E39 and leaves W36 active.
        codes = [code for code in codes if code != "E39"]
    if not codes:
        raise RuntimeError(f"Could not parse reason code from: {text!r}")
    return "|".join(dict.fromkeys(codes))


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    # The E column is the source of truth.  Parse only the active rich-text
    # fragments so a struck-through retired code cannot become executable.
    # Preserve explicit suffixes (E20-1/E20-2, E31-1, ...), and continue an
    # existing family when Feishu still contains a bare code (E20 -> E20-5).
    # Every source row must resolve to exactly one active code.  The complete
    # raw E-cell text remains in reason_code_source for auditability.
    parsed_by_row: list[tuple[dict[str, Any], list[str]]] = []
    for row in rows:
        active_source = row.get("_active_reason_code_source")
        source_text = str(
            active_source
            if active_source is not None
            else row.get("员工端显示报错问题")
            or ""
        )
        parsed_codes = _normalize_codes(source_text).split("|")
        if len(parsed_codes) != 1:
            raise RuntimeError(
                f"Expected exactly one active reason code in Feishu row "
                f"{row.get('source_row', '?')}, got {parsed_codes!r}: {source_text!r}"
            )
        parsed_by_row.append((row, parsed_codes))
    base_counts: dict[str, int] = {}
    reserved_codes: set[str] = set()
    for _row, parsed_codes in parsed_by_row:
        for code in parsed_codes:
            if code.startswith("sys-") or re.search(r"-\d+$", code):
                if code in reserved_codes:
                    raise RuntimeError(f"Duplicate explicitly suffixed reason code in Feishu rows: {code}")
                reserved_codes.add(code)
            else:
                base_counts[code] = base_counts.get(code, 0) + 1

    used_codes = set(reserved_codes)
    explicit_suffixes: dict[str, set[int]] = {}
    for code in reserved_codes:
        match = re.match(r"^(.*)-(\d+)$", code)
        if match:
            explicit_suffixes.setdefault(match.group(1), set()).add(int(match.group(2)))
    next_suffix: dict[str, int] = {
        base: (max(suffixes) + 1 if suffixes else 1)
        for base, suffixes in explicit_suffixes.items()
    }
    for row, parsed_codes in parsed_by_row:
        audit_content = row["审核时看什么"]
        reason_source = str(row["员工端显示报错问题"])
        normalized_codes: list[str] = []
        for code in parsed_codes:
            if code.startswith("sys-") or re.search(r"-\d+$", code):
                normalized_code = code
            elif code in explicit_suffixes:
                suffix = next_suffix.get(code, 1)
                normalized_code = f"{code}-{suffix}"
                while normalized_code in used_codes:
                    suffix += 1
                    normalized_code = f"{code}-{suffix}"
                next_suffix[code] = suffix + 1
            elif base_counts.get(code, 0) > 1:
                suffix = next_suffix.get(code, 1)
                normalized_code = f"{code}-{suffix}"
                while normalized_code in used_codes:
                    suffix += 1
                    normalized_code = f"{code}-{suffix}"
                next_suffix[code] = suffix + 1
            else:
                normalized_code = code

            if normalized_code in used_codes and normalized_code not in reserved_codes:
                raise RuntimeError(
                    f"Duplicate normalized reason code {normalized_code!r} in Feishu row {row['source_row']}"
                )
            if normalized_code in normalized_codes:
                raise RuntimeError(
                    f"Duplicate normalized reason code {normalized_code!r} within Feishu row {row['source_row']}"
                )
            normalized_codes.append(normalized_code)
            used_codes.add(normalized_code)
        normalized.append({
            "source_row": row["source_row"],
            **{header: row[header] for header in EXPECTED_HEADERS},
            "rule_key": f"travel_r{int(row['source_row']):02d}_{_slug(audit_content)}",
            "reason_code": "|".join(normalized_codes),
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
