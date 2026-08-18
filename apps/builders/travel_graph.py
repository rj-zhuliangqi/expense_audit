"""Build the travel-expense audit decision graph.

The graph is intentionally data-contract-first: common invoice checks remain
compatible with the existing GoRules output shape, while travel-specific data
is read from ``serviceData.travelAudit``.  Missing travel data is reported as a
WARNING requiring manual review; it must never be silently treated as PASS.

Usage::

    python build_travel_graph.py

Output: graph-latest-travel-0807.json
"""
from __future__ import annotations

import csv
import json
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from expense_audit_orchestrator.paths import LEGACY_TRAVEL_RULE_SOURCE, OFFICIAL_GRAPH_PATHS, PROJECT_ROOT

ROOT = PROJECT_ROOT
SOURCE_GRAPH = OFFICIAL_GRAPH_PATHS["telecom"]
AUDIT_CSV = LEGACY_TRAVEL_RULE_SOURCE
OUTPUT_GRAPH = OFFICIAL_GRAPH_PATHS["travel"]

# These IDs are part of the existing graph/writeback contract.  They are
# deliberately reused for every travel decision table.
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
    ("a1b2c3d4-0000-0000-0000-createtime0", "创建时间", "create_time"),
]
STD_OUTPUT_MAP = {field: field_id for field_id, _name, field in STD_OUTPUTS}
INPUT_NAMESPACE = uuid.UUID("f3e9c4b3-e0f2-4d29-9b37-bf2bb3f355e0")


def _stable_id(value: str) -> str:
    return str(uuid.uuid5(INPUT_NAMESPACE, value))


def _literal(value: Any) -> str:
    """Encode a Python value as a Zen/FEEL string literal."""
    return json.dumps(value, ensure_ascii=False)


def _slug(text: str) -> str:
    text = re.sub(r"[^0-9A-Za-z一-龥]+", "_", text).strip("_")
    return text.lower() or "rule"


def _std_outputs() -> list[dict[str, str]]:
    return [{"id": fid, "name": name, "field": field} for fid, name, field in STD_OUTPUTS]


def _load_csv_rows() -> list[dict[str, str]]:
    with AUDIT_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 32:
        raise ValueError(f"Expected 32 travel audit rows, found {len(rows)}")
    return rows


