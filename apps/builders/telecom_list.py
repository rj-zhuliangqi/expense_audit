#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path
from typing import Any

from expense_audit_orchestrator.paths import DEFAULT_OPERATOR_CITY_CSV_PATH, PROJECT_ROOT, require_path, resolve_project_path

CSV_PATH = DEFAULT_OPERATOR_CITY_CSV_PATH
JSON_PATH = PROJECT_ROOT / "prepared-input.json"


def build_telecom_list(csv_path: Path | str) -> list[tuple[str, str]]:
    csv_file = resolve_project_path(csv_path)
    assert csv_file is not None
    require_path(csv_file, "telecom operator/city CSV")
    telecom_list: list[tuple[str, str]] = []

    with csv_file.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV does not contain headers")

        op_key = "运营商" if "运营商" in reader.fieldnames else reader.fieldnames[0]
        city_key = "城市" if "城市" in reader.fieldnames else reader.fieldnames[1]

        for row in reader:
            operator = (row.get(op_key) or "").strip()
            region = (row.get(city_key) or "").strip()
            if not operator or not region:
                continue
            telecom_list.append((operator, region))

    return telecom_list


def inject_into_service_data(node: Any, telecom_list: list[tuple[str, str]]) -> int:
    updated_count = 0

    if isinstance(node, dict):
        service_data = node.get("serviceData")
        if isinstance(service_data, dict):
            # JSON has no tuple type; tuples become lists when serialized.
            service_data["telecom_list"] = telecom_list
            updated_count += 1

        for value in node.values():
            updated_count += inject_into_service_data(value, telecom_list)

    elif isinstance(node, list):
        for item in node:
            updated_count += inject_into_service_data(item, telecom_list)

    return updated_count


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Inject telecom operator/city reference data into prepared JSON")
    parser.add_argument(
        "--source",
        type=Path,
        default=CSV_PATH,
        help="operator/city CSV source; defaults to resources/reference/operator_city.csv",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=JSON_PATH,
        help="prepared JSON to update",
    )
    args = parser.parse_args(argv)
    telecom_list = build_telecom_list(args.source)

    with args.input.open("r", encoding="utf-8") as f:
        data = json.load(f)

    updated_count = inject_into_service_data(data, telecom_list)
    if updated_count == 0:
        raise ValueError("No serviceData node found in JSON")

    with args.input.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.write("\n")

    print(f"telecom_list entries: {len(telecom_list)}")
    print(f"serviceData nodes updated: {updated_count}")


if __name__ == "__main__":
    main()
