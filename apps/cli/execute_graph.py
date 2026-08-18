import argparse
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Sequence

from expense_audit_orchestrator import audit_client
from expense_audit_orchestrator.bootstrap import create_receipt_audit_service
from expense_audit_orchestrator.core import DEFAULT_OCR_PATH
from expense_audit_orchestrator.runtime_client import DEFAULT_GRAPH_PATH, create_graph_runtime_client


DEFAULT_RECEIPT_CODE = "REC20260603001"
DEFAULT_AUDIT_SERVICE_URL = audit_client.DEFAULT_AUDIT_SERVICE_URL


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


@contextmanager
def ensure_audit_service_url(audit_service_url: str | None = None):
    """解析上游核销单服务地址。

    未显式传入时沿用 ``DEFAULT_AUDIT_SERVICE_URL``（生产真实网关，或通过环境变量
    ``AUDIT_SERVICE_URL`` 覆盖）。本地不再拉起 mock 服务。
    """
    yield audit_service_url or audit_client.DEFAULT_AUDIT_SERVICE_URL


def run_graph(
    receipt_code: str = DEFAULT_RECEIPT_CODE,
    *,
    profile: str = "telecom",
    graph_path: Path | str | None = None,
    ocr_sample_path: Path | str = DEFAULT_OCR_PATH,
    prepared_output_path: Path | str | None = None,
    audit_service_url: str | None = None,
    graph_runtime_url: str | None = None,
    telecom_asset_dir: Path | str | None = None,
) -> dict[str, Any]:
    audit_service_url_context = (
        ensure_audit_service_url()
        if audit_service_url is None
        else nullcontext(audit_service_url)
    )

    with audit_service_url_context as resolved_audit_service_url:
        expense_service = create_receipt_audit_service(
            profile=profile,
            graph_path=graph_path,
            audit_service_url=resolved_audit_service_url,
            graph_runtime_url=graph_runtime_url,
            telecom_asset_dir=telecom_asset_dir,
        )

        print("🤖 开始执行独立图执行测试...")
        print(f"🚀 测试单据号: {receipt_code}")
        print(f"🧭 使用流程图: {graph_path}")

        evaluation_result = expense_service.evaluate(receipt_code, ocr_sample_path)
        if prepared_output_path is not None:
            export_prepared_input(evaluation_result["preparedInput"], prepared_output_path)
        print_decision_output(evaluation_result["decisionOutput"])
        return evaluation_result


def export_prepared_input_only(
    receipt_code: str = DEFAULT_RECEIPT_CODE,
    *,
    profile: str = "telecom",
    graph_path: Path | str | None = None,
    ocr_sample_path: Path | str = DEFAULT_OCR_PATH,
    prepared_output_path: Path | str,
    audit_service_url: str | None = None,
    graph_runtime_url: str | None = None,
    telecom_asset_dir: Path | str | None = None,
) -> dict[str, Any]:
    audit_service_url_context = (
        ensure_audit_service_url()
        if audit_service_url is None
        else nullcontext(audit_service_url)
    )

    with audit_service_url_context as resolved_audit_service_url:
        expense_service = create_receipt_audit_service(
            profile=profile,
            graph_path=graph_path,
            audit_service_url=resolved_audit_service_url,
            graph_runtime_url=graph_runtime_url,
            telecom_asset_dir=telecom_asset_dir,
        )

        print("🤖 开始执行独立图执行测试...")
        print(f"🚀 测试单据号: {receipt_code}")
        print(f"🧭 使用流程图: {graph_path}")
        print("🧪 仅导出准备数据，不执行 graph runtime")

        prepared_input = expense_service.prepare_input(receipt_code, ocr_sample_path)
        export_prepared_input(prepared_input, prepared_output_path)
        return prepared_input