# The CSV is the source of truth for display text and policy metadata.  The
# key/behavior fields below are the executable graph mapping for each row.
RULE_DEFINITIONS: list[dict[str, Any]] = [
    {"row": 1, "key": "e38_city_transport_amount", "name": "市内交通费及场站用车金额检查", "codes": ["E38"], "mode": "reject", "formula": "e38", "document_level": True},
    {"row": 2, "key": "e23_role_city_transport", "name": "特定岗位市内交通费检查", "codes": ["E23"], "mode": "reject", "formula": "e23", "document_level": True},
    {"row": 3, "key": "e20_city_transport_date", "name": "市内交通票据日期检查", "codes": ["E20"], "mode": "reject"},
    {"row": 4, "key": "e30_station_vehicle", "name": "场站用车报销标准检查", "codes": ["E30"], "mode": "reject", "formula": "e30", "document_level": True},
    {"row": 5, "key": "e25_meal_meeting_subsidy", "name": "含餐会议公杂补贴检查", "codes": ["E25"], "mode": "reject", "formula": "e25", "document_level": True},
    {"row": 6, "key": "e31_subsidy_amount", "name": "公杂补贴金额检查", "codes": ["E31"], "mode": "reject", "formula": "e31_subsidy", "document_level": True},
    {"row": 7, "key": "e20_self_driving_date", "name": "自驾车票据日期检查", "codes": ["E20"], "mode": "reject"},
    {"row": 8, "key": "self_driving_amount", "name": "自驾车费用金额检查", "codes": ["E32", "E31"], "mode": "multi_reject", "formula": "self_driving_amount", "document_level": True},
    {"row": 9, "key": "e29_other_transport_passenger", "name": "轮船客车旅客姓名检查", "codes": ["E29"], "mode": "reject"},
    {"row": 10, "key": "e20_other_transport_date", "name": "轮船客车票据日期检查", "codes": ["E20"], "mode": "reject"},
    {"row": 11, "key": "e31_other_transport_amount", "name": "轮船客车金额检查", "codes": ["E31"], "mode": "reject", "document_level": True},
    {"row": 12, "key": "e29_train_passenger", "name": "火车票旅客姓名检查", "codes": ["E29"], "mode": "reject"},
    {"row": 13, "key": "e20_train_date", "name": "火车票行程日期检查", "codes": ["E20"], "mode": "reject"},
    {"row": 14, "key": "e32_train_seat", "name": "火车票座位等级检查", "codes": ["E32"], "mode": "reject"},
    {"row": 15, "key": "e31_train_amount", "name": "火车票金额检查", "codes": ["E31"], "mode": "reject", "document_level": True},
    {"row": 16, "key": "e20_flight_date", "name": "机票行程日期检查", "codes": ["E20"], "mode": "reject"},
    {"row": 17, "key": "e31_vaccine_amount", "name": "疫苗检查费金额检查", "codes": ["E31"], "mode": "reject", "document_level": True},
    {"row": 18, "key": "e31_network_card_amount", "name": "网络电话卡费金额检查", "codes": ["E31"], "mode": "reject", "document_level": True},
    {"row": 19, "key": "e31_refund_change_amount", "name": "退改签金额检查", "codes": ["E31"], "mode": "reject", "document_level": True},
    {"row": 20, "key": "w37_baggage_airline", "name": "行李托运航司检查", "codes": ["W37"], "mode": "warning"},
    {"row": 21, "key": "w35_baggage_date", "name": "行李托运日期检查", "codes": ["W35"], "mode": "warning"},
    {"row": 22, "key": "w38_baggage_weight", "name": "行李托运重量检查", "codes": ["W38"], "mode": "warning"},
    {"row": 23, "key": "e31_baggage_amount", "name": "行李托运费金额检查", "codes": ["E31"], "mode": "reject", "document_level": True},
    {"row": 24, "key": "e17_recharge_card", "name": "差旅充值卡发票检查", "codes": ["E17"], "mode": "reject", "formula": "e17"},
    {"row": 25, "key": "sys001_authenticity", "name": "发票真伪检查", "codes": ["sys-001"], "mode": "reject", "formula": "sys001"},
    {"row": 26, "key": "e09_saler_blacklist", "name": "销方黑名单检查", "codes": ["E09"], "mode": "reject", "formula": "e09"},
    {"row": 27, "key": "sys003_void", "name": "发票作废检查", "codes": ["sys-003"], "mode": "reject"},
    {"row": 28, "key": "sys004_red_flush", "name": "发票红冲检查", "codes": ["sys-004"], "mode": "reject"},
    {"row": 29, "key": "e05_duplicate", "name": "发票重复使用检查", "codes": ["E05"], "mode": "reject", "formula": "e05"},
    {"row": 30, "key": "w39_travel_scene", "name": "差旅发票业务场景检查", "codes": ["W39"], "mode": "warning", "content_classifier": True},
    {"row": 31, "key": "travel_tax_amount", "name": "发票可抵扣税额检查", "codes": ["TRAVEL-TAX-001"], "mode": "warning", "formula": "tax", "document_level": True},
    {"row": 32, "key": "travel_monthly_train", "name": "自行报销月结火车票检查", "codes": ["TRAVEL-TRAIN-001"], "mode": "reject", "formula": "monthly_train"},
]


