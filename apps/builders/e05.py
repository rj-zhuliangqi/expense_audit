"""Shared graph helpers for the common E05 invoice-duplicate rule.

E05 is evaluated once per invoice by the graph runtime, but its second input
(``receiptInvoiceDuplicate``) is calculated at reimbursement-document scope by
``ReceiptAuditService``.  Keeping the node patch here prevents each expense
profile from carrying a different E05 implementation.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

E05_HISTORY_INPUT_ID = "e05-history-duplicate-input"
E05_RECEIPT_INPUT_ID = "e05-receipt-invoice-duplicate-input"
E05_RECEIPT_DUPLICATE_EXPRESSION = "serviceData.receiptInvoiceDuplicate ?? false"
MESSAGE_FIELD_ID = "509fd9ba-3996-4e4a-9021-df6513ed6807"
RESULT_FIELD_ID = "f35ede49-0eae-4dda-b39e-11a11383697a"

_HISTORY_INSTANCE_CODES = (
    '((serviceData.e05HistoryDuplicateInstanceCodes ?? "") != "" ? '
    'serviceData.e05HistoryDuplicateInstanceCodes : '
    '(filter((serviceData.invoiceUsageHistory ?? []) as h, '
    'h.chequeNo == (invoiceNo ?? ""))[0].miInstanceCode ?? '
    'filter((serviceData.invoiceUsageHistory ?? []) as h, '
    'h.chequeNo == (invoiceNo ?? ""))[0].instanceCode ?? "未知"))'
)
_HISTORY_AMOUNT = (
    '(serviceData.e05HistoryDuplicateAmount ?? '
    'filter((serviceData.invoiceUsageHistory ?? []) as h, '
    'h.chequeNo == (invoiceNo ?? ""))[0].estimatedTotalAmount ?? '
    'filter((serviceData.invoiceUsageHistory ?? []) as h, '
    'h.chequeNo == (invoiceNo ?? ""))[0].totalAmount ?? 0)'
)

E05_HISTORY_DUPLICATE_MESSAGE = (
    '"票据 发票号 " + (invoiceNo ?? "") + '
    '" 已在以下报销单中使用过：" + '
    + _HISTORY_INSTANCE_CODES
    + ' + "；已报销金额为 " + string(number('
    + _HISTORY_AMOUNT
    + ')) + " 元，不能再次用于本次报销。"'
)
E05_RECEIPT_DUPLICATE_MESSAGE = (
    '"票据 发票号 " + (invoiceNo ?? "") + '
    '" 在本核销单内重复出现，不能再次用于本次报销。"'
)
E05_BOTH_DUPLICATE_MESSAGE = (
    '"票据 发票号 " + (invoiceNo ?? "") + '
    '" 在本核销单内重复出现，且已在以下报销单中使用过：" + '
    + _HISTORY_INSTANCE_CODES
    + ' + "；已报销金额为 " + string(number('
    + _HISTORY_AMOUNT
    + ')) + " 元，不能再次用于本次报销。"'
)


def patch_e05_node(node: dict[str, Any]) -> dict[str, Any]:
    """Normalize an existing E05 decision table to the common two-input form.

    The source graphs already contain the standard history-only E05 node.  This
    function preserves its output metadata and replaces only the input/rule
    matrix, so it can be used by every profile graph builder and by migration
    scripts for checked-in graph artifacts.
    """
    content = node.setdefault("content", {})
    inputs = list(content.get("inputs") or [])
    history_input = next(
        (item for item in inputs if item.get("field") == "isWriteOff"),
        None,
    )
    if history_input is None:
        history_input = {"id": E05_HISTORY_INPUT_ID, "name": "发票是否被其他核销单使用", "field": "isWriteOff"}
    else:
        history_input = deepcopy(history_input)
        history_input["id"] = E05_HISTORY_INPUT_ID
        history_input["name"] = "发票是否被其他核销单使用"
        history_input["field"] = "isWriteOff"

    receipt_input = {
        "id": E05_RECEIPT_INPUT_ID,
        "name": "发票是否在本核销单内重复",
        "field": "isReceiptInvoiceDuplicate",
    }

    rules = list(content.get("rules") or [])
    history_reject = next(
        (rule for rule in rules if rule.get(history_input.get("id")) == "false"),
        None,
    )
    if history_reject is None:
        # Source tables may use a profile-specific input id; locate the false
        # row before the input id is normalized.
        old_history_id = next(
            (item.get("id") for item in inputs if item.get("field") == "isWriteOff"),
            None,
        )
        history_reject = next(
            (rule for rule in rules if old_history_id and rule.get(old_history_id) == "false"),
            None,
        )
    history_pass = next(
        (rule for rule in rules if rule.get(history_input.get("id")) == "true"),
        None,
    )
    if history_pass is None:
        old_history_id = next(
            (item.get("id") for item in inputs if item.get("field") == "isWriteOff"),
            None,
        )
        history_pass = next(
            (rule for rule in rules if old_history_id and rule.get(old_history_id) == "true"),
            None,
        )
    if history_reject is None or history_pass is None:
        raise ValueError(f"E05 node {node.get('id')!r} must contain true/false history rows")

    def make_rule(
        source: dict[str, Any],
        rule_id: str,
        history: str,
        receipt: str,
        message: str,
        result: str | None = None,
    ) -> dict[str, Any]:
        rule = deepcopy(source)
        rule["_id"] = rule_id
        # Remove any old input columns and write the canonical two conditions.
        for item in inputs:
            old_id = item.get("id")
            if old_id:
                rule.pop(old_id, None)
        rule[E05_HISTORY_INPUT_ID] = history
        rule[E05_RECEIPT_INPUT_ID] = receipt
        rule[MESSAGE_FIELD_ID] = message
        if result is not None:
            rule[RESULT_FIELD_ID] = result
        return rule

    content["inputs"] = [history_input, receipt_input]
    content["rules"] = [
        make_rule(
            history_reject,
            "e05-history-and-receipt-duplicate",
            "false",
            "true",
            E05_BOTH_DUPLICATE_MESSAGE,
        ),
        make_rule(
            history_reject,
            "e05-history-duplicate",
            "false",
            "false",
            E05_HISTORY_DUPLICATE_MESSAGE,
        ),
        make_rule(
            history_reject,
            "e05-receipt-duplicate",
            "true",
            "true",
            E05_RECEIPT_DUPLICATE_MESSAGE,
        ),
        # A PASS row must not carry the reject-only remediation copied from
        # the source history row.  Otherwise a successful E05 still appears
        # with "删除重复发票" / "重复报销" in writeback.
        make_rule(history_pass, "e05-no-duplicate", "true", "false", '""', '"PASS"')
        | {
            "a1b2c3d4-0000-0000-0000-suggestion0": '""',
            "a1b2c3d4-0000-0000-0000-problemcategory0": '""',
            "a1b2c3d4-0000-0000-0000-optimizationactioncategory0": '""',
        },
    ]
    return node
