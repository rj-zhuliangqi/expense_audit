import json

from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from .receipt_summary import (
    build_ai_audit_advice,
    build_ai_audit_summary,
    build_ai_audit_summary_finance,
    extract_valid_invoice_final_amount,
)


ComplianceRule = Callable[[str, Mapping[str, Any]], bool]
AuditTravelsBuilder = Callable[[list[tuple[dict[str, Any], dict[str, Any]]], Mapping[str, Any]], list[dict[str, Any]]]
FormBuilder = Callable[[list[tuple[dict[str, Any], dict[str, Any]]], Mapping[str, Any]], list[dict[str, Any]]]
AuditRuleCatalog = Mapping[str, Mapping[str, Any]]
AuditRiskCatalog = Mapping[str, Mapping[str, Any]]

# goodsName 仅供内部审核规则和明细合规判断使用，不再作为回写结果字段返回。
_EXCLUDED_TRUTHCHECK_FIELD_NAMES = frozenset({"goodsName"})


# E31（表单发票金额不足）对应 docs/更新通讯费.csv 的最新回写内容。
# 制度列 CSV 为 "/"（空），故 E31 policiesIndex 恒为空。
E31_PROBLEM_CATEGORY = "金额不足"
E31_OPTIMIZATION_ACTION_CATEGORY = "【补充发票】【调减金额】"
# 个人交通费 E31 使用交通费流程图/CSV 的专属文案。回写层不能把通讯费
# 的 E31 固定文案覆盖到个人交通费结果上。
PERSONAL_TRANSPORT_E31_SUGGESTION = (
    "请补充上传金额足够的有效交通费发票；若下方已有票据被标记异常，请先按提示修正。"
    "也可将填报金额调减至有效发票金额以内。"
)
PERSONAL_TRANSPORT_E31_PROBLEM_CATEGORY = "金额不足"
PERSONAL_TRANSPORT_E31_OPTIMIZATION_ACTION_CATEGORY = "【补充发票】【调减金额】"
E31_SUGGESTION = (
    "请补充上传金额足够的有效发票。若下方已有发票被标记为异常，请先按提示修正这些发票；"
    "修正通过后，系统会重新计算可用发票金额。"
)


def _is_pass_like_advice(value: str) -> bool:
    """Return whether an existing summary is an unconditional pass message.

    Older callers may still send one of the historical pass-only phrases.  If
    the current receipt has a REJECT/WARNING result, keeping such a phrase
    would hide the actual audit outcome, so it must be replaced by the
    deterministic advice generated from the normalized statuses.
    """
    normalized = "".join(str(value or "").strip().lower().split())
    if not normalized:
        return False
    return any(
        phrase in normalized
        for phrase in (
            "本次发票全部通过",
            "全部通过",
            "审核通过",
            "建议通过",
            "无需修改",
            "无问题",
        )
    )


def _default_compliance(goods_name: str, item: Mapping[str, Any]) -> bool:
    return True


def assemble_result_audit_info(
    prepared_receipt: Mapping[str, Any],
    processed_receipt: Mapping[str, Any],
    *,
    compliance_rule: ComplianceRule = _default_compliance,
    audit_travels_builder: AuditTravelsBuilder | None = None,
    form_invoice_tax_views_builder: FormBuilder | None = None,
    audit_rule_catalog: AuditRuleCatalog | None = None,
    audit_risk_catalog: AuditRiskCatalog | None = None,
    expense_profile: str | None = None,
) -> dict[str, Any]:
    receipt_code = str(processed_receipt.get("receiptCode") or prepared_receipt.get("receiptCode") or "")
    expense_profile = expense_profile or _resolve_expense_profile_name(
        processed_receipt, prepared_receipt
    )
    prepared_service_data = prepared_receipt.get("serviceData")
    processed_service_data = processed_receipt.get("serviceData")
    service_data = dict(
        prepared_service_data
        if isinstance(prepared_service_data, Mapping) and prepared_service_data
        else processed_service_data
        if isinstance(processed_service_data, Mapping)
        else {}
    )
    # Prepared data is authoritative, but retain fields added by a newer
    # processed payload when an older caller omitted them (notably isEor).
    processed_audit_info = (
        processed_service_data.get("auditInfo")
        if isinstance(processed_service_data, Mapping)
        else None
    )
    audit_info = dict(processed_audit_info) if isinstance(processed_audit_info, Mapping) else {}
    prepared_audit_info = service_data.get("auditInfo")
    if isinstance(prepared_audit_info, Mapping):
        audit_info.update(prepared_audit_info)
    service_data["auditInfo"] = audit_info
    instance_code = _get_string_value(audit_info, "instanceCode") or receipt_code
    is_eor = _is_eor_enabled(audit_info, expense_profile)
    normalized_is_eor = _normalize_is_eor_value(audit_info.get("isEor"))

    invoice_pairs = _pair_invoices(prepared_receipt, processed_receipt)
    is_amount_sufficient = processed_receipt.get("isAmountSufficient")
    amount_status_available = "isAmountSufficient" in processed_receipt
    is_gift_count_reasonable = processed_receipt.get("isGiftCountReasonable")
    gift_reception_count = _coerce_receipt_number(
        processed_receipt.get("giftReceptionCount")
    )
    total_goods_count = _coerce_receipt_number(
        processed_receipt.get("totalGoodsCount")
    )
    gift_lookup_error = _resolve_gift_detail_lookup_error(service_data)

    # Receipt-level amount context used to build the final E31 message with real
    # totals (有效发票合计金额 / 报销金额 / 缺少金额). Sourced from the orchestrator's
    # receipt result; falls back to summing per-invoice finalAmounts when absent.
    apply_amount = _resolve_receipt_amount(processed_receipt, "applyAmount")
    valid_invoice_total = _resolve_receipt_amount(processed_receipt, "validInvoiceTotal")
    if valid_invoice_total is None:
        remaining = _resolve_receipt_amount(processed_receipt, "remainingApplyAmount")
        if apply_amount is not None and remaining is not None:
            valid_invoice_total = apply_amount - remaining
        else:
            valid_invoice_total = _sum_invoice_final_amounts(invoice_pairs)

    result = {
        "instanceCode": instance_code,
        "auditLogs": _build_audit_logs(
            instance_code,
            audit_info,
            invoice_pairs,
            is_amount_sufficient=is_amount_sufficient,
            is_eor=is_eor,
            amount_status_available=amount_status_available,
            apply_amount=apply_amount,
            valid_invoice_total=valid_invoice_total,
            is_gift_count_reasonable=is_gift_count_reasonable,
            gift_reception_count=gift_reception_count,
            total_goods_count=total_goods_count,
            gift_lookup_error=gift_lookup_error,
            audit_rule_catalog=audit_rule_catalog,
            expense_profile=expense_profile,
        ),
        "auditInvoiceInfos": _build_audit_invoice_infos(
            instance_code,
            audit_info,
            invoice_pairs,
            is_amount_sufficient=is_amount_sufficient,
            is_eor=is_eor,
            amount_status_available=amount_status_available,
            apply_amount=apply_amount,
            valid_invoice_total=valid_invoice_total,
            is_gift_count_reasonable=is_gift_count_reasonable,
            gift_reception_count=gift_reception_count,
            total_goods_count=total_goods_count,
            gift_lookup_error=gift_lookup_error,
            expense_profile=expense_profile,
        ),
        "auditInvoiceFiles": _build_audit_invoice_files(service_data),
        "auditRelationFiles": _build_audit_relation_files(invoice_pairs),
        "auditInvoiceInfoContents": _build_audit_invoice_info_contents(
            instance_code, invoice_pairs, compliance_rule
        ),
        "auditTravels": audit_travels_builder(invoice_pairs, service_data) if audit_travels_builder else [],
        "formInvoiceTaxViews": (
            form_invoice_tax_views_builder(invoice_pairs, service_data) if form_invoice_tax_views_builder else []
        ),
        "auditTruthCheckLogs": _build_audit_truthcheck_logs(instance_code, invoice_pairs),
        "auditTruthCheckResultBills": _build_audit_truthcheck_result_bills(instance_code, invoice_pairs),
        "auditTruthCheckResultItems": _build_audit_truthcheck_result_items(instance_code, invoice_pairs),
        "auditTruthCheckResultItemCols": _build_audit_truthcheck_result_item_cols(instance_code, invoice_pairs),
    }
    # IsEor 是核销单原始业务字段，不是模型输出。E31 的图内判断依赖它，
    # 回写接口也会同步更新 form_masterinfo.IsEor，因此只允许以数据库兼容的
    # 单字符值回写，禁止把 Python bool 或 ``"true"/"false"`` 直接传给后端。
    if normalized_is_eor is not None:
        result["isEor"] = normalized_is_eor
    # 核销单级金额汇总：优先使用编排层已计算的值；兼容直接调用
    # assemble_result_audit_info 的场景时，在回写前按同一套 Decimal 规则补算。
    ai_audit_summary = processed_receipt.get("aiAuditSummary")
    if not isinstance(ai_audit_summary, str) or not ai_audit_summary.strip():
        ai_audit_summary = build_ai_audit_summary(prepared_receipt, processed_receipt)
    if isinstance(ai_audit_summary, str) and ai_audit_summary.strip():
        result["aiAuditSummary"] = ai_audit_summary.strip()

    # 给财务看的整体稽核建议必须基于回写层最终生成的 auditLogs 统计。
    # 这里不能直接沿用编排层的中间值，因为回写层会对 E31/W33 等核销单级
    # 结果做过滤和归一化。
    ai_audit_summary_finance = build_ai_audit_summary_finance(
        prepared_receipt,
        processed_receipt,
        audit_logs=result["auditLogs"],
        audit_risk_catalog=audit_risk_catalog,
        expense_profile=expense_profile,
    )
    if ai_audit_summary_finance:
        result["aiAuditSummaryFinance"] = ai_audit_summary_finance

    # 核销单级整体建议必须与最终稽核状态一致。历史结果或外部调用方可能
    # 携带了“本次发票全部通过！”的旧文案，但当前回写阶段已经把 E31/W33
    # 等核销单级规则重算为 reject/warning；此时必须用确定性建议替换旧文案。
    generated_advice = build_ai_audit_advice(
        prepared_receipt,
        processed_receipt,
        expense_profile=expense_profile,
    )
    overall_advice = processed_receipt.get("aiAuditAdvice")
    if isinstance(overall_advice, str) and overall_advice.strip():
        normalized_advice = overall_advice.strip()
        if generated_advice and _is_pass_like_advice(normalized_advice):
            normalized_advice = generated_advice
        result["aiAuditAdvice"] = normalized_advice
    elif generated_advice:
        result["aiAuditAdvice"] = generated_advice
    return result