# These checks use the existing invoice-level inputs and must remain runnable
# even before the travel-specific interface is connected.  Travel-specific rows
# are gated by serviceData.travelAudit; absent data produces WARNING/manual review,
# never an automatic PASS.
COMMON_RULE_KEYS = {
    "e17_recharge_card",
    "sys001_authenticity",
    "e09_saler_blacklist",
    "sys003_void",
    "sys004_red_flush",
    "e05_duplicate",
}


def _rule_state_expression(definition: dict[str, Any]) -> str:
    key = definition["key"]
    code = definition["codes"][0]
    # Explicit ruleStates is the stable integration seam.  It lets upstream
    # data preparation implement complex date/order calculations independently.
    # Guard the nested lookup so common invoice checks can still run when the
    # travel-specific serviceData.travelAudit object is not present yet.
    explicit = f'(serviceData.travelAudit != null ? serviceData.travelAudit.ruleStates.{key} : null)'
    formula = definition.get("formula")
    fallback = _formula_expression(formula, key, code) if formula else '"missing"'
    evaluated = f'({explicit} ?? ({fallback}))'
    if definition["key"] in COMMON_RULE_KEYS:
        base = evaluated
    else:
        base = f'($.travelDataPresent ? {evaluated} : "missing")'
    if definition.get("document_level"):
        # ``raisedRuleKeys`` prevents a same-code collision (E32 is both the
        # self-driving amount rule and the per-ticket train-seat rule).  Keep
        # the code fallback for the historical document-level E31/amount
        # semantics and for callers that only provide raisedRuleCodes.
        key_match = f'some((serviceData.travelAudit.raisedRuleKeys ?? []) as c, c == {_literal(key)})'
        code_match = "false"
        if code in {"E38", "E23", "E30", "E25", "E31", "TRAVEL-TAX-001"}:
            code_match = f'some((serviceData.travelAudit.raisedRuleCodes ?? []) as c, c == {_literal(code)})'
        already = f'(serviceData.travelAudit != null and (serviceData.travelAudit.primaryInvoice == false or {key_match} or {code_match}))'
        base = f'({already} ? "dedup" : ({base}))'
    return base