def _resolve_prepared_input_payload(payload: dict[str, Any]) -> dict[str, Any]:
    invoice_preparations = payload.get("invoicePreparations")
    if isinstance(invoice_preparations, list):
        for item in invoice_preparations:
            if not isinstance(item, dict):
                continue
            prepared_input = item.get("preparedInput")
            if isinstance(prepared_input, dict):
                return prepared_input

    return payload


def run_graph_with_prepared_input(
    prepared_input_path: Path | str,
    *,
    graph_path: Path | str = DEFAULT_GRAPH_PATH,
    prepared_output_path: Path | str | None = None,
    graph_runtime_url: str | None = None,
) -> dict[str, Any]:
    payload = json.loads(Path(prepared_input_path).read_text(encoding="utf-8"))
    prepared_input = _resolve_prepared_input_payload(payload)
    runtime_client = create_graph_runtime_client(graph_runtime_url)
    result = runtime_client.evaluate(prepared_input=prepared_input, graph_path=graph_path)
    decision_output = result["decisionOutput"]

    print("🤖 开始执行独立图执行测试...")
    print(f"🧭 使用流程图: {graph_path}")
    print(f"📥 使用规则输入: {prepared_input_path}")

    if prepared_output_path is not None:
        export_prepared_input(prepared_input, prepared_output_path)

    print_decision_output(decision_output)
    return result


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute a built GoRules graph against expense audit data")
    parser.add_argument("--receipt-code", default=DEFAULT_RECEIPT_CODE)
    parser.add_argument("--profile", default="telecom")
    parser.add_argument("--graph-path", default=str(DEFAULT_GRAPH_PATH))
    parser.add_argument("--ocr-sample-path", default=str(DEFAULT_OCR_PATH))
    parser.add_argument(
        "--telecom-asset-dir",
        help="通讯费 operator_city.csv 所在目录；默认用包内资产",
    )
    parser.add_argument(
        "--graph-runtime-url",
        help="指定下游 graph runtime 地址；未传时优先读 GRAPH_RUNTIME_URL，否则默认 http://127.0.0.1:8090",
    )
    parser.add_argument(
        "--prepared-input-path",
        help="直接读取已有的规则输入 JSON 并执行，不再调用 OCR 或上游服务",
    )
    parser.add_argument(
        "--prepared-output-path",
        help="将准备好的规则输入导出到指定 JSON 文件",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="只导出准备好的规则输入 JSON，不执行 graph runtime",
    )
    parser.add_argument(
        "--audit-service-url",
        help="指定上游核销单服务地址；未传时使用 DEFAULT_AUDIT_SERVICE_URL（可用环境变量 AUDIT_SERVICE_URL 覆盖）",
    )
    return parser


def main_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    if args.prepare_only and args.prepared_input_path is not None:
        parser.error("--prepare-only cannot be used with --prepared-input-path")

    if args.prepare_only and args.prepared_output_path is None:
        parser.error("--prepare-only requires --prepared-output-path")

    if args.prepared_input_path is not None:
        run_graph_with_prepared_input(
            prepared_input_path=args.prepared_input_path,
            graph_path=args.graph_path,
            prepared_output_path=args.prepared_output_path,
            graph_runtime_url=args.graph_runtime_url,
        )
        return 0

    if args.prepare_only:
        export_prepared_input_only(
            receipt_code=args.receipt_code,
            profile=args.profile,
            graph_path=args.graph_path,
            ocr_sample_path=args.ocr_sample_path,
            prepared_output_path=args.prepared_output_path,
            audit_service_url=args.audit_service_url,
            graph_runtime_url=args.graph_runtime_url,
            telecom_asset_dir=args.telecom_asset_dir,
        )
        return 0

    run_graph(
        receipt_code=args.receipt_code,
        profile=args.profile,
        graph_path=args.graph_path,
        ocr_sample_path=args.ocr_sample_path,
        prepared_output_path=args.prepared_output_path,
        audit_service_url=args.audit_service_url,
        graph_runtime_url=args.graph_runtime_url,
        telecom_asset_dir=args.telecom_asset_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())