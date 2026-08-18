import json
from pathlib import Path
from typing import Any

from expense_audit_orchestrator import (
    DEFAULT_OCR_PATH,
    ReceiptDataPreparer,
    build_rule_input,
    call_ocr_service,
    create_receipt_audit_service,
    fetch_audit_info,
    fetch_audit_invoice_file_info,
    fetch_audit_invoice_files,
    fetch_company_blacklist,
    fetch_company_list,
    fetch_expense_invoice_types,
    fetch_invoice_info,
    get_invoice_file_from_server,
)
from expense_audit_orchestrator.audit_client import DEFAULT_AUDIT_SERVICE_URL
from graph_runtime import DEFAULT_GRAPH_PATH, evaluate_prepared_input, load_decision


DEFAULT_RECEIPT_CODE = "REC20260603001"


def print_decision_output(decision_output: dict[str, Any]) -> None:
    print(f"🎯 [规则判定结束] 引擎返回结果:\n {json.dumps(decision_output, ensure_ascii=False, indent=4)}")

    if decision_output.get("checkStatus") == "failed":
        print(f"❌ 单据未通过规则校验:\n  {decision_output.get('message')}")
    elif decision_output.get("checkStatus") == "warning":
        print(f"⚠️ 单据需要人工复核:\n  {decision_output.get('message')}")
    else:
        print("✅ 规则通过！可以进入下一步处理")


def build_prepared_input_export(prepared_input: dict[str, Any]) -> dict[str, Any]:
    return prepared_input


def export_prepared_input(prepared_input: dict[str, Any], output_path: Path | str) -> Path:
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(build_prepared_input_export(prepared_input), ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    print(f"📝 已导出准备数据到: {output_file}")
    return output_file


# ==========================================
# 2. 核心：单据处理流水线 (包含规则引擎执行)
# ==========================================
def process_receipt_pipeline(
    receipt_code: str,
    decision_engine: Any,
    ocr_sample_path: Path | str = DEFAULT_OCR_PATH,
    data_preparer: ReceiptDataPreparer | None = None,
) -> dict[str, Any]:
    print(f"\n🚀 开始处理新单据: {receipt_code}")

    prepared_input = (data_preparer or ReceiptDataPreparer()).prepare(receipt_code, ocr_sample_path)
    evaluation_result = evaluate_prepared_input(
        decision_engine,
        prepared_input,
        receipt_code=receipt_code,
    )
    decision_output = evaluation_result["decisionOutput"]
    print_decision_output(decision_output)

    return decision_output