def _formula_expression(formula: str | None, key: str, code: str) -> str:
    """Return a conservative fallback for rules with stable scalar inputs."""
    if formula == "e38":
        return '(serviceData.travelAudit.cityTransportApplyAmount ?? "") == "" or (serviceData.travelAudit.cityTransportStandardAmount ?? "") == "" or (serviceData.travelAudit.cityTransportInvoiceAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.cityTransportApplyAmount) <= number(serviceData.travelAudit.cityTransportStandardAmount) and number(serviceData.travelAudit.cityTransportApplyAmount) <= number(serviceData.travelAudit.cityTransportInvoiceAmount) ? "pass" : "reject")'
    if formula == "e23":
        return '(serviceData.travelAudit.employeeRole ?? "") == "" or (serviceData.travelAudit.cityTransportApplyAmount ?? "") == "" ? "missing" : ((contains(serviceData.travelAudit.employeeRole, "销售") or contains(serviceData.travelAudit.employeeRole, "售前")) and number(serviceData.travelAudit.cityTransportApplyAmount) > 0 ? "reject" : "pass")'
    if formula == "e30":
        return '(serviceData.travelAudit.stationVehicleApplyAmount ?? "") == "" or (serviceData.travelAudit.stationVehicleAllowedAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.stationVehicleApplyAmount) <= number(serviceData.travelAudit.stationVehicleAllowedAmount) ? "pass" : "reject")'
    if formula == "e25":
        return '(serviceData.travelAudit.subsidyInfo.mealMeetingApplyAmount ?? "") == "" or (serviceData.travelAudit.subsidyInfo.mealMeetingAllowedAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.subsidyInfo.mealMeetingApplyAmount) <= number(serviceData.travelAudit.subsidyInfo.mealMeetingAllowedAmount) ? "pass" : "reject")'
    if formula == "e31_subsidy":
        return '(serviceData.travelAudit.subsidyInfo.applyAmount ?? "") == "" or (serviceData.travelAudit.subsidyInfo.calculatedAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.subsidyInfo.applyAmount) == number(serviceData.travelAudit.subsidyInfo.calculatedAmount) ? "pass" : "reject")'
    if formula == "self_driving_amount":
        return '(serviceData.travelAudit.selfDrivingApplyAmount ?? "") == "" or (serviceData.travelAudit.selfDrivingTheoryAmount ?? "") == "" or (serviceData.travelAudit.selfDrivingInvoiceAmount ?? "") == "" ? "missing" : (number(serviceData.travelAudit.selfDrivingApplyAmount) > number(serviceData.travelAudit.selfDrivingTheoryAmount) ? "reject_theory" : (number(serviceData.travelAudit.selfDrivingApplyAmount) > number(serviceData.travelAudit.selfDrivingInvoiceAmount) ? "reject_invoice" : "pass"))'
    if formula == "e17":
        return '(goodsName ?? "") == "" ? "missing" : ((contains(goodsName, "充值卡") or contains(goodsName, "预付卡") or contains(goodsName, "储值卡")) ? "reject" : "pass")'
    if formula == "sys001":
        return 'verifyResult == null ? "missing" : (len(verifyResult) == 0 ? "pass" : "reject")'
    if formula == "e09":
        return 'salerName == null or serviceData.companyBlacklist == null ? "missing" : (salerName not in map(serviceData.companyBlacklist as c, c.value) ? "pass" : "reject")'
    if formula == "e05":
        return 'invoiceNo == null or serviceData.invoiceUsageHistory == null ? "missing" : (invoiceNo not in map(serviceData.invoiceUsageHistory as c, c.chequeNo) ? "pass" : "reject")'
    if formula == "tax":
        return '((serviceData.travelAudit.taxInfo.invoiceDeductibleTaxTotal ?? serviceData.travelAudit.taxInfo.invoiceDeductibleTax ?? "") == "" or (serviceData.travelAudit.taxInfo.formInputTax ?? "") == "") ? "missing" : (number(serviceData.travelAudit.taxInfo.invoiceDeductibleTaxTotal ?? serviceData.travelAudit.taxInfo.invoiceDeductibleTax) == number(serviceData.travelAudit.taxInfo.formInputTax) ? "pass" : "warning")'
    if formula == "monthly_train":
        return 'serviceData.travelAudit.selfBoughtMonthlyTrain == null ? "missing" : (serviceData.travelAudit.selfBoughtMonthlyTrain == true ? "reject" : "pass")'
    return '"missing"'


def _pending_message(name: str) -> str:
    return f"{name}所需差旅接口数据缺失或未就绪，无法完成自动稽核，需人工复核。"


def _rule_content(row: dict[str, str], definition: dict[str, Any]) -> str:
    return row.get("审核时看什么", "").strip() or definition["name"]


def _rule_type(row: dict[str, str]) -> str:
    category = row.get("稽核类别", "")
    if "金额稽核" in category:
        return "verification-form"
    if "业务" in category:
        return "staff-behavior"
    return "general-rules"


def _rule_message(row: dict[str, str], definition: dict[str, Any]) -> str:
    message = row.get("message", "").strip()
    if message:
        return message
    if definition["codes"][0] == "TRAVEL-TAX-001":
        return "本次差旅费发票可抵扣税额与核销单填写进项税额不一致。"
    if definition["codes"][0] == "TRAVEL-TRAIN-001":
        return "检测到自行报销月结火车票，请核对票据来源和费用类型。"
    return f"{definition['name']}未通过差旅费稽核。"


def _rule_suggestion(row: dict[str, str], definition: dict[str, Any]) -> str:
    suggestion = row.get("建议", "").strip()
    if suggestion and suggestion != "/":
        return suggestion
    if definition["codes"][0] == "TRAVEL-TAX-001":
        return "【核对税额】请核对发票可抵扣税额和表单进项税额。"
    if definition["codes"][0] == "TRAVEL-TRAIN-001":
        return "【核对票据】请确认火车票是否属于月结订单，避免自行重复报销。"
    return "【补充或更正】请核对差旅行程、费用金额和对应票据。"


