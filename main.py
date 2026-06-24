import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import mock_server
from expense_audit_orchestrator.audit_client import DEFAULT_AUDIT_SERVICE_URL
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
        _wait_for_mock_server_service(DEFAULT_AUDIT_SERVICE_URL, process=process)
        yield DEFAULT_AUDIT_SERVICE_URL
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