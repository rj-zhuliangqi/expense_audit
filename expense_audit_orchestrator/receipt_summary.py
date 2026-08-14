"""Receipt-level amount aggregation used by orchestration and writeback.

The graph still runs one invoice at a time.  This module deliberately performs
receipt-level aggregation only after all invoice results are available, so the
summary cannot depend on whichever invoice happened to run last.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


# E31 is the receipt-level amount sufficiency rule.  E34 returns the amount
# remaining after non-reimbursable invoice lines are deducted, so both rules are
# allowed to contribute their finalAmount.  Any other blocking reject/failed
# rule makes the whole invoice invalid for the receipt-level total.
_INVOICE_FINAL_AMOUNT_EXEMPT_RULE_CODES = frozenset({"E31", "E34"})


def build_ai_audit_summary(
    prepared_receipt: Mapping[str, Any],
    processed_receipt: Mapping[str, Any],
) -> str | None:
    """Build the fixed receipt-level ``aiAuditSummary`` text.

    Amounts are parsed as :class:`~decimal.Decimal` from their string form, not
    through binary floating point.  The four values are calculated as:

    * application amount: receipt ``auditInfo.applyAmount``;
    * submitted invoice total: sum of every invoice OCR amount, regardless of
      whether that invoice is later rejected;
    * valid reimbursable total: sum of valid invoices' ``invoice_finalAmount``;
    * amount to supplement: ``max(application - valid, 0)``.

    ``None`` is returned when the application amount cannot be determined.  A
    missing individual OCR amount is ignored rather than guessing from the
    current remaining application amount.
    """
    totals = _calculate_receipt_amounts(prepared_receipt, processed_receipt)
    if totals is None:
        return None

    return (
        f"本次报销申请总金额{_format_summary_amount(totals.apply_amount)}元|"
        f"提交发票总金额{_format_summary_amount(totals.submitted_total)}元|"
        f"发票有效可报销金额{_format_summary_amount(totals.valid_invoice_total)}元|"
        f"发票待补充金额{_format_summary_amount(totals.shortage)}元"
    )


def build_ai_audit_advice(
    prepared_receipt: Mapping[str, Any],
    processed_receipt: Mapping[str, Any],
) -> str | None:
    """Build the deterministic receipt-level ``aiAuditAdvice`` text.

    The advice is intentionally generated locally rather than through an LLM.
    An invoice is listed as a problem invoice only when it has a blocking
    ``reject``/``failed`` rule.  E31 and E34 remain exempt: E31 is the
    receipt-level amount rule and E34 contributes its post-deduction
    ``invoice_finalAmount`` as a valid amount.
    """
    totals = _calculate_receipt_amounts(prepared_receipt, processed_receipt)
    if totals is None:
        return None

    problem_invoice_numbers: list[str] = []
    for preparation, invoice_result in _invoice_pairs(prepared_receipt, processed_receipt):
        if not _is_problem_invoice(invoice_result):
            continue
        problem_invoice_numbers.append(
            _resolve_invoice_number(preparation, invoice_result)
        )

    if problem_invoice_numbers:
        return (
            f"本次报销捕捉{len(problem_invoice_numbers)}张问题发票,"
            f"需要删除/重开发票{'、'.join(problem_invoice_numbers)},"
            f"待补充发票金额{_format_summary_amount(totals.shortage)}元,"
            "期待下一次满分"
        )

    return "本次发票全部通过！"


def invoice_contributes_valid_amount(invoice_result: Mapping[str, Any]) -> bool:
    """Return whether an invoice can contribute its final reimbursable amount.

    ``warning`` remains valid.  For a rejected/failed invoice, E31 and E34 are
    the only allowed exceptions: E31 is the receipt-level sufficiency result,
    while E34's ``invoice_finalAmount`` is already the post-deduction amount.
    """
    execution_status = str(invoice_result.get("executionStatus") or "").strip().upper()
    if execution_status and execution_status != "SUCCEEDED":
        return False

    decision_status = str(invoice_result.get("decisionStatus") or "").strip().lower()
    if decision_status not in ("failed", "reject"):
        # Processed orchestrator results always have executionStatus.  For
        # direct writeback callers that omit status metadata, a finalAmount is
        # still authoritative unless a structured blocking rule says otherwise.
        if not execution_status:
            return not _has_blocking_rule(invoice_result)
        return True

    rule_results = _iter_decision_rule_results(_resolve_decision_output(invoice_result))
    if not rule_results:
        return False

    return not any(_is_blocking_rule(rule_result) for rule_result in rule_results)


class _ReceiptAmountTotals:
    __slots__ = ("apply_amount", "submitted_total", "valid_invoice_total", "shortage")

    def __init__(
        self,
        apply_amount: Decimal,
        submitted_total: Decimal,
        valid_invoice_total: Decimal,
    ) -> None:
        self.apply_amount = apply_amount
        self.submitted_total = submitted_total
        self.valid_invoice_total = valid_invoice_total
        self.shortage = max(apply_amount - valid_invoice_total, Decimal("0"))


def _calculate_receipt_amounts(
    prepared_receipt: Mapping[str, Any],
    processed_receipt: Mapping[str, Any],
) -> _ReceiptAmountTotals | None:
    apply_amount = _resolve_apply_amount(prepared_receipt, processed_receipt)
    if apply_amount is None:
        return None

    submitted_total = Decimal("0")
    valid_invoice_total = Decimal("0")
    for preparation, invoice_result in _invoice_pairs(prepared_receipt, processed_receipt):
        prepared_input = _resolve_prepared_input(invoice_result) or _resolve_prepared_input(preparation)
        submitted_amount = _resolve_invoice_ocr_amount(prepared_input)
        if submitted_amount is not None:
            submitted_total += submitted_amount

        final_amount = extract_valid_invoice_final_amount(invoice_result)
        if final_amount is not None:
            valid_invoice_total += final_amount

    return _ReceiptAmountTotals(
        apply_amount=apply_amount,
        submitted_total=submitted_total,
        valid_invoice_total=valid_invoice_total,
    )


def extract_valid_invoice_final_amount(invoice_result: Mapping[str, Any]) -> Decimal | None:
    """Extract the E31/E34-aware post-audit ``invoice_finalAmount`` exactly."""
    if not invoice_contributes_valid_amount(invoice_result):
        return None

    decision_output = _resolve_decision_output(invoice_result)
    candidates: list[Any] = [decision_output.get("invoice_finalAmount")]
    content_valid_result = decision_output.get("invoice_content_valid_result")
    if isinstance(content_valid_result, Mapping):
        candidates.append(content_valid_result.get("invoice_finalAmount"))

    for candidate in candidates:
        amount = _to_decimal(candidate)
        if amount is not None:
            return amount
    return None


def _resolve_apply_amount(
    prepared_receipt: Mapping[str, Any],
    processed_receipt: Mapping[str, Any],
) -> Decimal | None:
    service_data = prepared_receipt.get("serviceData")
    if isinstance(service_data, Mapping):
        audit_info = service_data.get("auditInfo")
        if isinstance(audit_info, Mapping):
            amount = _to_decimal(audit_info.get("applyAmount"))
            if amount is not None:
                return amount

    return _to_decimal(processed_receipt.get("applyAmount"))


def _invoice_pairs(
    prepared_receipt: Mapping[str, Any],
    processed_receipt: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    preparations = [
        item for item in (prepared_receipt.get("invoicePreparations") or [])
        if isinstance(item, Mapping)
    ]
    results = [
        item for item in (processed_receipt.get("invoiceResults") or [])
        if isinstance(item, Mapping)
    ]
    results_by_key = {
        str(item.get("invoiceKey") or ""): item
        for item in results
        if str(item.get("invoiceKey") or "")
    }

    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for index, preparation in enumerate(preparations):
        key = str(preparation.get("invoiceKey") or "")
        result = results_by_key.get(key)
        if result is None and index < len(results):
            result = results[index]
        pairs.append((preparation, result or {}))

    # Keep the helper useful for writeback callers that provide only
    # processed invoice results.
    if not preparations:
        pairs.extend(({}, result) for result in results)
    return pairs


def _resolve_prepared_input(source: Mapping[str, Any]) -> Mapping[str, Any]:
    prepared_input = source.get("preparedInput")
    return prepared_input if isinstance(prepared_input, Mapping) else {}


def _resolve_invoice_ocr_amount(prepared_input: Mapping[str, Any]) -> Decimal | None:
    # 提交发票总金额严格取 OCR 归一化后的 totalAmount。
    # 不再使用 amount / invoiceAmount 等兼容字段，避免统计口径漂移。
    return _to_decimal(prepared_input.get("totalAmount"))


def _resolve_decision_output(invoice_result: Mapping[str, Any]) -> Mapping[str, Any]:
    decision_output = invoice_result.get("decisionOutput")
    if isinstance(decision_output, Mapping):
        return decision_output

    runtime_result = invoice_result.get("runtimeResult")
    if isinstance(runtime_result, Mapping):
        nested = runtime_result.get("decisionOutput")
        if isinstance(nested, Mapping):
            return nested
    return {}


def _iter_decision_rule_results(decision_output: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        value
        for value in decision_output.values()
        if isinstance(value, Mapping)
        and any(
            key in value
            for key in ("distinguish_result", "distinguishResult", "reason_code", "reasonCode", "audit_content", "audit_type")
        )
    ]


def _has_blocking_rule(invoice_result: Mapping[str, Any]) -> bool:
    return any(
        _is_blocking_rule(rule_result)
        for rule_result in _iter_decision_rule_results(_resolve_decision_output(invoice_result))
    )


def _is_problem_invoice(invoice_result: Mapping[str, Any]) -> bool:
    if _has_blocking_rule(invoice_result):
        return True

    # A structured decision status of failed means the invoice did not pass
    # audit even when the graph did not return an individual rule row.
    return str(invoice_result.get("decisionStatus") or "").strip().lower() == "failed"


def _resolve_invoice_number(
    preparation: Mapping[str, Any],
    invoice_result: Mapping[str, Any],
) -> str:
    for source in (invoice_result, preparation):
        prepared_input = _resolve_prepared_input(source)
        value = _first_text_value(prepared_input, "invoiceNo", "chequeNo", "serialNo")
        if value:
            return value

        invoice_file = source.get("invoiceFile")
        if isinstance(invoice_file, Mapping):
            value = _first_text_value(invoice_file, "invoiceNo", "chequeNo", "serialNo")
            if value:
                return value

    for source in (invoice_result, preparation):
        invoice_key = source.get("invoiceKey")
        if invoice_key is not None and str(invoice_key).strip():
            return str(invoice_key).strip()
    return "unknown"


def _first_text_value(source: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return None


def _is_blocking_rule(rule_result: Mapping[str, Any]) -> bool:
    reason_code = rule_result.get("reason_code") or rule_result.get("reasonCode")
    status = str(
        rule_result.get("distinguish_result")
        or rule_result.get("distinguishResult")
        or ""
    ).strip().lower()
    return (
        str(reason_code or "").strip().upper() not in _INVOICE_FINAL_AMOUNT_EXEMPT_RULE_CODES
        and status in {"reject", "failed"}
    )


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # str(float) avoids importing the binary representation into the
        # monetary calculation while retaining the value supplied by callers.
        value = str(value)
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return None
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _format_summary_amount(value: Decimal) -> str:
    """Format exact monetary values with commas and at least two decimals."""
    decimal_places = max(2, max(0, -value.as_tuple().exponent))
    quantum = Decimal(1).scaleb(-decimal_places)
    displayed = value.quantize(quantum)
    return f"{displayed:,.{decimal_places}f}"


__all__ = [
    "build_ai_audit_advice",
    "build_ai_audit_summary",
    "extract_valid_invoice_final_amount",
    "invoice_contributes_valid_amount",
]