def _rule_row(
    *,
    state: str,
    code: str,
    result: str,
    audit_content: str,
    audit_type: str,
    message: str,
    policy: str,
    suggestion: str,
    distinguish_content: str = "",
) -> dict[str, str]:
    return {
        "_id": _stable_id(f"rule:{code}:{state}:{audit_content}"),
        "_description": "",
        # The input field ID is added by _make_decision_table.
        "f88cbb46-eb13-4ceb-9c68-0983f58e985f": "instance_code",
        "48a29115-f542-44d3-8c02-3ff71e19ee38": _literal(code),
        "f35ede49-0eae-4dda-b39e-11a11383697a": _literal(result),
        "ae1e04a0-7a6d-4a62-ba4b-734954c8ed5e": _literal(audit_content),
        "14801e5c-ebef-43a4-a56a-23cb5adf4a83": _literal(audit_type),
        "d9a8e0e2-8a13-4a39-b50e-c339519e811e": _literal(distinguish_content),
        "f47902a7-1dc2-41c9-8375-fa15174317bd": "invoice_file_id",
        "629d6a9c-0e78-40c1-baef-0abea4b1e67f": "invoice_info_id",
        "509fd9ba-3996-4e4a-9021-df6513ed6807": _literal(message),
        "a1b2c3d4-0000-0000-0000-regulation0": _literal(policy),
        "a1b2c3d4-0000-0000-0000-suggestion0": _literal(suggestion),
        "a1b2c3d4-0000-0000-0000-createtime0": "context.executionTime",
    }


def _make_decision_node(
    definition: dict[str, Any],
    row: dict[str, str],
) -> dict[str, Any]:
    key = definition["key"]
    state_key = f"travel_{key}_state"
    input_id = _stable_id(f"input:{key}")
    audit_content = _rule_content(row, definition)
    audit_type = _rule_type(row)
    policy = row.get("制度索引", "").strip()
    if policy == "/":
        policy = ""
    failure_message = _rule_message(row, definition)
    suggestion = _rule_suggestion(row, definition)
    pending = _pending_message(definition["name"])
    rules: list[dict[str, str]] = []

    rules.append(_rule_row(
        state="missing", code=definition["codes"][0], result="WARNING",
        audit_content=audit_content, audit_type=audit_type,
        message=pending, policy=policy, suggestion="",
        distinguish_content="差旅接口数据缺失或未就绪，无法完成自动稽核，需人工复核",
    ))
    rules[-1][input_id] = _literal("missing")
    rules.append(_rule_row(
        state="dedup", code=definition["codes"][0], result="PASS",
        audit_content=audit_content, audit_type=audit_type,
        message="", policy=policy, suggestion="",
    ))
    rules[-1][input_id] = _literal("dedup")
    rules.append(_rule_row(
        state="pass", code=definition["codes"][0], result="PASS",
        audit_content=audit_content, audit_type=audit_type,
        message="", policy=policy, suggestion="",
    ))
    rules[-1][input_id] = _literal("pass")

    if definition.get("mode") == "multi_reject":
        rules.append(_rule_row(
            state="reject_theory", code="E32", result="REJECT",
            audit_content=audit_content, audit_type=audit_type,
            message=failure_message, policy=policy, suggestion=suggestion,
        ))
        rules[-1][input_id] = _literal("reject_theory")
        rules.append(_rule_row(
            state="reject_invoice", code="E31", result="REJECT",
            audit_content=audit_content, audit_type=audit_type,
            message=failure_message, policy=policy, suggestion=suggestion,
        ))
        rules[-1][input_id] = _literal("reject_invoice")
    else:
        result = "WARNING" if definition["mode"] == "warning" else "REJECT"
        state = "warning" if definition["mode"] == "warning" else "reject"
        rules.append(_rule_row(
            state=state, code=definition["codes"][0], result=result,
            audit_content=audit_content, audit_type=audit_type,
            message=failure_message, policy=policy, suggestion=suggestion,
        ))
        rules[-1][input_id] = _literal(state)

    return {
        "id": f"travel_{key}_check",
        "type": "decisionTableNode",
        "content": {
            "hitPolicy": "first",
            "rules": rules,
            "inputs": [{"id": input_id, "name": "差旅规则状态", "field": state_key}],
            "outputs": _std_outputs(),
            "passThrough": False,
            "inputField": None,
            "outputPath": f"travel_{key}_result",
            "executionMode": "single",
        },
        "name": definition["name"],
        "position": {"x": 600, "y": 160 + definition["row"] * 92},
    }


