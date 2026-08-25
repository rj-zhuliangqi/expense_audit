"""Receipt-level amount aggregation used by orchestration and writeback.

The graph still runs one invoice at a time.  This module deliberately performs
receipt-level aggregation only after all invoice results are available, so the
summary cannot depend on whichever invoice happened to run last.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any


_FINANCE_SUMMARY_PROFILES = frozenset({"personal_transport", "telecom", "entertainment"})
_FINANCE_RISK_BLOCKING = "blocking"
_FINANCE_RISK_HIGH = "high"
_FINANCE_RISK_MEDIUM_LOW = "medium_low"
_FINANCE_RISK_LEVELS = frozenset(
    {_FINANCE_RISK_BLOCKING, _FINANCE_RISK_HIGH, _FINANCE_RISK_MEDIUM_LOW}
)


def build_ai_audit_summary_finance(
    prepared_receipt: Mapping[str, Any],
    processed_receipt: Mapping[str, Any],
    *,
    audit_risk_catalog: Mapping[str, Mapping[str, Any]] | None = None,
    expense_profile: str | None = None,
    audit_logs: Sequence[Mapping[str, Any]] | None = None,
) -> str | None:
    """Build the receipt-level audit-point summary for finance.

    This is intentionally separate from the employee-facing ``aiAuditAdvice``:
    it reports counts rather than remediation wording.  Each emitted rule
    result is one audit point.  PASS results count as passed; non-PASS results
    are classified by rule code.  E-codes are always blocking, while W-codes
    use the profile's risk catalog.

    ``audit_logs`` may be supplied by the writeback layer after it has applied
    receipt-level filtering (for example E31/W33 are kept only on the last
    invoice).  The orchestrator calls this function before writeback and walks
    the raw invoice results with the same filtering rule.
    """
    normalized_profile = str(expense_profile or "").strip().lower()
    if normalized_profile and normalized_profile not in _FINANCE_SUMMARY_PROFILES:
        return None
    if not normalized_profile and audit_risk_catalog is None:
        return None

    counts = {
        _FINANCE_RISK_HIGH: 0,
        _FINANCE_RISK_MEDIUM_LOW: 0,
        _FINANCE_RISK_BLOCKING: 0,
        "passed": 0,
    }

    rows: Sequence[Mapping[str, Any]] | None = audit_logs
    if rows is None:
        prebuilt_rows = _iter_prebuilt_audit_rows(processed_receipt)
        if prebuilt_rows:
            rows = prebuilt_rows

    if rows is not None:
        for row in rows:
            _count_finance_audit_point(row, counts, audit_risk_catalog)
    else:
        invoice_pairs = _invoice_pairs(prepared_receipt, processed_receipt)
        for index, (_preparation, invoice_result) in enumerate(invoice_pairs):
            decision_output = _resolve_decision_output(invoice_result)
            for rule_result in _iter_decision_rule_results(decision_output):
                reason_code = _resolve_rule_code(rule_result)
                # E31/W33 are receipt-level rules and are written once, on the
                # final invoice.  Counting earlier copies would inflate totals.
                if reason_code in {"E31", "W33"} and index != len(invoice_pairs) - 1:
                    continue
                _count_finance_audit_point(rule_result, counts, audit_risk_catalog)

    return (
        f"本单高风险 {counts[_FINANCE_RISK_HIGH]} 项、"
        f"中低风险 {counts[_FINANCE_RISK_MEDIUM_LOW]} 项，"
        f"阻断 {counts[_FINANCE_RISK_BLOCKING]} 项，"
        f"已通过 {counts['passed']} 项稽核项。"
    )


def _resolve_rule_code(rule_result: Mapping[str, Any]) -> str:
    return str(
        rule_result.get("reason_code")
        or rule_result.get("reasonCode")
        or ""
    ).strip().upper()


def _count_finance_audit_point(
    rule_result: Mapping[str, Any],
    counts: dict[str, int],
    audit_risk_catalog: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    reason_code = _resolve_rule_code(rule_result)
    if not reason_code:
        # A mapping without a reason code is not a countable audit point.
        return
    status = str(
        rule_result.get("distinguish_result")
        or rule_result.get("distinguishResult")
        or ""
    ).strip().lower()
    if status in {"pass", "passed"}:
        counts["passed"] += 1
        return

    risk_level = _resolve_finance_risk_level(reason_code, audit_risk_catalog)
    counts[risk_level] += 1


def _resolve_finance_risk_level(
    reason_code: str,
    audit_risk_catalog: Mapping[str, Mapping[str, Any]] | None,
) -> str:
    # 业务约定：所有 E 稽核点都是阻断，不能被配置误改。
    if reason_code.startswith("E"):
        return _FINANCE_RISK_BLOCKING

    metadata = (audit_risk_catalog or {}).get(reason_code)
    configured = metadata.get("riskLevel") if isinstance(metadata, Mapping) else None
    normalized = str(configured or "").strip().lower().replace("-", "_")
    aliases = {
        "block": _FINANCE_RISK_BLOCKING,
        "blocked": _FINANCE_RISK_BLOCKING,
        "high_risk": _FINANCE_RISK_HIGH,
        "low": _FINANCE_RISK_MEDIUM_LOW,
        "medium": _FINANCE_RISK_MEDIUM_LOW,
        "medium_risk": _FINANCE_RISK_MEDIUM_LOW,
        "low_risk": _FINANCE_RISK_MEDIUM_LOW,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in _FINANCE_RISK_LEVELS:
        return normalized

    # 未配置的新 W 规则采取保守的高风险口径，保证不会被漏计为已通过。
    if reason_code.startswith("W"):
        return _FINANCE_RISK_HIGH
    # sys-* 等非 E/W 结果不能安全地视为弱控，按阻断处理。
    return _FINANCE_RISK_BLOCKING


# E31 is the receipt-level amount sufficiency rule.  E34 and E36 can return an
# effective amount after prohibited content is deducted, so a rejection on one
# of those content rules does not discard the valid amount they explicitly
# returned.  Model failures have no reliable finalAmount and remain blocking.
_INVOICE_FINAL_AMOUNT_EXEMPT_RULE_CODES = frozenset({"E31", "E34", "E36"})


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
    """Build deterministic receipt-level advice without masking audit issues.

    ``aiAuditAdvice`` must never claim that everything passed while any rule is
    ``REJECT``, ``FAILED`` or ``WARNING``.  Model failures are called out
    explicitly so the caller can retry or contact an administrator.
    """
    totals = _calculate_receipt_amounts(prepared_receipt, processed_receipt)

    problem_invoice_numbers: list[str] = []
    for preparation, invoice_result in _invoice_pairs(prepared_receipt, processed_receipt):
        if not _is_problem_invoice(invoice_result):
            continue
        problem_invoice_numbers.append(
            _resolve_invoice_number(preparation, invoice_result)
        )

    status_flags = _audit_status_flags(processed_receipt)
    status_notice = _build_status_notice(status_flags)

    # 即使申请金额缺失，也不能因为无法计算金额而返回空建议或“全部通过”。
    # 只要图返回了 reject/warning/failed，汇总必须明确提示审核结果。
    if totals is None:
        if status_notice:
            return f"本次审核{status_notice}。"
        return None

    if problem_invoice_numbers:
        advice = (
            f"本次报销捕捉{len(problem_invoice_numbers)}张问题发票,"
            f"需要删除/重开发票{'、'.join(problem_invoice_numbers)},"
            f"待补充发票金额{_format_summary_amount(totals.shortage)}元,"
            "期待下一次满分"
        )
        if status_notice:
            advice += f"；{status_notice}"
        return advice

    if status_notice:
        return (
            f"本次审核{status_notice}；"
            f"待补充发票金额{_format_summary_amount(totals.shortage)}元"
        )

    return "本次发票全部通过！"


def _build_status_notice(flags: Mapping[str, bool]) -> str:
    """Return an explicit status notice for the receipt-level advice.

    Keep the status labels in the summary so a warning/reject cannot be hidden
    behind a generic “problem invoice” sentence.  When a model error is mapped
    to a rule-level REJECT/WARNING, both the status and the operational action
    are retained in the final advice.
    """
    notices: list[str] = []
    if flags.get("external_error"):
        notices.append("存在业务费用明细接口异常，请稍后重试或联系管理员处理")
    if flags.get("model_error"):
        notices.append("存在模型服务异常，请稍后重试或联系管理员处理")
    if flags.get("reject"):
        notices.append("存在REJECT稽核项，请根据稽核明细处理")
    if flags.get("warning"):
        notices.append("存在WARNING稽核项，请根据稽核明细进行人工复核")
    if flags.get("failed"):
        notices.append("存在FAILED稽核项，当前结果无法确认，请稍后重试或联系管理员处理")
    return "；".join(notices)


def invoice_contributes_valid_amount(invoice_result: Mapping[str, Any]) -> bool:
    """Return whether an invoice may contribute a valid reimbursable amount.

    E31 is receipt-level and therefore does not invalidate an otherwise valid
    invoice.  E34/E36 are allowed only when the graph supplied their
    post-deduction ``invoice_finalAmount``.  Every other rejected/failed rule,
    including a model-service failure, blocks the invoice amount.
    """
    execution_status = str(invoice_result.get("executionStatus") or "").strip().upper()
    if execution_status and execution_status != "SUCCEEDED":
        return False

    decision_output = _resolve_decision_output(invoice_result)
    final_amount = _extract_decision_final_amount(decision_output)
    rule_results = _iter_decision_rule_results(decision_output)
    # E36 是 LLM 内容审核规则。新图要求 LLM 必须返回 finalAmount；如果
    # 结果缺少该字段，不能回退 OCR 金额，否则模型格式错误会被误当成有效发票。
    has_e36_result = any(
        str(rule_result.get("reason_code") or rule_result.get("reasonCode") or "")
        .strip()
        .upper()
        == "E36"
        for rule_result in rule_results
    )
    if has_e36_result and final_amount is None:
        return False
    if any(
        _is_blocking_rule(rule_result, final_amount=final_amount)
        for rule_result in rule_results
    ):
        return False

    decision_status = str(invoice_result.get("decisionStatus") or "").strip().lower()
    if decision_status in ("failed", "reject") and not rule_results:
        return False

    # No execution/decision metadata is used by a few direct writeback callers;
    # in that case the absence of a blocking structured rule is sufficient.
    return True


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

        final_amount = extract_valid_invoice_final_amount(
            invoice_result,
            prepared_input=prepared_input,
        )
        if final_amount is not None:
            valid_invoice_total += final_amount

    return _ReceiptAmountTotals(
        apply_amount=apply_amount,
        submitted_total=submitted_total,
        valid_invoice_total=valid_invoice_total,
    )


def extract_valid_invoice_final_amount(
    invoice_result: Mapping[str, Any],
    prepared_input: Mapping[str, Any] | None = None,
) -> Decimal | None:
    """Extract graph ``invoice_finalAmount`` with a safe OCR fallback.

    The LLM output is authoritative when present.  A successful invoice with no
    amount (for example an older graph result) falls back to the prepared OCR
    ``totalAmount`` only when no blocking rule is present.  Model failures and
    rejected invoices never receive that fallback.
    """
    decision_output = _resolve_decision_output(invoice_result)
    final_amount = _extract_decision_final_amount(decision_output)
    if not invoice_contributes_valid_amount(invoice_result):
        return None
    if final_amount is not None:
        return final_amount

    # E36 的有效金额必须来自 LLM。即使规则状态是 PASS，也不能在新旧图
    # 结果混用时用 OCR 金额兜底。
    if any(
        str(rule_result.get("reason_code") or rule_result.get("reasonCode") or "")
        .strip()
        .upper()
        == "E36"
        for rule_result in _iter_decision_rule_results(decision_output)
    ):
        return None

    source = prepared_input or _resolve_prepared_input(invoice_result)
    return _resolve_invoice_ocr_amount(source)


def _extract_decision_final_amount(decision_output: Mapping[str, Any]) -> Decimal | None:
    candidates: list[Any] = [decision_output.get("invoice_finalAmount")]
    content_valid_result = decision_output.get("invoice_content_valid_result")
    if isinstance(content_valid_result, Mapping):
        candidates.append(content_valid_result.get("invoice_finalAmount"))

    for candidate in candidates:
        amount = _to_decimal(candidate)
        if amount is not None and amount.is_finite() and amount >= 0:
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
    decision_output = _resolve_decision_output(invoice_result)
    final_amount = _extract_decision_final_amount(decision_output)
    return any(
        _is_blocking_rule(rule_result, final_amount=final_amount)
        for rule_result in _iter_decision_rule_results(decision_output)
    )


def _is_problem_invoice(invoice_result: Mapping[str, Any]) -> bool:
    if _has_blocking_rule(invoice_result):
        return True

    # E31 is a receipt-level issue and E34 may have a valid post-deduction
    # amount; neither should be listed as a bad invoice here.  E36 remains a
    # visible problem even when its partial effective amount is usable for E31.
    decision_output = _resolve_decision_output(invoice_result)
    final_amount = _extract_decision_final_amount(decision_output)
    for rule_result in _iter_decision_rule_results(decision_output):
        status = str(
            rule_result.get("distinguish_result")
            or rule_result.get("distinguishResult")
            or ""
        ).strip().lower()
        if status not in {"reject", "failed"}:
            continue
        reason_code = str(
            rule_result.get("reason_code") or rule_result.get("reasonCode") or ""
        ).strip().upper()
        if reason_code == "E31":
            continue
        if reason_code == "E34" and final_amount is not None:
            continue
        return True

    # A structured decision status of failed means the invoice did not pass
    # audit even when the graph did not return an individual rule row.
    decision_status = str(invoice_result.get("decisionStatus") or "").strip().lower()
    return decision_status == "failed" and not _iter_decision_rule_results(decision_output)


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


def _is_blocking_rule(
    rule_result: Mapping[str, Any],
    *,
    final_amount: Decimal | None = None,
) -> bool:
    reason_code = rule_result.get("reason_code") or rule_result.get("reasonCode")
    normalized_code = str(reason_code or "").strip().upper()
    status = str(
        rule_result.get("distinguish_result")
        or rule_result.get("distinguishResult")
        or ""
    ).strip().lower()
    if status not in {"reject", "failed"}:
        return False
    if normalized_code == "E31":
        return False
    if normalized_code in _INVOICE_FINAL_AMOUNT_EXEMPT_RULE_CODES and final_amount is not None:
        # A parsed E36 result may legitimately return a post-deduction amount,
        # but an E36 model-service failure must never be treated as a valid
        # amount merely because a stale/partial payload also contains a number.
        if _is_model_failure_rule(normalized_code, status, rule_result):
            return True
        return False
    return True


_MODEL_ERROR_TOKENS = (
    "模型服务",
    "模型异常",
    "模型失败",
    "模型调用",
    "llm",
    "model service",
    "model failure",
    "model call",
)


def _is_model_error_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text) and any(token in text for token in _MODEL_ERROR_TOKENS)


def _mapping_contains_model_error(value: Any) -> bool:
    """Detect model failures even when the graph kept them outside rule rows."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = str(key or "").replace("_", "").lower()
            if normalized_key in {"llmstatus", "llmstate"}:
                if str(nested or "").strip().lower() not in {"success", "succeeded", "ok"}:
                    return True
            elif normalized_key in {"errormessage", "error"} and _is_model_error_text(nested):
                return True
            if _mapping_contains_model_error(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_mapping_contains_model_error(item) for item in value)
    return False