def _pair_invoices(
    prepared_receipt: Mapping[str, Any],
    processed_receipt: Mapping[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    invoice_preparations = list(prepared_receipt.get("invoicePreparations") or [])
    invoice_results = list(processed_receipt.get("invoiceResults") or [])
    results_by_key = {
        str(item.get("invoiceKey") or ""): dict(item)
        for item in invoice_results
        if isinstance(item, Mapping)
    }

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, preparation in enumerate(invoice_preparations):
        if not isinstance(preparation, Mapping):
            continue
        preparation_dict = dict(preparation)
        invoice_key = str(preparation_dict.get("invoiceKey") or "")
        result = results_by_key.get(invoice_key)
        if result is None and index < len(invoice_results) and isinstance(invoice_results[index], Mapping):
            result = dict(invoice_results[index])
        pairs.append((preparation_dict, result or {}))

    return pairs


def _resolve_receipt_amount(processed_receipt: Mapping[str, Any], key: str) -> float | None:
    value = processed_receipt.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _sum_invoice_final_amounts(
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> float | None:
    total = 0.0
    found = False
    for preparation, result in invoice_pairs:
        prepared_input = _resolve_prepared_input(preparation, result)
        final_amount = extract_valid_invoice_final_amount(
            result,
            prepared_input=prepared_input,
        )
        if final_amount is not None:
            total += float(final_amount)
            found = True
    return total if found else None


def _build_e31_message(
    apply_amount: float | None,
    valid_invoice_total: float | None,
    *,
    expense_profile: str | None = None,
) -> str:
    """Build an E31 message from the profile's latest CSV template.

    正常执行路径会从核销单结果拿到报销金额和可用发票金额。金额缺失时，
    不再回退到旧版「当前核销单有效发票合计金额……」占位文案；如果 E31
    已明确判定金额不足但调用方没有传金额上下文，则保留通用的不可提交提示。
    """
    if apply_amount is None or valid_invoice_total is None:
        return "可用发票金额不足，暂不能提交。"
    shortage = max(apply_amount - valid_invoice_total, 0.0)
    amount_label = "本次交通费报销金额为" if _is_personal_transport_profile(expense_profile) else "本次报销金额为"
    if _is_personal_transport_profile(expense_profile):
        return (
            f"本次交通费报销金额为 {_format_amount(apply_amount)} 元，"
            f"当前有效发票金额为 {_format_amount(valid_invoice_total)} 元，"
            f"待补充 {_format_amount(shortage)} 元。可用发票金额不足，暂不能提交。"
        )
    return (
        f"{amount_label} {_format_amount(apply_amount)} 元，"
        f"当前可用发票金额为 {_format_amount(valid_invoice_total)} 元， "
        f"待补充{_format_amount(shortage)} 元。可用发票金额不足，暂不能提交。"
    )


def _override_e31_rule_result(
    rule_result: Mapping[str, Any],
    *,
    is_amount_sufficient: bool | None,
    is_eor: bool = False,
    apply_amount: float | None = None,
    valid_invoice_total: float | None = None,
    expense_profile: str | None = None,
) -> dict[str, Any]:
    """Apply receipt-level E31 result without losing the graph rule metadata.

    E31 is recalculated by the orchestrator after graph execution.  Keeping this
    override in one helper is important: both auditLogs and auditInvoiceInfos
    previously had separate hard-coded overrides, and the former was still using
    the old CSV message/suggestion.
    """
    overridden = dict(rule_result)
    if is_amount_sufficient is True:
        overridden["distinguish_result"] = "PASS"
        overridden["distinguishResult"] = "PASS"
        overridden["message"] = "发票合计金额充足"
        overridden["policiesIndex"] = ""
        overridden["employeeSuggestionTips"] = ""
        overridden["problem_category"] = ""
        overridden["optimization_action_category"] = ""
    elif is_amount_sufficient is None:
        overridden["distinguish_result"] = "REJECT"
        overridden["distinguishResult"] = "REJECT"
        overridden["message"] = (
            "有效发票金额无法确认，可能是模型服务异常，请稍后重试或联系管理员处理。"
        )
        overridden["policiesIndex"] = ""
        overridden["employeeSuggestionTips"] = (
            "【模型异常】请稍后重试；如问题持续，请联系管理员处理。"
        )
        overridden["problem_category"] = "模型服务异常"
        overridden["optimization_action_category"] = "【稍后重试】【联系管理员】"
    else:
        result_status = "WARNING" if is_eor else "REJECT"
        overridden["distinguish_result"] = result_status
        overridden["distinguishResult"] = result_status
        if _is_personal_transport_profile(expense_profile):
            # E31 是核销单级金额规则，必须使用回写层计算出的整单有效发票金额；
            # 不能保留图内按单张发票计算的 message，也不能把占位符写回结果。
            overridden["message"] = _build_e31_message(
                apply_amount, valid_invoice_total, expense_profile=expense_profile
            )
            overridden["policiesIndex"] = overridden.get("policiesIndex") or ""
            overridden["employeeSuggestionTips"] = (
                _get_string_value(overridden, "employeeSuggestionTips")
                or PERSONAL_TRANSPORT_E31_SUGGESTION
            )
            overridden["problem_category"] = (
                _get_rule_tag(overridden, "problem_category", "problemCategory", "problemTags")
                or PERSONAL_TRANSPORT_E31_PROBLEM_CATEGORY
            )
            overridden["optimization_action_category"] = (
                _get_rule_tag(
                    overridden,
                    "optimization_action_category",
                    "optimizationActionCategory",
                    "suggestionTags",
                )
                or PERSONAL_TRANSPORT_E31_OPTIMIZATION_ACTION_CATEGORY
            )
        else:
            overridden["message"] = _build_e31_message(
                apply_amount, valid_invoice_total, expense_profile=expense_profile
            )
            overridden["policiesIndex"] = ""
            overridden["employeeSuggestionTips"] = E31_SUGGESTION
            overridden["problem_category"] = E31_PROBLEM_CATEGORY
            overridden["optimization_action_category"] = E31_OPTIMIZATION_ACTION_CATEGORY
    return overridden


def _format_amount(value: float) -> str:
    # 保留两位小数，去掉无意义的尾零（10.00 -> 10，10.50 -> 10.5）
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _coerce_receipt_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _format_count(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if value.is_integer() else str(value)


def _override_w33_rule_result(
    rule_result: Mapping[str, Any],
    *,
    is_reasonable: bool,
    gift_reception_count: float | None,
    total_goods_count: float | None,
) -> dict[str, Any]:
    overridden = dict(rule_result)
    if is_reasonable:
        overridden["distinguish_result"] = "PASS"
        overridden["distinguishResult"] = "PASS"
        overridden["message"] = (
            f"全部发票购买商品数量【{_format_count(total_goods_count)}】"
            f"不少于赠送纪念品接待人数【{_format_count(gift_reception_count)}】，"
            "礼品数量与接待人数匹配合理"
        )
        overridden["policiesIndex"] = ""
        overridden["employeeSuggestionTips"] = ""
    else:
        overridden["distinguish_result"] = "WARNING"
        overridden["distinguishResult"] = "WARNING"
        overridden["message"] = (
            f"全部发票购买商品数量【{_format_count(total_goods_count)}】"
            f"少于赠送纪念品接待人数【{_format_count(gift_reception_count)}】，"
            "存在礼品数量与接待人数不匹配的异常风险，"
            "赠送纪念品的商品数量需≥接待人数，确保一人一份的合理配比"
        )
        overridden["policiesIndex"] = "《锐捷网络员工费用管理与报销制度》\n5.2票据使用规范"
        overridden["employeeSuggestionTips"] = (
            "【业务确认】请确认礼品数量与接待人数是否匹配，如为实际业务发生的合理配比，"
            "请在单据备注栏说明具体接待情况及礼品分配逻辑，财务将进行人工复核"
        )
    return overridden


def _override_w33_lookup_error_rule_result(
    rule_result: Mapping[str, Any],
    *,
    error: str,
) -> dict[str, Any]:
    """Keep W33 as WARNING when its business-fee-detail lookup failed."""
    overridden = dict(rule_result)
    message = str(error or "请稍后重试或联系管理员处理。")
    overridden["distinguish_result"] = "WARNING"
    overridden["distinguishResult"] = "WARNING"
    overridden["message"] = overridden.get("message") or (
        "业务招待费业务费用明细接口异常，无法确认【项目类别】及赠送纪念品接待人数，"
        f"W33 稽核未完成：{message}"
    )
    overridden["policiesIndex"] = overridden.get("policiesIndex") or ""
    overridden["employeeSuggestionTips"] = overridden.get("employeeSuggestionTips") or (
        "【接口异常】请稍后重试；如问题持续，请联系管理员处理。"
    )
    overridden["problem_category"] = overridden.get("problem_category") or "业务费用明细接口异常"
    overridden["optimization_action_category"] = (
        overridden.get("optimization_action_category") or "【稍后重试】【联系管理员】"
    )
    return overridden


def _override_w33_lookup_error_decision_output(
    decision_output: Mapping[str, Any],
    *,
    error: str,
) -> dict[str, Any]:
    """Override or synthesize W33 in invoice-info writeback for lookup errors."""
    overridden_output = dict(decision_output)
    found = False
    for key, value in decision_output.items():
        if not isinstance(value, Mapping):
            continue
        reason_code = str(value.get("reason_code") or value.get("reasonCode") or "").upper()
        if reason_code == "W33":
            overridden_output[key] = _override_w33_lookup_error_rule_result(
                value, error=error
            )
            found = True
    if not found:
        overridden_output["gift_count_result"] = _override_w33_lookup_error_rule_result(
            {
                "reason_code": "W33",
                "audit_content": "检查【项目类别】为赠送纪念品中接待人数量与发票中购买商品数量的合理性",
                "audit_type": "staff-behavior",
            },
            error=error,
        )
    return overridden_output


def _resolve_gift_detail_lookup_error(service_data: Mapping[str, Any]) -> str | None:
    entertainment_data = service_data.get("entertainment_data")
    if not isinstance(entertainment_data, Mapping):
        return None
    status = str(entertainment_data.get("giftDetailLookupStatus") or "").strip().lower()
    if status != "error":
        return None
    return str(
        entertainment_data.get("giftDetailLookupError")
        or "业务费用明细接口返回异常。"
    ).strip()


def _override_w33_decision_output(
    decision_output: Mapping[str, Any],
    *,
    is_reasonable: bool,
    gift_reception_count: float | None,
    total_goods_count: float | None,
) -> dict[str, Any]:
    overridden_output = dict(decision_output)
    for key, value in decision_output.items():
        if not isinstance(value, Mapping):
            continue
        reason_code = value.get("reason_code") or value.get("reasonCode")
        if reason_code == "W33":
            overridden_output[key] = _override_w33_rule_result(
                value,
                is_reasonable=is_reasonable,
                gift_reception_count=gift_reception_count,
                total_goods_count=total_goods_count,
            )
    return overridden_output


def _build_audit_logs(
    instance_code: str,
    audit_info: Mapping[str, Any],
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    is_amount_sufficient: bool | None = None,
    is_eor: bool = False,
    amount_status_available: bool = False,
    apply_amount: float | None = None,
    valid_invoice_total: float | None = None,
    is_gift_count_reasonable: bool | None = None,
    gift_reception_count: float | None = None,
    total_goods_count: float | None = None,
    gift_lookup_error: str | None = None,
    audit_rule_catalog: AuditRuleCatalog | None = None,
    expense_profile: str | None = None,
) -> list[dict[str, Any]]:
    audit_logs: list[dict[str, Any]] = []
    for index, (preparation, result) in enumerate(invoice_pairs):
        is_last = (index == len(invoice_pairs) - 1)
        prepared_input = _resolve_prepared_input(preparation, result)
        current_invoice_info = _resolve_first_mapping(prepared_input.get("serviceData", {}).get("currentInvoiceInfo"))
        current_audit_invoice_file = _resolve_current_audit_invoice_file(preparation, prepared_input)
        decision_output = dict(result.get("decisionOutput") or {})
        prepared_instance_code = _get_string_value(prepared_input, "instance_code") or instance_code
        prepared_invoice_file_id = _get_string_value(prepared_input, "invoice_file_id") or _get_string_value(
            current_audit_invoice_file,
            "afiid",
            "aifid",
        )
        prepared_invoice_info_id = _get_string_value(prepared_input, "invoice_info_id") or _get_string_value(
            current_invoice_info,
            "aiiid",
        )
        rule_results = _extract_rule_results(decision_output)
        if rule_results:
            invoice_logs: list[dict[str, Any]] = []
            for rule_result in rule_results:
                reason_code = rule_result.get("reason_code") or rule_result.get("reasonCode")
                if reason_code == "E31":
                    if not is_last:
                        continue
                    # E31 不能以 FAILED 回写。即使调用方没有提供整单金额状态，
                    # 也按“金额无法确认”写成 REJECT，禁止默认通过。
                    rule_result = _override_e31_rule_result(
                        rule_result,
                        is_amount_sufficient=is_amount_sufficient,
                        is_eor=is_eor,
                        apply_amount=apply_amount,
                        valid_invoice_total=valid_invoice_total,
                        expense_profile=expense_profile,
                    )

                if reason_code == "W33":
                    # W33 是核销单级规则，只在最后一张发票回写最终结果。
                    if not is_last:
                        continue
                    if is_gift_count_reasonable is not None:
                        rule_result = _override_w33_rule_result(
                            rule_result,
                            is_reasonable=bool(is_gift_count_reasonable),
                            gift_reception_count=gift_reception_count,
                            total_goods_count=total_goods_count,
                        )
                    elif gift_lookup_error:
                        rule_result = _override_w33_lookup_error_rule_result(
                            rule_result, error=gift_lookup_error
                        )

                # createTime 取图内各稽核点输出的 create_time（来自 context.executionTime），
                # 兼容 create_time / createTime 两种键名；回写层不另行生成时间戳。
                invoice_logs.append(
                    {
                        "instanceCode": _get_string_value(rule_result, "instance_code")
                        or prepared_instance_code
                        or instance_code,
                        "invoiceFileId": _get_string_value(rule_result, "invoice_file_id")
                        or prepared_invoice_file_id
                        or _get_string_value(current_audit_invoice_file, "afiid", "aifid"),
                        "invoiceInfoId": _get_string_value(rule_result, "invoice_info_id")
                        or prepared_invoice_info_id
                        or current_invoice_info.get("aiiid"),
                        "reasonCode": rule_result.get("reason_code") or rule_result.get("reasonCode"),
                        "auditType": rule_result.get("audit_type") or rule_result.get("auditType"),
                        "auditContent": rule_result.get("audit_content") or rule_result.get("auditContent"),
                        "distinguishContent": rule_result.get("distinguish_content") or rule_result.get("distinguishContent"),
                        "distinguishResult": _normalize_rule_distinguish_result(
                            rule_result.get("distinguish_result") or rule_result.get("distinguishResult"),
                            reason_code=str(
                                rule_result.get("reason_code") or rule_result.get("reasonCode") or ""
                            ),
                        )
                        or result.get("decisionStatus"),
                        "message": rule_result.get("message") or result.get("errorMessage"),
                        "specificProblemDes": rule_result.get("message") or result.get("errorMessage"),
                        "policiesIndex": rule_result.get("policiesIndex"),
                        "employeeSuggestionTips": rule_result.get("employeeSuggestionTips"),
                        # 通讯费规则图新增的标签字段：
                        # problem_category -> problemTags，优化动作分类 -> suggestionTags。
                        "problemTags": _get_rule_tag(rule_result, "problem_category", "problemCategory", "problemTags"),
                        "suggestionTags": _get_rule_tag(
                            rule_result,
                            "optimization_action_category",
                            "optimizationActionCategory",
                            "suggestionTags",
                        ),
                        "createTime": rule_result.get("create_time") or rule_result.get("createTime"),
                    }
                )
            if audit_rule_catalog:
                actual_codes = {str(log.get("reasonCode") or "") for log in invoice_logs}
                for reason_code, metadata in audit_rule_catalog.items():
                    # E31/W33 是核销单级结果，只能由最后一张发票承载；
                    # W33 在上面的运行结果中若已出现，会先被非最后发票过滤，
                    # 因此这里也必须阻止 catalog 补齐逻辑把它重新写回前置发票。
                    if reason_code == "W33" and not is_last:
                        continue
                    if reason_code in actual_codes or reason_code in {"E31", "sys-001", "sys-003", "sys-004"}:
                        continue
                    catalog_rule_result = {
                        "instanceCode": prepared_instance_code or instance_code,
                        "invoiceFileId": prepared_invoice_file_id,
                        "invoiceInfoId": prepared_invoice_info_id,
                        "reasonCode": reason_code,
                        "auditType": metadata.get("auditType", "general-rules"),
                        "auditContent": metadata.get("auditContent", ""),
                        "distinguishContent": metadata.get("distinguishContent", ""),
                        "distinguishResult": "pass",
                        "message": "",
                        "specificProblemDes": "",
                        "policiesIndex": metadata.get("policiesIndex", ""),
                        "employeeSuggestionTips": "",
                        "problemTags": _get_rule_tag(metadata, "problem_category", "problemCategory", "problemTags"),
                        "suggestionTags": _get_rule_tag(
                            metadata,
                            "optimization_action_category",
                            "optimizationActionCategory",
                            "suggestionTags",
                        ),
                        "createTime": None,
                    }
                    if reason_code == "W33":
                        if is_gift_count_reasonable is not None:
                            overridden_w33 = _override_w33_rule_result(
                                {"distinguish_result": "PASS"},
                                is_reasonable=bool(is_gift_count_reasonable),
                                gift_reception_count=gift_reception_count,
                                total_goods_count=total_goods_count,
                            )
                        elif gift_lookup_error:
                            overridden_w33 = _override_w33_lookup_error_rule_result(
                                {"distinguish_result": "PASS"},
                                error=gift_lookup_error,
                            )
                        else:
                            overridden_w33 = None
                        if overridden_w33 is not None:
                            catalog_rule_result.update(
                                {
                                    "distinguishResult": _normalize_rule_distinguish_result(
                                        overridden_w33.get("distinguish_result"),
                                        reason_code="W33",
                                    ),
                                    "message": overridden_w33.get("message", ""),
                                    "specificProblemDes": overridden_w33.get("message", ""),
                                    "policiesIndex": overridden_w33.get("policiesIndex", ""),
                                    "employeeSuggestionTips": overridden_w33.get(
                                        "employeeSuggestionTips", ""
                                    ),
                                    "problemTags": _get_rule_tag(
                                        overridden_w33,
                                        "problem_category",
                                        "problemCategory",
                                        "problemTags",
                                    ),
                                    "suggestionTags": _get_rule_tag(
                                        overridden_w33,
                                        "optimization_action_category",
                                        "optimizationActionCategory",
                                        "suggestionTags",
                                    ),
                                }
                            )
                    invoice_logs.append(catalog_rule_result)
            audit_logs.extend(invoice_logs)
            continue

        audit_logs.append(
            {
                "instanceCode": prepared_instance_code,
                "invoiceFileId": prepared_invoice_file_id,
                "invoiceInfoId": prepared_invoice_info_id,
                "reasonCode": decision_output.get("reasonCode"),
                "auditType": decision_output.get("auditType"),
                "auditContent": decision_output.get("auditContent"),
                "distinguishContent": decision_output.get("distinguishContent"),
                "distinguishResult": result.get("decisionStatus"),
                "message": decision_output.get("message") or result.get("errorMessage"),
                "specificProblemDes": decision_output.get("message") or result.get("errorMessage"),
                "policiesIndex": decision_output.get("policiesIndex"),
                "employeeSuggestionTips": decision_output.get("employeeSuggestionTips"),
                "problemTags": _get_rule_tag(
                    decision_output,
                    "problem_category",
                    "problemCategory",
                    "problemTags",
                ),
                "suggestionTags": _get_rule_tag(
                    decision_output,
                    "optimization_action_category",
                    "optimizationActionCategory",
                    "suggestionTags",
                ),
                "createTime": decision_output.get("create_time") or decision_output.get("createTime"),
            }
        )
    return audit_logs


def _build_audit_relation_files(invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    relation_files: list[dict[str, Any]] = []
    for preparation, result in invoice_pairs:
        prepared_input = _resolve_prepared_input(preparation, result)
        current_audit_invoice_file = _resolve_current_audit_invoice_file(preparation, prepared_input)
        ocr_envelope = _resolve_ocr_envelope(prepared_input)
        upload = dict(ocr_envelope.get("upload") or {})
        status = dict(ocr_envelope.get("status") or {})
        relation_files.append(
            {
                "fileId": _get_string_value(current_audit_invoice_file, "fid"),
                "fileName": _get_string_value(current_audit_invoice_file, "fileName"),
                "manufacturer": "piao-zone",
                "manufacturerFileId": upload.get("fileDownUrl"),
                "manufacturerFileDownloadUrl": upload.get("fileDownUrl"),
                "status": True,
                "createBy": None,
                "createTime": current_audit_invoice_file.get("createTime") or status.get("finishedAt"),
                "updateBy": None,
                "updateTime": status.get("finishedAt"),
            }
        )
    return relation_files


def _build_audit_invoice_files(service_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    invoice_files: list[dict[str, Any]] = []
    for item in service_data.get("auditInvoiceFiles") or []:
        if not isinstance(item, Mapping):
            continue
        normalized_item = dict(item)
        normalized_item["aifid"] = str(uuid4())
        normalized_item["type"] = 1
        invoice_files.append(normalized_item)
    return invoice_files


def _build_audit_truthcheck_logs(
    instance_code: str,
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    truthcheck_logs: list[dict[str, Any]] = []
    for preparation, result in invoice_pairs:
        prepared_input = _resolve_prepared_input(preparation, result)
        ocr_envelope = _resolve_ocr_envelope(prepared_input)
        recognition = dict(ocr_envelope.get("recognition") or {})
        status = dict(ocr_envelope.get("status") or {})
        raw_payload = recognition.get("rawPayload")
        truthcheck_logs.append(
            {
                "atclid": str(uuid4()),
                "miInstanceCode": instance_code,
                "json": _stringify_json_payload(raw_payload),
                "status": status.get("code"),
                "msg": status.get("message"),
                "createTime": status.get("finishedAt"),
            }
        )
    return truthcheck_logs


def _build_audit_invoice_infos(
    instance_code: str,
    audit_info: Mapping[str, Any],
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    is_amount_sufficient: bool | None = None,
    is_eor: bool = False,
    amount_status_available: bool = False,
    apply_amount: float | None = None,
    valid_invoice_total: float | None = None,
    is_gift_count_reasonable: bool | None = None,
    gift_reception_count: float | None = None,
    total_goods_count: float | None = None,
    gift_lookup_error: str | None = None,
    expense_profile: str | None = None,
) -> list[dict[str, Any]]:
    invoice_infos: list[dict[str, Any]] = []
    for index, (preparation, result) in enumerate(invoice_pairs):
        is_last = (index == len(invoice_pairs) - 1)
        prepared_input = _resolve_prepared_input(preparation, result)
        service_data = dict(prepared_input.get("serviceData") or {})
        current_invoice_info = _resolve_first_mapping(service_data.get("currentInvoiceInfo"))
        current_audit_invoice_file = _resolve_current_audit_invoice_file(preparation, prepared_input)
        decision_output = dict(result.get("decisionOutput") or {})

        if is_last and "amount_result" in decision_output:
            overridden_amount_result = _override_e31_rule_result(
                decision_output["amount_result"],
                is_amount_sufficient=is_amount_sufficient,
                is_eor=is_eor,
                apply_amount=apply_amount,
                valid_invoice_total=valid_invoice_total,
                expense_profile=expense_profile,
            )
            decision_output = {**decision_output, "amount_result": overridden_amount_result}

        if is_last and is_gift_count_reasonable is not None:
            decision_output = _override_w33_decision_output(
                decision_output,
                is_reasonable=bool(is_gift_count_reasonable),
                gift_reception_count=gift_reception_count,
                total_goods_count=total_goods_count,
            )
        elif is_last and gift_lookup_error:
            decision_output = _override_w33_lookup_error_decision_output(
                decision_output, error=gift_lookup_error
            )

        ignore_codes = [] if is_last else ["E31", "W33"]
        primary_rule_result = _select_primary_rule_result(decision_output, ignore_reason_codes=ignore_codes)
        invoice_infos.append(
            {
                "aiiid": current_invoice_info.get("aiiid"),
                "miInstanceCode": instance_code,
                "createTime": current_invoice_info.get("createTime") or current_audit_invoice_file.get("createTime"),
                "miApplyUserId": current_invoice_info.get("miApplyUserId") or audit_info.get("verifiUserId"),
                "miApplyUserName": current_invoice_info.get("miApplyUserName") or audit_info.get("verifiUserName"),
                "billTypeCode": None,
                "accountingCode": prepared_input.get("accountingCode"),
                "chequeNo": _get_string_value(prepared_input, "chequeNo", "invoiceNo", "serialNo"),
                "issueDate": prepared_input.get("invoiceDate") or prepared_input.get("billCreateTime"),
                "estimatedTotalAmount": prepared_input.get("totalAmount") or prepared_input.get("amount") or prepared_input.get("invoiceAmount"),
                "payingCorp": prepared_input.get("buyerName") or prepared_input.get("orgName") or audit_info.get("verifiUserCompanyName"),
                "payerBankCode": prepared_input.get("buyerAccount"),
                "payerActName": prepared_input.get("buyerAddressPhone"),
                "payerAct": prepared_input.get("buyerTaxNo"),
                "drawingCorp": prepared_input.get("salerName"),
                "receActName": prepared_input.get("salerTaxNo"),
                "receAct": prepared_input.get("salerAddressPhone"),
                "sealNo": prepared_input.get("companySeal"),
                "receBankCode": prepared_input.get("salerAccount"),
                "taxRate": _resolve_first_item_value(prepared_input.get("items"), "taxRate"),
                "totalTax": prepared_input.get("totalTaxAmount") or prepared_input.get("taxAmount"),
                "summary": prepared_input.get("remark"),
                "orderDate": prepared_input.get("orderDate"),
                "priorEndorsee": prepared_input.get("priorEndorsee"),
                "costClasses": prepared_input.get("costClasses"),
                "electronicBillNo": prepared_input.get("electronicBillNo"),
                "post": prepared_input.get("post"),
                "departure": prepared_input.get("departure"),
                "reasonStatus": 1 if result.get("executionStatus") == "SUCCEEDED" else 0,
                "reasonCode": _resolve_reason_code(primary_rule_result, decision_output),
                "fid": current_audit_invoice_file.get("fid"),
                "parentFid": current_audit_invoice_file.get("fid"),
                "billTypeFullName": None,
                "atcrid": current_invoice_info.get("atcrid"),
                "enable": True,
                "aiid": current_audit_invoice_file.get("aiid"),
            }
        )
    return invoice_infos


def _build_audit_invoice_info_contents(
    instance_code: str,
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    compliance_rule: ComplianceRule = _default_compliance,
) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for preparation, result in invoice_pairs:
        prepared_input = _resolve_prepared_input(preparation, result)
        service_data = dict(prepared_input.get("serviceData") or {})
        current_invoice_info = _resolve_first_mapping(service_data.get("currentInvoiceInfo"))
        for item in prepared_input.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            goods_name = _get_string_value(item, "goodsName") or ""
            contents.append(
                {
                    "aiicid": str(uuid4()),
                    "aiiid": current_invoice_info.get("aiiid"),
                    "miInstanceCode": instance_code,
                    "standard": item.get("specModel"),
                    "unit": item.get("unit"),
                    "quantity": item.get("num"),
                    "unitprice": item.get("unitPrice"),
                    "content": goods_name,
                    "taxRate": item.get("taxRate"),
                    "amount": item.get("detailAmount"),
                    "taxAmount": item.get("taxAmount"),
                    "createTime": _resolve_ocr_envelope(prepared_input).get("status", {}).get("finishedAt"),
                    "atcrId": current_invoice_info.get("atcrid"),
                    "compliance": compliance_rule(goods_name, item),
                }
            )
    return contents


def _collect_truthcheck_raw_payloads(invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for preparation, result in invoice_pairs:
        prepared_input = _resolve_prepared_input(preparation, result)
        ocr_envelope = _resolve_ocr_envelope(prepared_input)
        raw_payload = ocr_envelope.get("recognition", {}).get("rawPayload")
        if isinstance(raw_payload, Mapping):
            payloads.append(dict(raw_payload))
    return payloads


def _build_audit_truthcheck_result_bills(
    instance_code: str,
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for preparation, result in invoice_pairs:
        prepared_input = _resolve_prepared_input(preparation, result)
        current_invoice_info = _resolve_first_mapping(prepared_input.get("serviceData", {}).get("currentInvoiceInfo"))
        current_audit_invoice_file = _resolve_current_audit_invoice_file(preparation, prepared_input)
        create_time = _resolve_truthcheck_create_time(prepared_input, current_audit_invoice_file)
        for invoice_record in _extract_truthcheck_invoice_records(prepared_input):
            for field_mapping in _resolve_truthcheck_field_mappings(prepared_input, "bill"):
                field_name = _get_string_value(field_mapping, "fieldName")
                if (
                    field_name is None
                    or field_name in _EXCLUDED_TRUTHCHECK_FIELD_NAMES
                    or field_name not in invoice_record
                ):
                    continue
                rows.append(
                    {
                        "atcrbid": str(uuid4()),
                        "miInstanceCode": instance_code,
                        "fid": current_audit_invoice_file.get("fid"),
                        "name": field_mapping.get("fieldLable"),
                        "code": field_name,
                        "value": _stringify_truthcheck_value(invoice_record.get(field_name)),
                        "atcrId": current_invoice_info.get("atcrid"),
                        "createTime": create_time,
                        "id": current_audit_invoice_file.get("fid"),
                        "aiid": current_audit_invoice_file.get("aiid"),
                    }
                )

    return rows


def _build_audit_truthcheck_result_items(
    instance_code: str,
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for preparation, result in invoice_pairs:
        prepared_input = _resolve_prepared_input(preparation, result)
        current_invoice_info = _resolve_first_mapping(prepared_input.get("serviceData", {}).get("currentInvoiceInfo"))
        current_audit_invoice_file = _resolve_current_audit_invoice_file(preparation, prepared_input)
        create_time = _resolve_truthcheck_create_time(prepared_input, current_audit_invoice_file)
        for invoice_record in _extract_truthcheck_invoice_records(prepared_input):
            for field_mapping in _resolve_truthcheck_field_mappings(prepared_input, "item"):
                field_name = _get_string_value(field_mapping, "fieldName")
                if (
                    field_name is None
                    or field_name in _EXCLUDED_TRUTHCHECK_FIELD_NAMES
                    or field_name not in invoice_record
                ):
                    continue
                rows.append(
                    {
                        "atcriid": str(uuid4()),
                        "miInstanceCode": instance_code,
                        "fid": current_audit_invoice_file.get("fid"),
                        "name": field_name,
                        "label": field_mapping.get("fieldLable"),
                        "code": None,
                        "value": _stringify_truthcheck_value(invoice_record.get(field_name)),
                        "atcrId": current_invoice_info.get("atcrid"),
                        "createTime": create_time,
                        "id": current_audit_invoice_file.get("fid"),
                        "aiid": current_audit_invoice_file.get("aiid"),
                    }
                )
    return rows


def _build_audit_truthcheck_result_item_cols(
    instance_code: str,
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for preparation, result in invoice_pairs:
        prepared_input = _resolve_prepared_input(preparation, result)
        current_invoice_info = _resolve_first_mapping(prepared_input.get("serviceData", {}).get("currentInvoiceInfo"))
        current_audit_invoice_file = _resolve_current_audit_invoice_file(preparation, prepared_input)
        create_time = _resolve_truthcheck_create_time(prepared_input, current_audit_invoice_file)
        for invoice_record in _extract_truthcheck_invoice_records(prepared_input):
            for field_mapping in _resolve_truthcheck_field_mappings(prepared_input, "item"):
                field_name = _get_string_value(field_mapping, "fieldName")
                if (
                    field_name is None
                    or field_name in _EXCLUDED_TRUTHCHECK_FIELD_NAMES
                    or field_name not in invoice_record
                ):
                    continue
                rows.append(
                    {
                        "atcricid": str(uuid4()),
                        "miInstanceCode": instance_code,
                        "fid": current_audit_invoice_file.get("fid"),
                        "name": field_name,
                        "label": field_mapping.get("fieldLable"),
                        "atcrId": current_invoice_info.get("atcrid"),
                        "createTime": create_time,
                        "id": current_audit_invoice_file.get("fid"),
                        "aiid": current_audit_invoice_file.get("aiid"),
                    }
                )
    return rows


def _resolve_truthcheck_field_mappings(
    prepared_input: Mapping[str, Any],
    belong_table: str,
) -> list[dict[str, Any]]:
    service_data = prepared_input.get("serviceData")
    if not isinstance(service_data, Mapping):
        return []

    truthcheck_field_mappings = service_data.get("truthCheckFieldMappings")
    if not isinstance(truthcheck_field_mappings, Mapping):
        return []

    mappings = truthcheck_field_mappings.get(belong_table)
    if not isinstance(mappings, list):
        return []

    resolved_mappings: list[dict[str, Any]] = []
    for item in mappings:
        if not isinstance(item, Mapping):
            continue
        if not _is_enabled_truthcheck_mapping(item.get("status")):
            continue
        resolved_mappings.append(dict(item))
    return resolved_mappings


def _is_enabled_truthcheck_mapping(status: Any) -> bool:
    if status is None:
        return True

    if isinstance(status, bool):
        return status

    if isinstance(status, (int, float)):
        return status != 0

    if isinstance(status, str):
        normalized = status.strip().lower()
        if not normalized:
            return False
        return normalized not in {"0", "false", "no", "off", "disabled"}

    return bool(status)


def _extract_truthcheck_invoice_records(prepared_input: Mapping[str, Any]) -> list[dict[str, Any]]:
    ocr_envelope = _resolve_ocr_envelope(prepared_input)
    recognition = ocr_envelope.get("recognition")
    if not isinstance(recognition, Mapping):
        return []

    raw_payload = recognition.get("rawPayload")
    if not isinstance(raw_payload, Mapping):
        return []

    data = raw_payload.get("data")
    if isinstance(data, Mapping):
        return [dict(data)]
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, Mapping)]
    return []


def _resolve_truthcheck_create_time(
    prepared_input: Mapping[str, Any],
    current_audit_invoice_file: Mapping[str, Any],
) -> Any:
    ocr_envelope = _resolve_ocr_envelope(prepared_input)
    status = ocr_envelope.get("status")
    if isinstance(status, Mapping) and status.get("finishedAt") is not None:
        return status.get("finishedAt")
    return current_audit_invoice_file.get("createTime")


def _stringify_truthcheck_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _stringify_json_payload(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, str):
        return value

    return json.dumps(value, ensure_ascii=False)


def _resolve_prepared_input(preparation: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    candidate = result.get("preparedInput")
    if isinstance(candidate, Mapping):
        return dict(candidate)
    candidate = preparation.get("preparedInput")
    if isinstance(candidate, Mapping):
        return dict(candidate)
    return {}


def _resolve_current_audit_invoice_file(preparation: Mapping[str, Any], prepared_input: Mapping[str, Any]) -> dict[str, Any]:
    service_data = prepared_input.get("serviceData")
    if isinstance(service_data, Mapping):
        candidate = service_data.get("currentAuditInvoiceFile")
        if isinstance(candidate, Mapping):
            return dict(candidate)

    invoice_file = preparation.get("invoiceFile")
    if isinstance(invoice_file, Mapping):
        audit_invoice_file = invoice_file.get("auditInvoiceFile")
        if isinstance(audit_invoice_file, Mapping):
            return dict(audit_invoice_file)
        return dict(invoice_file)

    return {}


def _resolve_ocr_envelope(prepared_input: Mapping[str, Any]) -> dict[str, Any]:
    service_data = prepared_input.get("serviceData")
    if isinstance(service_data, Mapping):
        candidate = service_data.get("ocrEnvelope")
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return {}


def _resolve_first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                return dict(item)
    return {}


def _resolve_first_item_value(items: Any, key: str) -> Any:
    if isinstance(items, list):
        for item in items:
            if isinstance(item, Mapping) and key in item:
                return item.get(key)
    return None


def _is_personal_transport_profile(expense_profile: str | None) -> bool:
    return (expense_profile or "").strip().lower() in {
        "personal_transport",
        "personal-transport",
        "交通费",
        "个人交通费",
    }


def _is_eor_enabled(audit_info: Mapping[str, Any], expense_profile: str | None) -> bool:
    """Return whether EOR semantics apply to this profile's E31 rule.

    The audit-info endpoint returns ``isEor`` as ``"1"``/``"0"``.  Keep the
    profile guard here as a second line of defense so a future travel graph (or
    a legacy caller that happens to carry the field) cannot accidentally turn
    travel E31 into a warning.
    """
    normalized_profile = (expense_profile or "").strip().lower().replace("-", "_")
    if normalized_profile not in {"telecom", "personal_transport", "entertainment"}:
        return False
    return _normalize_is_eor_value(audit_info.get("isEor")) == "1"


def _normalize_is_eor_value(value: Any) -> str | None:
    """Normalize the source IsEor flag to the one-character API/DB format."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true"}:
        return "1"
    if normalized in {"0", "false"}:
        return "0"
    return None


def _resolve_expense_profile_name(*sources: Mapping[str, Any]) -> str | None:
    for source in sources:
        profile = source.get("resolvedProfile")
        if isinstance(profile, Mapping):
            value = profile.get("name")
        else:
            value = getattr(profile, "name", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _get_rule_tag(rule_result: Mapping[str, Any], *keys: str) -> Any:
    """读取稽核结果标签，兼容通讯费图的 snake_case 字段及历史命名。"""
    for key in keys:
        if key in rule_result:
            return rule_result[key]
    return ""


def _get_string_value(data: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _extract_rule_results(decision_output: Mapping[str, Any]) -> list[dict[str, Any]]:
    rule_results: list[dict[str, Any]] = []
    for value in decision_output.values():
        if isinstance(value, Mapping) and _is_rule_result(value):
            normalized = _normalize_model_failure_rule_result(value)
            reason_code = str(
                normalized.get("reason_code") or normalized.get("reasonCode") or ""
            ).strip().upper()
            status = str(
                normalized.get("distinguish_result")
                or normalized.get("distinguishResult")
                or ""
            ).strip().lower()

            # 回写协议中 E31 不允许暴露 FAILED；金额无法确认统一按 REJECT。
            if reason_code == "E31" and status == "failed":
                normalized["distinguish_result"] = "REJECT"
                normalized["distinguishResult"] = "REJECT"
                normalized["message"] = normalized.get("message") or (
                    "有效发票金额无法确认，可能是模型服务异常，请稍后重试或联系管理员处理。"
                )
                normalized["employeeSuggestionTips"] = normalized.get(
                    "employeeSuggestionTips"
                ) or "【模型异常】请稍后重试；如问题持续，请联系管理员处理。"
                normalized["problem_category"] = normalized.get(
                    "problem_category"
                ) or "模型服务异常"
                normalized["optimization_action_category"] = normalized.get(
                    "optimization_action_category"
                ) or "【稍后重试】【联系管理员】"

            # W33 是弱控，历史数据即使写成 REJECT/FAILED，也只能按 WARNING
            # 回写，避免弱控把整单升级为拒绝。
            elif reason_code == "W33" and status in {"reject", "failed"}:
                normalized["distinguish_result"] = "WARNING"
                normalized["distinguishResult"] = "WARNING"
                normalized["message"] = normalized.get("message") or (
                    "礼品数量与接待人数的匹配结果需要人工复核。"
                )

            rule_results.append(normalized)
    return rule_results


def _normalize_model_failure_rule_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Map legacy LLM ``FAILED`` rows to the business status contract.

    The graph now emits E36=REJECT and W31=WARNING directly.  This normalizer
    keeps old graph/runtime payloads safe during rolling upgrades and ensures a
    model outage can never be mistaken for a generic FAILED or a pass.
    """
    result = dict(value)
    reason_code = str(
        result.get("reason_code") or result.get("reasonCode") or ""
    ).strip().upper()
    status = str(
        result.get("distinguish_result") or result.get("distinguishResult") or ""
    ).strip().lower()
    rule_text = " ".join(
        str(result.get(key) or "")
        for key in (
            "message",
            "problem_category",
            "problemCategory",
            "employeeSuggestionTips",
            "suggestion",
        )
    ).lower()
    model_failure = any(
        token in rule_text
        for token in ("模型服务", "模型异常", "模型失败", "llm", "model service", "model failure")
    )
    if reason_code == "E31" and (status == "failed" or model_failure):
        result["distinguish_result"] = "REJECT"
        result["distinguishResult"] = "REJECT"
        result["message"] = result.get("message") or (
            "有效发票金额无法确认，可能是模型服务异常，请稍后重试或联系管理员处理。"
        )
        result["policiesIndex"] = result.get("policiesIndex") or ""
        result["employeeSuggestionTips"] = result.get("employeeSuggestionTips") or (
            "【模型异常】请稍后重试；如问题持续，请联系管理员处理。"
        )
        result["problem_category"] = result.get("problem_category") or "模型服务异常"
        result["optimization_action_category"] = (
            result.get("optimization_action_category") or "【稍后重试】【联系管理员】"
        )
    elif reason_code == "W33" and status in {"reject", "failed"}:
        result["distinguish_result"] = "WARNING"
        result["distinguishResult"] = "WARNING"
        result["message"] = result.get("message") or (
            "礼品数量与接待人数的匹配结果需要人工复核。"
        )
    elif reason_code in {"E17", "W40", "W32", "E34", "E36"} and (status == "failed" or model_failure):
        # E17/E34/E36 都依赖 LLM 完成内容或金额判断，模型失败时必须
        # 显式拒绝，不能沿用旧图的 FAILED 或因缺少结果而默认 PASS。
        failure_messages = {
            "E17": "模型服务暂时异常，当前充值卡检查未完成，请稍后重试或联系管理员处理。",
            "W40": "模型服务暂时异常，当前账单年份检查未完成，请稍后重试或联系管理员处理。",
            "W32": "模型服务暂时异常，当前手机号检查未完成，请稍后重试或联系管理员处理。",
            "E34": "模型服务暂时异常，当前发票内容金额检查未完成，请稍后重试或联系管理员处理。",
            "E36": "模型服务暂时异常，当前内容合规检查未完成，请稍后重试或联系管理员处理。",
        }
        result["distinguish_result"] = "REJECT"
        result["distinguishResult"] = "REJECT"
        result["message"] = result.get("message") or failure_messages[reason_code]
        result["policiesIndex"] = result.get("policiesIndex") or ""
        result["employeeSuggestionTips"] = result.get("employeeSuggestionTips") or (
            "【模型异常】请稍后重试；如问题持续，请联系管理员处理。"
        )
        result["problem_category"] = result.get("problem_category") or "模型服务异常"
        result["optimization_action_category"] = (
            result.get("optimization_action_category") or "【稍后重试】【联系管理员】"
        )
    elif reason_code == "W31" and (status == "failed" or model_failure):
        result["distinguish_result"] = "WARNING"
        result["distinguishResult"] = "WARNING"
        result["message"] = result.get("message") or (
            "模型服务暂时异常，当前虚开发票预警检查未完成，请稍后重试或联系管理员处理。"
        )
        result["policiesIndex"] = result.get("policiesIndex") or ""
        result["employeeSuggestionTips"] = result.get("employeeSuggestionTips") or (
            "【模型异常】请稍后重试；如问题持续，请联系管理员处理。"
        )
        result["problem_category"] = result.get("problem_category") or "模型服务异常"
        result["optimization_action_category"] = (
            result.get("optimization_action_category") or "【稍后重试】【联系管理员】"
        )
    return result


def _is_rule_result(value: Mapping[str, Any]) -> bool:
    return any(
        key in value
        for key in (
            "distinguish_result",
            "reason_code",
            "audit_content",
            "audit_type",
        )
    )


def _normalize_rule_distinguish_result(
    value: Any,
    *,
    reason_code: str | None = None,
) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()
    if normalized in {"pass", "passed"}:
        return "pass"
    if normalized in {"warn", "warning"}:
        return "warning"
    normalized_reason_code = str(reason_code or "").strip().upper()
    if normalized in {"fail", "failed"}:
        if normalized_reason_code == "E31":
            return "reject"
        if normalized_reason_code == "W33":
            return "warning"
        return "failed"
    if normalized == "reject":
        if normalized_reason_code == "W33":
            return "warning"
        return "reject"
    return normalized or None


def _select_primary_rule_result(
    decision_output: Mapping[str, Any],
    *,
    ignore_reason_codes: list[str] | None = None,
) -> dict[str, Any] | None:
    rule_results = _extract_rule_results(decision_output)
    if ignore_reason_codes:
        rule_results = [
            r for r in rule_results
            if (r.get("reason_code") or r.get("reasonCode")) not in ignore_reason_codes
        ]
    for candidate_status in ("reject", "failed", "warning"):
        for rule_result in rule_results:
            if _normalize_rule_distinguish_result(
                rule_result.get("distinguish_result"),
                reason_code=str(
                    rule_result.get("reason_code") or rule_result.get("reasonCode") or ""
                ),
            ) == candidate_status:
                return rule_result
    if rule_results:
        return rule_results[0]
    return None


def _resolve_reason_code(
    primary_rule_result: Mapping[str, Any] | None,
    decision_output: Mapping[str, Any],
) -> str | None:
    """解析回写用的 reasonCode。

    优先取主规则结果的 reason_code；若主规则结果不存在或其 reason_code 为空，
    则 fallback 到 decision_output 顶层的 reasonCode（执行图整体结论）。
    避免 primary_rule_result 存在但 reason_code 为空时 reasonCode 落为 null。
    """
    if primary_rule_result is not None:
        reason_code = primary_rule_result.get("reason_code")
        if reason_code:
            return reason_code
    return decision_output.get("reasonCode")
