"""Build the travel-expense audit decision graph.

The travel graph is generated from ``resources/reference/travel_rules.csv``.
The CSV is an offline snapshot of the Feishu rule sheet; the graph builder
never needs a Feishu login or network access.  Every source row becomes its
own decision table, including rows that share a reason code.  The output path
contains the stable ``rule_key`` so the application can distinguish each source
rule even when legacy Feishu text contains a repeated base code.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from apps.builders.e05 import (
    E05_BOTH_DUPLICATE_MESSAGE,
    E05_HISTORY_DUPLICATE_MESSAGE,
    E05_RECEIPT_DUPLICATE_MESSAGE,
)
from expense_audit_orchestrator.paths import (
    LEGACY_TRAVEL_RULE_SOURCE,
    OFFICIAL_GRAPH_PATHS,
    PROJECT_ROOT,
    resolve_project_path,
)

SOURCE_GRAPH = OFFICIAL_GRAPH_PATHS["telecom"]
AUDIT_CSV = LEGACY_TRAVEL_RULE_SOURCE
OUTPUT_GRAPH = OFFICIAL_GRAPH_PATHS["travel"]

# The field IDs are part of the public decision-output/writeback contract.
STD_OUTPUTS = [
    ("f88cbb46-eb13-4ceb-9c68-0983f58e985f", "核销单号", "instance_code"),
    ("48a29115-f542-44d3-8c02-3ff71e19ee38", "稽核代码", "reason_code"),
    ("f35ede49-0eae-4dda-b39e-11a11383697a", "识别结果（warning  reject   pass）", "distinguish_result"),
    ("ae1e04a0-7a6d-4a62-ba4b-734954c8ed5e", "稽核内容", "audit_content"),
    ("14801e5c-ebef-43a4-a56a-23cb5adf4a83", "稽核类型", "audit_type"),
    ("d9a8e0e2-8a13-4a39-b50e-c339519e811e", "识别内容", "distinguish_content"),
    ("f47902a7-1dc2-41c9-8375-fa15174317bd", "发票文件主键", "invoice_file_id"),
    ("629d6a9c-0e78-40c1-baef-0abea4b1e67f", "发票信息主键", "invoice_info_id"),
    ("509fd9ba-3996-4e4a-9021-df6513ed6807", "日志内容", "message"),
    ("a1b2c3d4-0000-0000-0000-regulation0", "制度", "policiesIndex"),
    ("a1b2c3d4-0000-0000-0000-suggestion0", "建议", "employeeSuggestionTips"),
    ("a1b2c3d4-0000-0000-0000-problemcategory0", "问题分类", "problem_category"),
    (
        "a1b2c3d4-0000-0000-0000-optimizationactioncategory0",
        "优化动作分类",
        "optimization_action_category",
    ),
    ("a1b2c3d4-0000-0000-0000-createtime0", "创建时间", "create_time"),
]
INPUT_NAMESPACE = uuid.UUID("f3e9c4b3-e0f2-4d29-9b37-bf2bb3f355e0")
# Feishu currently reuses these public codes across two source rows.  The
# stable rule_key/outputPath, rather than the public code, is the backend
# identity for those rows.  Other repeated legacy codes are normalized with
# explicit occurrence suffixes by the snapshot synchronizer.
_REUSED_PUBLIC_CODES = frozenset({"E32", "E39"})

# Rows whose business result is document-level.  The row number is stable even
# if the text/code in the Feishu sheet is edited later.
_DOCUMENT_SOURCE_ROWS = frozenset({2, 4, 6, 7, 8, 10, 13, 17, 20, 21, 22, 26, 37})
_COMMON_SOURCE_ROWS = frozenset({27, 28, 29, 30, 31, 32, 34, 35, 36})

# Executable mapping.  Display text and metadata always come from the CSV;
# this table only tells the graph/data-preparation seam which normalized state
# to read and whether it is an invoice or document check.
_BEHAVIOR: dict[int, dict[str, Any]] = {
    # ``sources`` is a graph-level safety guard.  The profile normally turns a
    # failed source into a ``missing`` rule state, but keeping the dependency
    # map in the graph also protects direct graph callers that pass a stale
    # ``ruleStates`` map together with ``sourceStatus: NOT_READY``.
    2: {"state": "r02", "formula": "e38", "sources": ("journeys", "cityTransports")},
    3: {"state": "r03", "formula": "taxi_serial", "sources": ()},
    4: {"state": "r04", "formula": "e23", "sources": ("journeys",)},
    5: {"state": "r05", "formula": "date", "sources": ("journeys", "cityTransports")},
    6: {"state": "r06", "formula": "e30", "sources": ("journeys", "airTickets", "trainTickets")},
    7: {"state": "r07", "formula": "e25", "sources": ("businessFeeDetails", "journeys", "travelSubsidies")},
    8: {"state": "r08", "formula": "subsidy", "sources": ("travelSubsidies",)},
    9: {"state": "r09", "formula": "date", "sources": ("journeys", "drivingCars")},
    10: {"state": "r10", "formula": "self_driving", "sources": ("drivingCars",)},
    11: {"state": "r11", "formula": "passenger", "sources": ("otherTransports",)},
    12: {"state": "r12", "formula": "date", "sources": ("journeys", "otherTransports")},
    13: {"state": "r13", "formula": "amount", "sources": ("otherTransports",)},
    14: {"state": "r14", "formula": "passenger", "sources": ("trainTickets",)},
    15: {"state": "r15", "formula": "date", "sources": ("journeys", "trainTickets")},
    16: {"state": "r16", "formula": "seat", "sources": ("trainTickets",)},
    17: {"state": "r17", "formula": "amount", "sources": ("trainTickets",)},
    18: {"state": "r18", "formula": "monthly_train", "sources": ("trainTickets",)},
    19: {"state": "r19", "formula": "date", "sources": ("journeys", "airTickets")},
    20: {"state": "r20", "formula": "amount", "sources": ("otherExpenses",)},
    21: {"state": "r21", "formula": "amount", "sources": ("otherExpenses",)},
    22: {"state": "r22", "formula": "amount", "sources": ("otherExpenses",)},
    23: {"state": "r23", "formula": "baggage", "sources": ("airTickets",)},
    24: {"state": "r24", "formula": "baggage", "sources": ("journeys", "airTickets")},
    25: {"state": "r25", "formula": "baggage", "sources": ("airTickets",)},
    26: {"state": "r26", "formula": "amount", "sources": ("otherExpenses",)},
    27: {"state": "r27", "formula": "e17", "sources": ()},
    28: {"state": "r28", "formula": "sys001", "sources": ()},
    29: {"state": "r29", "formula": "e09", "sources": ()},
    30: {"state": "r30", "formula": "invoice_status", "sources": ()},
    31: {"state": "r31", "formula": "invoice_status", "sources": ()},
    32: {"state": "r32", "formula": "e05", "sources": ()},
    33: {"state": "r33", "formula": "scene", "sources": ()},
    34: {"state": "r34", "formula": "e01", "sources": ()},
    35: {"state": "r35", "formula": "e02", "sources": ()},
    36: {"state": "r36", "formula": "year", "sources": ()},
    37: {"state": "r37", "formula": "tax", "sources": ()},
}


def _stable_id(value: str) -> str:
    return str(uuid.uuid5(INPUT_NAMESPACE, value))


def _literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _std_outputs() -> list[dict[str, str]]:
    return [{"id": fid, "name": name, "field": field} for fid, name, field in STD_OUTPUTS]


def _load_csv_rows(source_path: Path | str | None = None) -> list[dict[str, str]]:
    source = resolve_project_path(source_path, AUDIT_CSV)
    assert source is not None
    if not source.exists():
        raise FileNotFoundError(f"travel rule source not found: {source}")
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 36:
        raise ValueError(f"Expected 36 travel audit rows, found {len(rows)}")
    required = {
        "source_row", "审核时看什么", "费控需要提供的接口", "规则标准",
        "员工端显示报错问题", "制度索引", "问题分类", "优化后具体问题说明",
        "优化动作分类", "优化后建议", "rule_key", "reason_code",
        # Normalized aliases are kept in the snapshot so reviewers and offline
        # consumers do not need to know the Feishu column names.
        "audit_content", "data_dependency", "rule_condition",
        "reason_code_source", "policiesIndex", "problem_category",
        "message", "optimization_action_category", "employeeSuggestionTips",
    }
    missing = required.difference(rows[0]) if rows else required
    if missing:
        raise ValueError(f"travel rule CSV missing columns: {sorted(missing)}")

    # The normalized snapshot is the backend-facing identity map.  A code may
    # appear in several decision-table outcome rows inside one node (PASS /
    # REJECT / WARNING).  E32/E39 are also reused across source rows per the
    # current Feishu sheet; rule_key/outputPath remains the precise identity.
    code_owner: dict[str, str] = {}
    for row in rows:
        rule_key = row.get("rule_key") or row.get("source_row") or "unknown"
        for code in _normalized_codes(row):
            previous = code_owner.get(code)
            if previous is not None and previous != rule_key and code not in _REUSED_PUBLIC_CODES:
                raise ValueError(
                    f"duplicate normalized travel reason code {code!r}: "
                    f"{previous} and {rule_key}"
                )
            code_owner[code] = rule_key
    return rows


def _normalized_codes(row: dict[str, str]) -> list[str]:
    return [code.strip() for code in re.split(r"[|,/、]+", row.get("reason_code", "")) if code.strip()]


def _build_definitions(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        source_row = int(row.get("source_row") or index + 1)
        behavior = dict(_BEHAVIOR.get(source_row) or {"state": f"r{source_row:02d}", "formula": "missing"})
        codes = _normalized_codes(row) or ["UNKNOWN"]
        definition = {
            "row": index,
            "source_row": source_row,
            "rule_key": row.get("rule_key") or f"travel_r{source_row:02d}",
            # key is kept as a compatibility alias for existing callers.
            "key": row.get("rule_key") or f"travel_r{source_row:02d}",
            "name": row.get("审核时看什么", "").strip() or f"差旅稽核规则第{source_row}行",
            "codes": codes,
            "mode": "warning" if codes[0].startswith("W") else "reject",
            "formula": behavior.pop("formula", "missing"),
            "state": behavior.pop("state", f"r{source_row:02d}"),
            "sources": tuple(behavior.pop("sources", ())),
            "document_level": source_row in _DOCUMENT_SOURCE_ROWS,
            "common": source_row in _COMMON_SOURCE_ROWS,
            "content_classifier": source_row == 33,
        }
        definitions.append(definition)
    return definitions


# Public builder metadata.  It is loaded from the checked-in snapshot so
# callers/tests can inspect the exact 36-row mapping without executing main.
RULE_DEFINITIONS = _build_definitions(_load_csv_rows())


def _rule_type(definition: dict[str, Any]) -> str:
    """Keep the historical audit_type contract; G column is a problem label."""
    if definition["source_row"] in {2, 6, 7, 8, 10, 13, 17, 20, 21, 22, 26, 37}:
        return "verification-form"
    if definition["source_row"] in {4, 11, 14, 18, 27, 29, 33}:
        return "staff-behavior"
    return "general-rules"


def _row_value(row: dict[str, str], key: str, *, default: str = "") -> str:
    value = str(row.get(key) or "").strip()
    return "" if value == "/" else (value or default)


def _normalized_row_value(
    row: dict[str, str],
    normalized_key: str,
    source_key: str,
    *,
    default: str = "",
) -> str:
    """Read a normalized snapshot alias, with raw Feishu-column fallback."""
    return _row_value(
        row,
        normalized_key,
        default=_row_value(row, source_key, default=default),
    )


def _failure_message(row: dict[str, str], definition: dict[str, Any]) -> str:
    return _normalized_row_value(
        row, "message", "优化后具体问题说明",
        default=f"{definition['name']}未通过差旅费稽核。",
    )


def _suggestion(row: dict[str, str]) -> str:
    return _normalized_row_value(row, "employeeSuggestionTips", "优化后建议")


def _policy(row: dict[str, str]) -> str:
    return _normalized_row_value(row, "policiesIndex", "制度索引")


def _state_expression(definition: dict[str, Any]) -> str:
    state = definition["state"]
    aliases = {
        # Stable rXX state names are the primary contract.  These descriptive
        # aliases keep old prepared receipts and graph fixtures executable.
        "r02": "e38_city_transport_amount",
        "r03": "e42_taxi_serial",
        "r04": "e23_role_city_transport",
        "r05": "e20_city_transport_date",
        "r06": "e30_station_vehicle",
        "r07": "e25_meal_meeting_subsidy",
        "r08": "e31_subsidy_amount",
        "r09": "e20_self_driving_date",
        "r10": "self_driving_amount",
        "r11": "e29_other_transport_passenger",
        "r12": "e20_other_transport_date",
        "r13": "e31_other_transport_amount",
        "r14": "e29_train_passenger",
        "r15": "e20_train_date",
        "r16": "e32_train_seat",
        "r17": "e31_train_amount",
        "r18": "travel_monthly_train",
        "r19": "e20_flight_date",
        "r20": "e31_vaccine_amount",
        "r21": "e31_network_card_amount",
        "r22": "e31_refund_change_amount",
        "r23": "w37_baggage_airline",
        "r24": "w35_baggage_date",
        "r25": "w38_baggage_weight",
        "r26": "e31_baggage_amount",
        "r27": "e17_recharge_card",
        "r28": "sys001_authenticity",
        "r29": "e09_saler_blacklist",
        "r30": "sys003_void",
        "r31": "sys004_red_flush",
        "r32": "e05_duplicate",
        "r33": "w39_travel_scene",
        "r34": "e01",
        "r35": "e02",
        "r36": "e33_year",
        "r37": "travel_tax_amount",
    }
    alias_expr = f" ?? serviceData.travelAudit.ruleStates.{aliases[state]}" if state in aliases else ""
    # A travelAudit object may exist without the normalized ruleStates map
    # (for example when only common invoice data was prepared).  Guard the
    # nested access explicitly; otherwise the expression engine can skip the
    # whole preprocess node instead of falling back to the invoice fields.
    explicit = (
        f"(serviceData.travelAudit != null and serviceData.travelAudit.ruleStates != null "
        f"? (serviceData.travelAudit.ruleStates.{state}{alias_expr}) : null)"
    )
    fallback = _fallback_expression(definition["formula"])
    evaluated = f"({explicit} ?? ({fallback}))"
    if not definition["common"]:
        evaluated = f"(serviceData.travelAudit != null ? {evaluated} : \"missing\")"
    if definition["document_level"]:
        key = definition["rule_key"]
        key_match = f"some((serviceData.travelAudit.raisedRuleKeys ?? []) as c, c == {_literal(key)})"
        already = f"(serviceData.travelAudit != null and ((serviceData.travelAudit.primaryInvoice == false) or {key_match}))"
        evaluated = f'({already} ? "dedup" : ({evaluated}))'

    # A stale/hand-built ruleStates map must not turn a failed travel API into
    # PASS.  The profile already emits ``missing`` for this case; this second
    # guard makes the graph contract hold when it is invoked directly.  Only
    # dependencies declared for this rule are checked, so common invoice
    # rules (E01/E02/E05/etc.) can still run without travel APIs.
    sources = tuple(definition.get("sources") or ())
    if sources:
        clauses = " or ".join(
            f"(serviceData.travelAudit.sourceStatus.{source} != null and "
            f"serviceData.travelAudit.sourceStatus.{source}.status != {_literal('READY')})"
            for source in sources
        )
        not_ready = f"(serviceData.travelAudit.sourceStatus != null and ({clauses}))"
        evaluated = f'({not_ready} ? "missing" : ({evaluated}))'
    return evaluated


def _fallback_expression(formula: str) -> str:
    # These fallbacks keep the graph usable with old prepared fixtures.  The
    # production path supplies normalized ruleStates from travel/data.py.
    formulas = {
        "e38": '(serviceData.travelAudit.cityTransportApplyAmount ?? "") == "" or (serviceData.travelAudit.cityTransportStandardAmount ?? "") == "" or (serviceData.travelAudit.cityTransportInvoiceAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.cityTransportApplyAmount) <= number(serviceData.travelAudit.cityTransportStandardAmount) and number(serviceData.travelAudit.cityTransportApplyAmount) <= number(serviceData.travelAudit.cityTransportInvoiceAmount) ? "pass" : "reject")',
        "e23": '(serviceData.travelAudit.employeeRole ?? "") == "" or (serviceData.travelAudit.cityTransportApplyAmount ?? "") == "" ? "missing" : ((contains(serviceData.travelAudit.employeeRole, "销售") or contains(serviceData.travelAudit.employeeRole, "售前")) and number(serviceData.travelAudit.cityTransportApplyAmount) > 0 ? "reject" : "pass")',
        "e30": '(serviceData.travelAudit.stationVehicleApplyAmount ?? "") == "" or (serviceData.travelAudit.stationVehicleAllowedAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.stationVehicleApplyAmount) <= number(serviceData.travelAudit.stationVehicleAllowedAmount) ? "pass" : "reject")',
        "e25": '(serviceData.travelAudit.subsidyInfo.mealMeetingApplyAmount ?? "") == "" or (serviceData.travelAudit.subsidyInfo.mealMeetingAllowedAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.subsidyInfo.mealMeetingApplyAmount) <= number(serviceData.travelAudit.subsidyInfo.mealMeetingAllowedAmount) ? "pass" : "reject")',
        "subsidy": '(serviceData.travelAudit.subsidyInfo.applyAmount ?? "") == "" or (serviceData.travelAudit.subsidyInfo.calculatedAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.subsidyInfo.applyAmount) == number(serviceData.travelAudit.subsidyInfo.calculatedAmount) ? "pass" : "reject")',
        "self_driving": '(serviceData.travelAudit.selfDrivingApplyAmount ?? "") == "" or (serviceData.travelAudit.selfDrivingTheoryAmount ?? "") == "" or (serviceData.travelAudit.selfDrivingInvoiceAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.selfDrivingApplyAmount) > number(serviceData.travelAudit.selfDrivingTheoryAmount) ? "reject_theory" : (number(serviceData.travelAudit.selfDrivingApplyAmount) > number(serviceData.travelAudit.selfDrivingInvoiceAmount) ? "reject_invoice" : "pass"))',
        "e17": '(goodsName ?? "") == "" ? "missing" : ((contains(goodsName, "充值卡") or contains(goodsName, "预付卡") or contains(goodsName, "储值卡")) ? "reject" : "pass")',
        "sys001": 'verifyResult == null ? "missing" : (len(verifyResult) == 0 ? "pass" : "reject")',
        "e09": 'salerName == null or serviceData.companyBlacklist == null ? "missing" : (salerName not in map(serviceData.companyBlacklist as c, c.value) ? "pass" : "reject")',
        "e05": '(invoiceNo == null or (serviceData.invoiceUsageHistory == null and serviceData.e05HistoryDuplicateInstanceCodes == null)) ? "missing" : (((serviceData.receiptInvoiceDuplicate ?? false) == true) and (((serviceData.e05HistoryDuplicateInstanceCodes ?? "") != "") or some((serviceData.invoiceUsageHistory ?? []) as c, c.chequeNo == (invoiceNo ?? ""))) ? "reject_both" : (((serviceData.e05HistoryDuplicateInstanceCodes ?? "") != "") or some((serviceData.invoiceUsageHistory ?? []) as c, c.chequeNo == (invoiceNo ?? ""))) ? "reject" : ((serviceData.receiptInvoiceDuplicate ?? false) == true ? "reject_receipt" : "pass"))',
        "monthly_train": 'serviceData.travelAudit.selfBoughtMonthlyTrain == null ? "missing" : (serviceData.travelAudit.selfBoughtMonthlyTrain == true ? "reject" : "pass")',
        "tax": '((serviceData.travelAudit.taxInfo.invoiceDeductibleTaxTotal ?? serviceData.travelAudit.taxInfo.invoiceDeductibleTax ?? "") == "" or (serviceData.travelAudit.taxInfo.formInputTax ?? "") == "") ? "missing" : (number(serviceData.travelAudit.taxInfo.invoiceDeductibleTaxTotal ?? serviceData.travelAudit.taxInfo.invoiceDeductibleTax) == number(serviceData.travelAudit.taxInfo.formInputTax) ? "pass" : "warning")',
        "year": '(invoiceDate ?? "") == "" or serviceData.auditInfo.submitTime == null ? "missing" : (d(invoiceDate).year() == d(serviceData.auditInfo.submitTime).year() ? "pass" : "reject")',
        "e01": '(isInvoiceHeaderMatch ?? serviceData.invoiceHeaderMatch) == null ? "missing" : ((isInvoiceHeaderMatch ?? serviceData.invoiceHeaderMatch) == true ? "pass" : "reject")',
        "e02": '(isInvoiceTaxNumberMatch ?? serviceData.invoiceTaxNumberMatch) == null ? "missing" : ((isInvoiceTaxNumberMatch ?? serviceData.invoiceTaxNumberMatch) == true ? "pass" : "reject")',
        "taxi_serial": '(serviceData.travelAudit.taxiInvoiceSerial == null and serviceData.taxiInvoiceSerial == null) ? "missing" : (((serviceData.travelAudit.taxiInvoiceSerial.historyHit ?? serviceData.taxiInvoiceSerial.historyHit ?? false) or (serviceData.travelAudit.taxiInvoiceSerial.batchHit ?? serviceData.taxiInvoiceSerial.batchHit ?? false)) ? "reject" : ((serviceData.travelAudit.taxiInvoiceSerial.lookupFailed ?? serviceData.taxiInvoiceSerial.lookupFailed ?? false) ? "warning" : "pass"))',
    }
    return formulas.get(formula, '"missing"')


def _rule_row(
    *,
    state: str,
    code: str,
    result: str,
    row: dict[str, str],
    definition: dict[str, Any],
    message: str | None = None,
    distinguish_content: str = "",
    message_is_expression: bool = False,
) -> dict[str, str]:
    content = _normalized_row_value(row, "audit_content", "审核时看什么", default=definition["name"])
    policy = _policy(row)
    suggestion = _suggestion(row)
    return {
        "_id": _stable_id(f"rule:{definition['rule_key']}:{state}:{code}"),
        "_description": _normalized_row_value(row, "rule_condition", "规则标准"),
        # Input field is inserted by _make_decision_node.
        "f88cbb46-eb13-4ceb-9c68-0983f58e985f": "instance_code",
        "48a29115-f542-44d3-8c02-3ff71e19ee38": _literal(code),
        "f35ede49-0eae-4dda-b39e-11a11383697a": _literal(result),
        "ae1e04a0-7a6d-4a62-ba4b-734954c8ed5e": _literal(content),
        "14801e5c-ebef-43a4-a56a-23cb5adf4a83": _literal(_rule_type(definition)),
        "d9a8e0e2-8a13-4a39-b50e-c339519e811e": _literal(distinguish_content),
        "f47902a7-1dc2-41c9-8375-fa15174317bd": "invoice_file_id",
        "629d6a9c-0e78-40c1-baef-0abea4b1e67f": "invoice_info_id",
        "509fd9ba-3996-4e4a-9021-df6513ed6807": (
            message if message_is_expression else _literal(message if message is not None else "")
        ),
        "a1b2c3d4-0000-0000-0000-regulation0": _literal(policy),
        "a1b2c3d4-0000-0000-0000-suggestion0": _literal(suggestion),
        "a1b2c3d4-0000-0000-0000-problemcategory0": _literal(
            _normalized_row_value(row, "problem_category", "问题分类")
        ),
        "a1b2c3d4-0000-0000-0000-optimizationactioncategory0": _literal(
            _normalized_row_value(row, "optimization_action_category", "优化动作分类")
        ),
        "a1b2c3d4-0000-0000-0000-createtime0": "context.executionTime",
    }


def _make_decision_node(definition: dict[str, Any], row: dict[str, str]) -> dict[str, Any]:
    input_id = _stable_id(f"input:{definition['rule_key']}")
    # Keep the historical E05 adapter input/node names for callers that
    # evaluate the common duplicate-invoice table in isolation.  The rule
    # remains keyed by its Feishu source-row rule_key and its output path is
    # still stable/unique, so this compatibility alias cannot collapse any
    # duplicate travel rule.
    input_field = (
        "travel_e05_duplicate_state"
        if definition["source_row"] == 32
        else f"travelAuditState_{definition['source_row']}"
    )
    code = definition["codes"][0]
    pending = f"{definition['name']}所需差旅接口数据缺失或未就绪，无法完成自动稽核，需人工复核。"
    failure = _failure_message(row, definition)
    rules: list[dict[str, str]] = []

    def add(
        state: str,
        result: str,
        output_code: str,
        *,
        message: str | None = None,
        content: str = "",
        message_is_expression: bool = False,
    ) -> None:
        rule = _rule_row(
            state=state,
            code=output_code,
            result=result,
            row=row,
            definition=definition,
            message=message,
            distinguish_content=content,
            message_is_expression=message_is_expression,
        )
        rule[input_id] = _literal(state)
        rules.append(rule)

    add("missing", "WARNING", code, message=pending, content="差旅接口数据缺失或未就绪，无法完成自动稽核，需人工复核")
    add("dedup", "PASS", code)
    add("pass", "PASS", code)

    if definition["source_row"] == 10:
        # The source E cell contains two final codes for this one audit point:
        # the application/theory overage and the insufficient invoice amount.
        # Keep both exact normalized codes instead of reverting to old codes.
        theory_code = definition["codes"][0]
        invoice_code = definition["codes"][1] if len(definition["codes"]) > 1 else definition["codes"][0]
        add("reject_theory", "REJECT", theory_code, message=failure)
        add("reject_invoice", "REJECT", invoice_code, message=failure)
    elif definition["source_row"] == 32:
        # E05's public messages include the duplicate source; preserve the
        # existing common-invoice output contract.
        for state, message in (
            ("reject_both", E05_BOTH_DUPLICATE_MESSAGE),
            ("reject_receipt", E05_RECEIPT_DUPLICATE_MESSAGE),
            ("reject", E05_HISTORY_DUPLICATE_MESSAGE),
        ):
            add(state, "REJECT", code, message=message, message_is_expression=True)
    elif definition["source_row"] == 37:
        # The tax check deliberately reuses E39 but a mismatch is a warning:
        # the amount may require finance review rather than automatic rejection.
        add("warning", "WARNING", code, message=failure)
    else:
        state = "warning" if definition["mode"] == "warning" else "reject"
        result = "WARNING" if definition["mode"] == "warning" else "REJECT"
        add(state, result, code, message=failure)

    return {
        "id": (
            "travel_e05_duplicate_check"
            if definition["source_row"] == 32
            else f"travel_{definition['rule_key']}_check"
        ),
        "type": "decisionTableNode",
        "content": {
            "hitPolicy": "first",
            "rules": rules,
            "inputs": [{"id": input_id, "name": "差旅规则状态", "field": input_field}],
            "outputs": _std_outputs(),
            "passThrough": False,
            "inputField": None,
            "outputPath": f"travel_{definition['rule_key']}_result",
            "executionMode": "single",
        },
        "name": definition["name"],
        "position": {"x": 600, "y": 120 + definition["row"] * 92},
    }


def _make_preprocess_node(definitions: list[dict[str, Any]]) -> dict[str, Any]:
    expressions: list[dict[str, str]] = [
        {"id": _stable_id("expr:travelDataPresent"), "key": "travelDataPresent", "value": "serviceData.travelAudit != null"},
        {"id": _stable_id("expr:travelPrimaryInvoice"), "key": "travelPrimaryInvoice", "value": "(serviceData.travelAudit.primaryInvoice ?? true)"},
    ]
    for definition in definitions:
        state_expression = _state_expression(definition)
        expressions.append(
            {
                "id": _stable_id(f"expr:travelState:{definition['rule_key']}"),
                "key": (
                    "travel_e05_duplicate_state"
                    if definition["source_row"] == 32
                    else f"travelAuditState_{definition['source_row']}"
                ),
                "value": state_expression,
            }
        )
    return {
        "id": "travel_audit_preprocess",
        "type": "expressionNode",
        "content": {
            "expressions": expressions,
            "passThrough": True,
            "inputField": None,
            "outputPath": None,
            "executionMode": "single",
        },
        "name": "差旅稽核数据预处理",
        "position": {"x": 150, "y": 1700},
    }


def _function_node(node_id: str, name: str, source: str, position: dict[str, int]) -> dict[str, Any]:
    return {"id": node_id, "type": "functionNode", "content": {"source": source}, "name": name, "position": position}


def _make_content_nodes() -> list[dict[str, Any]]:
    prompt = _function_node(
        "travel_content_classification_prompt",
        "差旅业务场景分类提示词",
        "export const handler = async (input) => ({ travelContentText: String(input?.goodsName ?? '') });\n",
        {"x": 650, "y": 4000},
    )
    classifier = _function_node(
        "travel_content_classification_llm",
        "差旅业务场景分类（确定性关键词/未知转人工）",
        """export const handler = async (input) => {
  const text = String(input?.travelContentText ?? '').replace(/[\\s\\u3000\\-_/·,.，。()（）]/g, '').toLowerCase();
  if (!text) return { travelContentState: 'missing' };
  const forbidden = ['保险', '餐饮', '餐费', '签证', '快递'];
  const allowed = ['机票', '航空', '飞机', '铁路', '火车', '高铁', '动车', '船票', '轮船', '大巴', '客车', '通行', '租赁', '乘车', '出租', '住宿', '房费', '酒店', '宾馆', '运输服务', '行李', '托运', '疫苗', '电话卡', '上网卡', '退票', '退改'];
  if (forbidden.some((keyword) => text.includes(keyword))) return { travelContentState: 'warning' };
  if (allowed.some((keyword) => text.includes(keyword))) return { travelContentState: 'pass' };
  return { travelContentState: 'warning' };
};
""",
        {"x": 920, "y": 4000},
    )
    postprocess = {
        "id": "travel_content_classification_postprocess",
        "type": "expressionNode",
        "content": {
            "expressions": [
                {
                    "id": _stable_id("expr:travelContentState"),
                    "key": "travelContentState",
                    "value": '(serviceData.travelAudit != null and serviceData.travelAudit.ruleStates != null) ? (serviceData.travelAudit.ruleStates.r33 ?? serviceData.travelAudit.ruleStates.w39_travel_scene ?? travelContentState ?? "missing") : (travelContentState ?? "missing")',
                }
            ],
            "passThrough": True,
            "inputField": None,
            "outputPath": None,
            "executionMode": "single",
        },
        "name": "差旅业务场景分类后处理",
        "position": {"x": 1180, "y": 4000},
    }
    return [prompt, classifier, postprocess]


def _make_content_decision_node(row: dict[str, str], definition: dict[str, Any]) -> dict[str, Any]:
    node = _make_decision_node(definition, row)
    content_input_id = _stable_id("input:travelContentState")
    old_input_id = node["content"]["inputs"][0]["id"]
    node["content"]["inputs"][0] = {"id": content_input_id, "name": "差旅业务场景状态", "field": "travelContentState"}
    for rule in node["content"]["rules"]:
        value = rule.pop(old_input_id, None)
        rule[content_input_id] = value
    return node


def _build_graph(source_path: Path | str | None = None) -> dict[str, Any]:
    if not SOURCE_GRAPH.exists():
        raise FileNotFoundError(f"source graph not found: {SOURCE_GRAPH}")
    source = json.loads(SOURCE_GRAPH.read_text(encoding="utf-8"))
    rows = _load_csv_rows(source_path)
    definitions = _build_definitions(rows)
    row_by_source = {int(row["source_row"]): row for row in rows}

    input_node = deepcopy(next(node for node in source["nodes"] if node["type"] == "inputNode"))
    output_node = deepcopy(next(node for node in source["nodes"] if node["type"] == "outputNode"))
    input_node["position"] = {"x": -920, "y": 1700}
    output_node["position"] = {"x": 1420, "y": 1700}
    nodes: list[dict[str, Any]] = [input_node, output_node, _make_preprocess_node(definitions)]
    edges: list[dict[str, str]] = []

    def add_edge(source_id: str, target_id: str, name: str) -> None:
        edges.append({"id": _stable_id(f"edge:{name}"), "sourceId": source_id, "targetId": target_id, "type": "edge"})

    input_id = input_node["id"]
    output_id = output_node["id"]
    add_edge(input_id, "travel_audit_preprocess", "input-preprocess")

    for definition in definitions:
        row = row_by_source[definition["source_row"]]
        node = _make_content_decision_node(row, definition) if definition["content_classifier"] else _make_decision_node(definition, row)
        nodes.append(node)
        if definition["content_classifier"]:
            add_edge("travel_content_classification_postprocess", node["id"], f"content-post-{definition['rule_key']}")
        else:
            add_edge("travel_audit_preprocess", node["id"], f"preprocess-{definition['rule_key']}")
        add_edge(node["id"], output_id, f"{definition['rule_key']}-output")

    nodes.extend(_make_content_nodes())
    add_edge(input_id, "travel_content_classification_prompt", "input-content-prompt")
    add_edge("travel_content_classification_prompt", "travel_content_classification_llm", "content-prompt-llm")
    add_edge("travel_content_classification_llm", "travel_content_classification_postprocess", "content-llm-postprocess")
    add_edge(input_id, "travel_content_classification_postprocess", "input-content-postprocess")
    return {"contentType": source.get("contentType", "application/vnd.gorules.decision"), "nodes": nodes, "edges": edges}


def build_travel_graph(source_path: Path | str | None = None) -> dict[str, Any]:
    return _build_graph(source_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the travel-expense audit graph")
    parser.add_argument("--source", type=Path, default=None, help="travel rule CSV source")
    args = parser.parse_args(argv)
    graph = build_travel_graph(args.source)
    OUTPUT_GRAPH.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {OUTPUT_GRAPH} ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges)")


if __name__ == "__main__":
    main()
