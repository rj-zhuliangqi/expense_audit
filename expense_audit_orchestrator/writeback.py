import json

from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4


ComplianceRule = Callable[[str, Mapping[str, Any]], bool]
AuditTravelsBuilder = Callable[[list[tuple[dict[str, Any], dict[str, Any]]], Mapping[str, Any]], list[dict[str, Any]]]
FormBuilder = Callable[[list[tuple[dict[str, Any], dict[str, Any]]], Mapping[str, Any]], list[dict[str, Any]]]


# E31 (发票合计金额不足) employeeSuggestionTips 文本 — CSV 第四列，两场景全文。
# 制度列 CSV 为 "/"（空），故 E31 policiesIndex 恒为空。
E31_SUGGESTION = (
    "场景1：前面检查有未通过发票，导致有效金额不足\n"
    "建议：【整改问题】整改下方标记问题的发票，整改后会重新计入有效金额；\n\n"
    "场景2：漏传发票或发票识别不清晰，导致有效金额不足\n"
    "建议：【检查上传】请补充上传电子发票，如为纸质发票，请拍照上传。"
    "如均已上传发票，请确认纸质发票金额识别是否正确"
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
) -> dict[str, Any]:
    receipt_code = str(processed_receipt.get("receiptCode") or prepared_receipt.get("receiptCode") or "")
    service_data = dict(prepared_receipt.get("serviceData") or processed_receipt.get("serviceData") or {})
    audit_info = dict(service_data.get("auditInfo") or {})
    instance_code = _get_string_value(audit_info, "instanceCode") or receipt_code

    invoice_pairs = _pair_invoices(prepared_receipt, processed_receipt)
    is_amount_sufficient = processed_receipt.get("isAmountSufficient")

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
            apply_amount=apply_amount,
            valid_invoice_total=valid_invoice_total,
        ),
        "auditInvoiceInfos": _build_audit_invoice_infos(instance_code, audit_info, invoice_pairs, is_amount_sufficient=is_amount_sufficient),
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
    # 核销单级整体建议：仅在非空时输出，避免给下游送 null。
    overall_advice = processed_receipt.get("aiAuditAdvice")
    if isinstance(overall_advice, str) and overall_advice.strip():
        result["aiAuditAdvice"] = overall_advice.strip()
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
        decision_output = dict(result.get("decisionOutput") or {})
        final_amount = (
            decision_output.get("invoice_finalAmount")
            or (decision_output.get("invoice_content_valid_result") or {}).get("invoice_finalAmount")
        )
        if final_amount is not None:
            try:
                total += float(final_amount)
                found = True
            except (ValueError, TypeError):
                pass
    return total if found else None


def _build_e31_message(apply_amount: float | None, valid_invoice_total: float | None) -> str:
    """Build the receipt-level E31 message with real totals.

    CSV 模板：当前核销单有效发票合计金额**元，低于报销单报销金额**元，
    有效发票金额缺少**元。 当金额不可得时退回静态文案。
    """
    if apply_amount is None or valid_invoice_total is None:
        return "发票合计金额不足"
    shortage = apply_amount - valid_invoice_total
    if shortage < 0:
        shortage = 0.0
    return (
        f"当前核销单有效发票合计金额{_format_amount(valid_invoice_total)}元，"
        f"低于报销单报销金额{_format_amount(apply_amount)}元，"
        f"有效发票金额缺少{_format_amount(shortage)}元。"
    )


def _format_amount(value: float) -> str:
    # 保留两位小数，去掉无意义的尾零（10.00 -> 10，10.50 -> 10.5）
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _build_audit_logs(
    instance_code: str,
    audit_info: Mapping[str, Any],
    invoice_pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    is_amount_sufficient: bool | None = None,
    apply_amount: float | None = None,
    valid_invoice_total: float | None = None,
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
            for rule_result in rule_results:
                reason_code = rule_result.get("reason_code") or rule_result.get("reasonCode")
                if reason_code == "E31":
                    if not is_last:
                        continue
                    if is_amount_sufficient is not None:
                        rule_result = dict(rule_result)
                        if is_amount_sufficient:
                            rule_result["distinguish_result"] = "PASS"
                            rule_result["distinguishResult"] = "PASS"
                            rule_result["message"] = "发票合计金额充足"
                            rule_result["policiesIndex"] = ""
                            rule_result["employeeSuggestionTips"] = ""
                        else:
                            rule_result["distinguish_result"] = "REJECT"
                            rule_result["distinguishResult"] = "REJECT"
                            rule_result["message"] = _build_e31_message(
                                apply_amount, valid_invoice_total
                            )
                            rule_result["policiesIndex"] = ""
                            rule_result["employeeSuggestionTips"] = E31_SUGGESTION

                audit_logs.append(
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
                            rule_result.get("distinguish_result") or rule_result.get("distinguishResult")
                        )
                        or result.get("decisionStatus"),
                        "message": rule_result.get("message") or result.get("errorMessage"),
                        "policiesIndex": rule_result.get("policiesIndex"),
                        "employeeSuggestionTips": rule_result.get("employeeSuggestionTips"),
                    }
                )
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
                "policiesIndex": decision_output.get("policiesIndex"),
                "employeeSuggestionTips": decision_output.get("employeeSuggestionTips"),
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
) -> list[dict[str, Any]]:
    invoice_infos: list[dict[str, Any]] = []
    for index, (preparation, result) in enumerate(invoice_pairs):
        is_last = (index == len(invoice_pairs) - 1)
        prepared_input = _resolve_prepared_input(preparation, result)
        service_data = dict(prepared_input.get("serviceData") or {})
        current_invoice_info = _resolve_first_mapping(service_data.get("currentInvoiceInfo"))
        current_audit_invoice_file = _resolve_current_audit_invoice_file(preparation, prepared_input)
        decision_output = dict(result.get("decisionOutput") or {})

        if is_last and is_amount_sufficient is not None and "amount_result" in decision_output:
            overridden_amount_result = dict(decision_output["amount_result"])
            if is_amount_sufficient:
                overridden_amount_result["distinguish_result"] = "PASS"
                overridden_amount_result["distinguishResult"] = "PASS"
                overridden_amount_result["message"] = "发票合计金额充足"
                overridden_amount_result["policiesIndex"] = ""
                overridden_amount_result["employeeSuggestionTips"] = ""
            else:
                overridden_amount_result["distinguish_result"] = "REJECT"
                overridden_amount_result["distinguishResult"] = "REJECT"
                overridden_amount_result["message"] = "发票合计金额不足"
                overridden_amount_result["policiesIndex"] = ""
                overridden_amount_result["employeeSuggestionTips"] = E31_SUGGESTION
            decision_output = {**decision_output, "amount_result": overridden_amount_result}

        ignore_codes = [] if is_last else ["E31"]
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
                "reasonCode": (
                    primary_rule_result.get("reason_code")
                    if primary_rule_result
                    else decision_output.get("reasonCode")
                ),
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
                if field_name is None or field_name not in invoice_record:
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
                if field_name is None or field_name not in invoice_record:
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
                if field_name is None or field_name not in invoice_record:
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
            rule_results.append(dict(value))
    return rule_results


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


def _normalize_rule_distinguish_result(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()
    if normalized in {"pass", "passed"}:
        return "pass"
    if normalized in {"warn", "warning"}:
        return "warning"
    if normalized in {"fail", "failed"}:
        return "failed"
    if normalized == "reject":
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
            if _normalize_rule_distinguish_result(rule_result.get("distinguish_result")) == candidate_status:
                return rule_result
    if rule_results:
        return rule_results[0]
    return None