def _make_preprocess_node() -> dict[str, Any]:
    expressions: list[dict[str, str]] = [
        {"id": _stable_id("expr:travelDataPresent"), "key": "travelDataPresent", "value": "serviceData.travelAudit != null"},
        {"id": _stable_id("expr:travelPrimaryInvoice"), "key": "travelPrimaryInvoice", "value": "(serviceData.travelAudit.primaryInvoice ?? true)"},
    ]
    for definition in RULE_DEFINITIONS:
        if definition.get("content_classifier"):
            continue
        expressions.append({
            "id": _stable_id(f"expr:{definition['key']}"),
            "key": f"travel_{definition['key']}_state",
            "value": _rule_state_expression(definition),
        })
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
        "position": {"x": 150, "y": 1600},
    }


def _function_node(node_id: str, name: str, source: str, position: dict[str, int]) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "functionNode",
        "content": {"source": source},
        "name": name,
        "position": position,
    }


def _make_content_nodes() -> list[dict[str, Any]]:
    # 发票内容唯一来自数据准备阶段生成的单据级 goodsName；流程图不再
    # 读取 contents/context.contents，也不再重复拼接或清洗明细。
    prompt = _function_node(
        "travel_content_classification_prompt",
        "差旅业务场景分类提示词",
        """export const handler = async (input) => ({\n  travelContentPrompt: '判断发票内容是否属于差旅费允许报销业务场景。',\n  travelContentText: String(input?.goodsName ?? '')\n});\n""",
        {"x": 650, "y": 3120},
    )
    classifier = _function_node(
        "travel_content_classification_llm",
        "差旅业务场景分类（确定性关键词/未知转人工）",
        """export const handler = async (input) => {\n  const text = String(input?.travelContentText ?? '').replace(/[\\s\\u3000\\-_/·,.，。()（）]/g, '').toLowerCase();\n  if (!text) return { travelContentState: 'missing', travelContentClassification: 'missing' };\n  const forbidden = ['保险', '餐饮', '餐费', '签证', '快递'];\n  const allowed = ['机票', '航空', '飞机', '铁路', '火车', '高铁', '动车', '船票', '轮船', '大巴', '客车', '通行', '租赁', '乘车', '出租', '滴滴', '住宿', '房费', '酒店', '宾馆', '运输服务', '行李', '托运', '诊疗', '疫苗', '电信服务', '电话卡', '上网卡', '退票', '退改', '改期'];\n  if (forbidden.some((keyword) => text.includes(keyword))) return { travelContentState: 'warning', travelContentClassification: 'forbidden' };\n  if (allowed.some((keyword) => text.includes(keyword))) return { travelContentState: 'pass', travelContentClassification: 'allowed' };\n  return { travelContentState: 'warning', travelContentClassification: 'unknown' };\n};\n""",
        {"x": 920, "y": 3120},
    )
    postprocess = {
        "id": "travel_content_classification_postprocess",
        "type": "expressionNode",
        "content": {
            "expressions": [{
                "id": _stable_id("expr:travelContentState"),
                "key": "travelContentState",
                "value": '(serviceData.travelAudit != null and serviceData.travelAudit.ruleStates.w39_travel_scene != null) ? serviceData.travelAudit.ruleStates.w39_travel_scene : (travelContentState ?? "missing")',
            }],
            "passThrough": True,
            "inputField": None,
            "outputPath": None,
            "executionMode": "single",
        },
        "name": "差旅业务场景分类后处理",
        "position": {"x": 1180, "y": 3120},
    }
    return [prompt, classifier, postprocess]