def _is_model_failure_rule(
    reason_code: str,
    status: str,
    rule_result: Mapping[str, Any],
) -> bool:
    # These rules depend on an external LLM.  E32/W32 are the telecom
    # billing-period/phone checks; E17/E34/E36 are content/amount checks;
    # W31 is a weak fraud warning.
    if reason_code not in {"E17", "E32", "W32", "E34", "E36", "W31"}:
        return False
    rule_text = " ".join(
        str(rule_result.get(key) or "")
        for key in (
            "message",
            "problem_category",
            "problemCategory",
            "employeeSuggestionTips",
            "suggestion",
        )
    )
    return status == "failed" or _is_model_error_text(rule_text)


def _audit_status_flags(processed_receipt: Mapping[str, Any]) -> dict[str, bool]:
    flags = {
        "reject": False,
        "warning": False,
        "failed": False,
        "model_error": False,
        "external_error": False,
    }

    # W33 依赖业务费用明细接口。接口异常时即使旧图没有返回 W33 规则，
    # 汇总也必须保留 WARNING，不能落到“本次发票全部通过”。
    service_data = processed_receipt.get("serviceData")
    if isinstance(service_data, Mapping):
        entertainment_data = service_data.get("entertainment_data")
        if isinstance(entertainment_data, Mapping):
            lookup_status = str(
                entertainment_data.get("giftDetailLookupStatus") or ""
            ).strip().lower()
            if lookup_status == "error":
                flags["external_error"] = True
                flags["warning"] = True

    # E31 是核销单级规则，不一定出现在 invoiceResults 的 decisionOutput 中。
    # 应用层已经根据有效发票金额写入 isAmountSufficient，汇总必须同步读取，
    # 否则 E31=REJECT 时会错误落到“本次发票全部通过！”。
    if "isGiftCountReasonable" in processed_receipt and processed_receipt.get(
        "isGiftCountReasonable"
    ) is False:
        # W33 是弱控，数量不足只能形成 WARNING，不能升级为 REJECT。
        flags["warning"] = True
    if "isAmountSufficient" in processed_receipt:
        amount_status = processed_receipt.get("isAmountSufficient")
        if amount_status is False or amount_status is None:
            # 金额不足或无法确认都只能形成 REJECT，不能暴露为 FAILED，也不能
            # 因为金额未知而默认通过。回写层会把 E31 写成同样的 REJECT。
            flags["reject"] = True

    for invoice_result in (
        item for item in (processed_receipt.get("invoiceResults") or [])
        if isinstance(item, Mapping)
    ):
        decision_output = _resolve_decision_output(invoice_result)
        rule_results = _iter_decision_rule_results(decision_output)
        invoice_has_model_failure = (
            _mapping_contains_model_error(decision_output)
            or _mapping_contains_model_error(invoice_result.get("runtimeResult"))
            or _is_model_error_text(invoice_result.get("errorMessage"))
            or _is_model_error_text(invoice_result.get("error_message"))
        )
        if invoice_has_model_failure:
            flags["model_error"] = True

        has_model_failure_rule = False
        has_generic_failed_rule = False
        hard_reject_rule = False
        weak_w33_rule = False
        for rule_result in rule_results:
            status = str(
                rule_result.get("distinguish_result")
                or rule_result.get("distinguishResult")
                or ""
            ).strip().lower()
            reason_code = str(
                rule_result.get("reason_code") or rule_result.get("reasonCode") or ""
            ).strip().upper()
            rule_text = " ".join(
                str(rule_result.get(key) or "")
                for key in (
                    "message",
                    "problem_category",
                    "problemCategory",
                    "employeeSuggestionTips",
                    "suggestion",
                )
            )

            # E31 是硬控且禁止 FAILED；W33 是弱控，只能 PASS/WARNING。
            # 兼容历史运行结果时在汇总层再次归一化，避免旧状态污染最终建议。
            if reason_code == "E31" and status == "failed":
                flags["reject"] = True
                if _is_model_error_text(rule_text):
                    flags["model_error"] = True
                hard_reject_rule = True
                continue
            if reason_code == "W33" and status in {"reject", "failed"}:
                flags["warning"] = True
                weak_w33_rule = True
                if _is_model_error_text(rule_text):
                    flags["model_error"] = True
                continue

            model_failure_rule = _is_model_failure_rule(reason_code, status, rule_result)
            if model_failure_rule:
                has_model_failure_rule = True
                # E36 是硬控，模型无法确认时按 REJECT；W31 是弱控，按 WARNING。
                if reason_code == "W31":
                    flags["warning"] = True
                else:
                    flags["reject"] = True
                    hard_reject_rule = True
                flags["model_error"] = True
                continue

            if status == "reject":
                flags["reject"] = True
                if reason_code != "W33":
                    hard_reject_rule = True
            elif status == "warning":
                flags["warning"] = True
            elif status == "failed":
                has_generic_failed_rule = True
                flags["failed"] = True

        execution_status = str(invoice_result.get("executionStatus") or "").strip().lower()
        decision_status = str(invoice_result.get("decisionStatus") or "").strip().lower()
        if execution_status == "failed":
            if invoice_has_model_failure:
                flags["reject"] = True
            else:
                flags["failed"] = True
        if decision_status == "reject":
            # 历史 W33 可能把弱控整体状态写成 reject，但回写协议中 W33
            # 不能产生 REJECT；只有存在其它硬控拒绝时才保留 reject。
            if weak_w33_rule and not hard_reject_rule:
                flags["warning"] = True
            else:
                flags["reject"] = True
        elif decision_status == "warning":
            flags["warning"] = True
        elif decision_status == "failed" and not has_model_failure_rule and not invoice_has_model_failure:
            # 只有真正的非模型执行失败才保留 FAILED；模型失败已经映射成
            # E36=REJECT 或 W31=WARNING，避免汇总再次泄露 FAILED。
            flags["failed"] = True
        elif decision_status == "failed" and invoice_has_model_failure and not has_generic_failed_rule:
            flags["reject"] = True

    # 兼容旧的回写/重放数据：这类数据可能只有已经展开的 auditLogs，
    # 没有 orchestrator 内部的 invoiceResults。不能因为缺少内部结构，
    # 就把已有的 REJECT/WARNING 覆盖成“本次发票全部通过！”。
    for audit_row in _iter_prebuilt_audit_rows(processed_receipt):
        status = str(
            audit_row.get("distinguishResult")
            or audit_row.get("distinguish_result")
            or ""
        ).strip().lower()
        reason_code = str(
            audit_row.get("reasonCode")
            or audit_row.get("reason_code")
            or ""
        ).strip().upper()
        rule_text = " ".join(
            str(audit_row.get(key) or "")
            for key in (
                "message",
                "specificProblemDes",
                "problemTags",
                "problem_category",
                "employeeSuggestionTips",
                "suggestionTags",
            )
        )
        if _is_model_error_text(rule_text):
            flags["model_error"] = True

        if reason_code == "W33" and status in {"reject", "failed"}:
            flags["warning"] = True
            continue
        if reason_code in {"E17", "E32", "W32", "E34", "E36", "W31"} and _is_model_failure_rule(
            reason_code, status, audit_row
        ):
            if reason_code == "W31":
                flags["warning"] = True
            else:
                flags["reject"] = True
            flags["model_error"] = True
            continue
        if reason_code == "E31" and status == "failed":
            flags["reject"] = True
        elif status == "reject":
            flags["reject"] = True
        elif status == "warning":
            flags["warning"] = True
        elif status == "failed":
            flags["failed"] = True

    if _mapping_contains_model_error(processed_receipt):
        flags["model_error"] = True
        # 没有结构化规则时也不能默认通过；按硬控失败处理。
        if not flags["reject"] and not flags["warning"] and not flags["failed"]:
            flags["reject"] = True
    return flags


def _iter_prebuilt_audit_rows(processed_receipt: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return already-expanded audit rows from legacy receipt payloads."""
    rows: list[Mapping[str, Any]] = []
    for key in ("auditLogs", "audit_logs"):
        value = processed_receipt.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    return rows


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
    "build_ai_audit_summary_finance",
    "extract_valid_invoice_final_amount",
    "invoice_contributes_valid_amount",
]
