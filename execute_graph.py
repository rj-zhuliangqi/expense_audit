import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Sequence
from urllib.request import urlopen

import mock_server
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
def ensure_mock_audit_service_url():
    port = mock_server.PORT
    existing_pid = _find_pid_listening_on_port(port)
    if existing_pid is not None:
        _stop_process_on_port(port, existing_pid)

    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().with_name("mock_server.py"))],
        cwd=str(Path(__file__).resolve().parent),
    )
    try:
        _wait_for_mock_server_service(audit_client.DEFAULT_AUDIT_SERVICE_URL, process=process)
        yield audit_client.DEFAULT_AUDIT_SERVICE_URL
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _find_pid_listening_on_port(port: int) -> int | None:
    try:
        result = subprocess.run(
            ["ss", "-ltnp", f"( sport = :{port} )"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    match = re.search(r"pid=(\d+)", result.stdout)
    if match is None:
        return None

    return int(match.group(1))


def _stop_process_on_port(port: int, pid: int, timeout: float = 3.0) -> None:
    print(f"[mock_server] 检测到 {port} 端口被进程 {pid} 占用，正在停止旧进程...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    if _wait_for_port_release(port, timeout=timeout):
        return

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return

    if _wait_for_port_release(port, timeout=timeout):
        return

    raise RuntimeError(f"无法释放端口 {port}，旧进程 {pid} 仍在占用")


def _wait_for_port_release(port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _find_pid_listening_on_port(port) is None:
            return True
        time.sleep(0.1)
    return False


def _wait_for_mock_server_service(
    service_url: str,
    *,
    process: Any | None = None,
    timeout: float = 5.0,
) -> None:
    endpoint = f"{service_url.rstrip('/')}/audit/companyblacklist"
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("mock_server.py 启动失败，进程已提前退出")

        try:
            with urlopen(endpoint, timeout=0.5) as response:
                payload = json.load(response)
            if payload.get("code") == 0:
                return
            last_error = RuntimeError(f"mock_server.py 返回了异常响应: {payload}")
        except Exception as exc:
            last_error = exc

        time.sleep(0.1)

    raise RuntimeError(f"mock_server.py 未能在 {service_url} 上就绪") from last_error


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
        ensure_mock_audit_service_url()
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
        ensure_mock_audit_service_url()
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
        help="指定上游核销单服务地址；未传时自动拉起本地 mock_server.py",
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