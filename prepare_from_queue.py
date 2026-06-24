from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

try:
    from dotenv import dotenv_values, load_dotenv
except ImportError:  # pragma: no cover
    dotenv_values = None
    load_dotenv = None

from expense_audit_orchestrator import (
    DEFAULT_GRAPH_PATH,
    DEFAULT_OCR_PATH,
    assemble_result_audit_info,
    create_receipt_audit_service,
)
from expense_audit_orchestrator.audit_client import DEFAULT_AUDIT_SERVICE_URL
from rabbitmq_worker import RabbitMQSettings, create_blocking_connection, parse_receipt_code, resolve_amqp_url


PROJECT_ROOT = Path(__file__).resolve().parent


def load_project_env() -> None:
    if load_dotenv is None:
        return

    env_path = PROJECT_ROOT / ".env"
    env_values = dotenv_values(env_path) if dotenv_values is not None else {}
    load_dotenv(env_path, override=False)

    for key, value in (env_values or {}).items():
        if value is None:
            continue

        current_value = os.getenv(key)
        if current_value is None or not current_value.strip():
            os.environ[key] = value


def export_prepared_receipt(prepared_receipt: dict[str, Any], output_path: Path | str) -> Path:
    return _export_json_payload(prepared_receipt, output_path, "prepared receipt")


def export_writeback_payload(writeback_payload: dict[str, Any], output_path: Path | str) -> Path:
    return _export_json_payload(writeback_payload, output_path, "writeback payload")


def _export_json_payload(payload: dict[str, Any], output_path: Path | str, label: str) -> Path:
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    print(f"[queue-prepare] exported {label} to: {output_file}")
    return output_file


def consume_and_prepare_once(
    *,
    service: Any | None,
    settings: RabbitMQSettings,
    queue_name: str,
    ocr_sample_path: Path | str | None = None,
    prepared_output_path: Path | str | None = None,
    writeback_output_path: Path | str | None = None,
    ack_on_success: bool = False,
    receipt_code_only: bool = False,
) -> int:
    connection = create_blocking_connection(settings)
    channel = connection.channel()
    method = None

    try:
        method, _properties, body = channel.basic_get(queue=queue_name, auto_ack=False)
        if method is None:
            print(f"[queue-prepare] queue={queue_name} is empty")
            return 0

        receipt_code = parse_receipt_code(body)
        source = getattr(method, "routing_key", "unknown")
        print(f"[queue-prepare] received receipt_code={receipt_code} source={source}")

        if receipt_code_only:
            print(f"[queue-prepare] receipt_code={receipt_code}")
            if ack_on_success:
                channel.basic_ack(delivery_tag=method.delivery_tag)
                print(f"[queue-prepare] acked delivery_tag={method.delivery_tag}")
            else:
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                print(f"[queue-prepare] requeued delivery_tag={method.delivery_tag}")
            return 0

        if service is None:
            raise ValueError("service is required unless receipt_code_only is enabled")

        prepared_receipt = service.prepare_receipt(receipt_code, ocr_sample_path or DEFAULT_OCR_PATH)
        if prepared_output_path is not None:
            export_prepared_receipt(prepared_receipt, prepared_output_path)

        if writeback_output_path is not None:
            process_prepared_method = getattr(service, "process_prepared_receipt", None)
            if not callable(process_prepared_method):
                raise ValueError("service does not support process_prepared_receipt required for writeback export")

            processed_receipt = process_prepared_method(prepared_receipt)
            writeback_payload = assemble_result_audit_info(prepared_receipt, processed_receipt)
            export_writeback_payload(writeback_payload, writeback_output_path)

        print(
            f"[queue-prepare] prepared receipt_code={receipt_code} "
            f"invoice_count={prepared_receipt.get('invoiceCount')}"
        )

        if ack_on_success:
            channel.basic_ack(delivery_tag=method.delivery_tag)
            print(f"[queue-prepare] acked delivery_tag={method.delivery_tag}")
        else:
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            print(f"[queue-prepare] requeued delivery_tag={method.delivery_tag}")

        return 0
    except Exception as exc:
        print(f"[queue-prepare] failed to prepare message: {exc}", file=sys.stderr)
        traceback.print_exc()
        if method is not None:
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        return 1
    finally:
        if getattr(connection, "is_open", False):
            connection.close()


def build_cli_parser() -> argparse.ArgumentParser:
    load_project_env()

    parser = argparse.ArgumentParser(
        description="Consume one RabbitMQ task and run receipt data preparation only"
    )
    parser.add_argument("--amqp-url", default=resolve_amqp_url())
    parser.add_argument("--queue", default=RabbitMQSettings().queue_name)
    parser.add_argument("--graph-path", default=str(DEFAULT_GRAPH_PATH))
    parser.add_argument("--audit-service-url", default=DEFAULT_AUDIT_SERVICE_URL)
    parser.add_argument("--ocr-sample-path", default=str(DEFAULT_OCR_PATH))
    parser.add_argument("--prepared-output-path")
    parser.add_argument(
        "--writeback-output-path",
        help="执行 prepare + runtime，并导出最终回写 payload；不会发起真实回调",
    )
    parser.add_argument(
        "--ack-on-success",
        action="store_true",
        help="准备成功后 ack；默认会 requeue，避免联调脚本误吞消息",
    )
    parser.add_argument(
        "--receipt-code-only",
        action="store_true",
        help="只读取并打印一条队列消息中的核销单号，不执行数据准备",
    )
    return parser


def main_cli(argv: Sequence[str] | None = None) -> int:
    load_project_env()
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    settings = RabbitMQSettings(amqp_url=resolve_amqp_url(args.amqp_url))
    service = None
    if not args.receipt_code_only:
        service = create_receipt_audit_service(
            graph_path=args.graph_path,
            audit_service_url=args.audit_service_url,
        )
    return consume_and_prepare_once(
        service=service,
        settings=settings,
        queue_name=args.queue,
        ocr_sample_path=args.ocr_sample_path,
        prepared_output_path=args.prepared_output_path,
        writeback_output_path=args.writeback_output_path,
        ack_on_success=args.ack_on_success,
        receipt_code_only=args.receipt_code_only,
    )


if __name__ == "__main__":
    raise SystemExit(main_cli())