def _make_content_decision_node(row: dict[str, str], definition: dict[str, Any]) -> dict[str, Any]:
    node = _make_decision_node(definition, row)
    # The content chain produces travelContentState rather than the normal
    # travel_*_state key.
    node["content"]["inputs"][0]["field"] = "travelContentState"
    node["content"]["inputs"][0]["id"] = _stable_id("input:travelContentState")
    for rule in node["content"]["rules"]:
        old_id = _stable_id(f"input:{definition['key']}")
        value = rule.pop(old_id, None)
        rule[_stable_id("input:travelContentState")] = value
    return node


def _build_graph() -> dict[str, Any]:
    source = json.loads(SOURCE_GRAPH.read_text(encoding="utf-8"))
    rows = _load_csv_rows()
    row_map = {i + 1: row for i, row in enumerate(rows)}

    # Preserve the canonical request/response node shape and source metadata,
    # but intentionally rebuild telecom-specific branches for travel.
    input_node = deepcopy(next(node for node in source["nodes"] if node["type"] == "inputNode"))
    output_node = deepcopy(next(node for node in source["nodes"] if node["type"] == "outputNode"))
    input_node["position"] = {"x": -920, "y": 1700}
    output_node["position"] = {"x": 1420, "y": 1700}

    nodes: list[dict[str, Any]] = [input_node, output_node, _make_preprocess_node()]
    edges: list[dict[str, str]] = []

    def add_edge(source_id: str, target_id: str, edge_name: str) -> None:
        edges.append({
            "id": _stable_id(f"edge:{edge_name}"),
            "sourceId": source_id,
            "targetId": target_id,
            "type": "edge",
        })

    input_id = input_node["id"]
    output_id = output_node["id"]
    preprocess_id = "travel_audit_preprocess"
    add_edge(input_id, preprocess_id, "input-preprocess")

    for definition in RULE_DEFINITIONS:
        row = row_map[definition["row"]]
        if definition.get("content_classifier"):
            node = _make_content_decision_node(row, definition)
        else:
            node = _make_decision_node(definition, row)
        nodes.append(node)
        node_id = node["id"]
        if definition.get("content_classifier"):
            # Content chain is inserted below; the decision receives the
            # postprocessed classification state.
            add_edge("travel_content_classification_postprocess", node_id, f"content-post-{definition['key']}")
        else:
            add_edge(preprocess_id, node_id, f"preprocess-{definition['key']}")
        add_edge(node_id, output_id, f"{definition['key']}-output")

    content_nodes = _make_content_nodes()
    nodes.extend(content_nodes)
    add_edge(input_id, "travel_content_classification_prompt", "input-content-prompt")
    add_edge("travel_content_classification_prompt", "travel_content_classification_llm", "content-prompt-llm")
    add_edge("travel_content_classification_llm", "travel_content_classification_postprocess", "content-llm-postprocess")
    add_edge(input_id, "travel_content_classification_postprocess", "input-content-postprocess")

    return {"contentType": source.get("contentType", "application/vnd.gorules.decision"), "nodes": nodes, "edges": edges}


def build_travel_graph() -> dict[str, Any]:
    return _build_graph()


def main() -> None:
    graph = build_travel_graph()
    OUTPUT_GRAPH.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {OUTPUT_GRAPH} ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges)")


if __name__ == "__main__":
    main()
