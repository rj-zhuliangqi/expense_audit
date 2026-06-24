from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Sequence, cast
from urllib.parse import urlparse

try:
    import pika
except ImportError:  # pragma: no cover
    pika = None

from expense_audit_orchestrator.audit_client import (
    DEFAULT_AUDIT_SERVICE_URL,
    fetch_audit_task_info_list,
    update_audit_task_status,
)
from expense_audit_orchestrator import (
    DEFAULT_GRAPH_PATH,
    DEFAULT_OCR_PATH,
    create_receipt_audit_service,
)
from expense_audit_orchestrator.writeback_client import build_receipt_writeback_file_sink


DEFAULT_AMQP_URL = "amqp://guest:guest@127.0.0.1:5672/%2F"
DEFAULT_DELAY_TIME_MILLIS = 300000


@dataclass(slots=True)
class RabbitMQSettings:
    amqp_url: str = DEFAULT_AMQP_URL
    exchange_name: str = "audit_exchange"
    queue_name: str = "audit_ai_verification_queue"
    monthly_queue_name: str = "audit_monthly_queue"
    delay_process_queue_name: str = "audit_delay_process_queue"
    delay_queue_name: str = "audit_delay_queue"
    routing_key: str = "audit_ai_verification_routing_key"
    monthly_routing_key: str = "audit_monthly_routing_key"
    delay_process_routing_key: str = "audit_delay_process_routing_key"
    delay_routing_key: str = "audit_delay_routing_key"
    delay_time_millis: int = DEFAULT_DELAY_TIME_MILLIS
    prefetch_count: int = 1


def resolve_amqp_url(amqp_url: str | None = None) -> str:
    candidate = amqp_url or os.getenv("RABBITMQ_URL") or os.getenv("AMQP_URL")
    normalized = candidate.strip() if candidate is not None else ""
    return normalized or DEFAULT_AMQP_URL


def resolve_audit_service_url(audit_service_url: str | None = None) -> str:
    candidate = (
        audit_service_url
        or os.getenv("AUDIT_SERVICE_URL")
        or os.getenv("EXPENSE_AUDIT_SERVICE_URL")
        or DEFAULT_AUDIT_SERVICE_URL
    )
    normalized = candidate.strip() if candidate is not None else ""
    resolved = normalized or DEFAULT_AUDIT_SERVICE_URL

    if _is_local_audit_service_url(resolved):
        raise ValueError(
            "rabbitmq_worker 主链路不允许使用本地 mock 审单地址。"
            " 请通过 --audit-service-url 或环境变量 AUDIT_SERVICE_URL 指定真实网关地址。"
            f" 当前值: {resolved}"
        )

    return resolved


def _is_local_audit_service_url(service_url: str) -> bool:
    parsed = urlparse(service_url)
    hostname = (parsed.hostname or "").strip().lower()
    return hostname in {"127.0.0.1", "localhost", "0.0.0.0"}


def resolve_delay_time_millis(delay_time_millis: int | None = None) -> int:
    if delay_time_millis is not None:
        return delay_time_millis

    candidate = os.getenv("AUDIT_DELAY_TIME_MILLIS")
    normalized = candidate.strip() if candidate is not None else ""
    return int(normalized or DEFAULT_DELAY_TIME_MILLIS)


def parse_receipt_code(body: bytes | str) -> str:
    raw_text = body.decode("utf-8") if isinstance(body, bytes) else body
    payload_text = raw_text.strip()
    if not payload_text:
        raise ValueError("message body is empty")

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return payload_text

    if isinstance(payload, str):
        normalized = payload.strip()
        if normalized:
            return normalized
        raise ValueError("message body is empty")

    if not isinstance(payload, dict):
        raise ValueError("message payload must be a string or JSON object")

    for candidate in (payload, payload.get("data")):
        if not isinstance(candidate, dict):
            continue
        for key in ("receiptCode", "receipt_code", "instanceCode", "instance_code", "code"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    raise ValueError("message payload does not contain receipt code")


def declare_topology(channel: Any, settings: RabbitMQSettings) -> None:
    channel.exchange_declare(
        exchange=settings.exchange_name,
        exchange_type="direct",
        durable=True,
    )

    for queue_name in (
        settings.queue_name,
        settings.monthly_queue_name,
        settings.delay_process_queue_name,
    ):
        channel.queue_declare(queue=queue_name, durable=True)

    channel.queue_declare(
        queue=settings.delay_queue_name,
        durable=True,
        exclusive=False,
        auto_delete=False,
        arguments={
            "x-dead-letter-exchange": settings.exchange_name,
            "x-dead-letter-routing-key": settings.delay_process_routing_key,
            "x-message-ttl": settings.delay_time_millis,
        },
    )

    channel.queue_bind(
        queue=settings.queue_name,
        exchange=settings.exchange_name,
        routing_key=settings.routing_key,
    )
    channel.queue_bind(
        queue=settings.monthly_queue_name,
        exchange=settings.exchange_name,
        routing_key=settings.monthly_routing_key,
    )
    channel.queue_bind(
        queue=settings.delay_process_queue_name,
        exchange=settings.exchange_name,
        routing_key=settings.delay_process_routing_key,
    )
    channel.queue_bind(
        queue=settings.delay_queue_name,
        exchange=settings.exchange_name,
        routing_key=settings.delay_routing_key,
    )


def resolve_consumer_queues(
    settings: RabbitMQSettings,
    queues: Sequence[str] | str | None = None,
) -> list[str]:
    if queues is None:
        return [
            settings.queue_name,
            settings.monthly_queue_name,
            settings.delay_process_queue_name,
        ]

    raw_values = queues.split(",") if isinstance(queues, str) else list(queues)
    queue_aliases = {
        "audit": settings.queue_name,
        "default": settings.queue_name,
        settings.queue_name: settings.queue_name,
        "monthly": settings.monthly_queue_name,
        settings.monthly_queue_name: settings.monthly_queue_name,
        "delay-process": settings.delay_process_queue_name,
        "delay_process": settings.delay_process_queue_name,
        settings.delay_process_queue_name: settings.delay_process_queue_name,
    }

    resolved_queues: list[str] = []
    for raw_value in raw_values:
        normalized = raw_value.strip()
        if not normalized:
            continue

        queue_name = queue_aliases.get(normalized)
        if queue_name is None:
            raise ValueError(f"unsupported queue selector: {normalized}")
        if queue_name not in resolved_queues:
            resolved_queues.append(queue_name)

    if not resolved_queues:
        raise ValueError("at least one consumer queue must be selected")

    return resolved_queues


class ReceiptAuditWorker:
    def __init__(
        self,
        service: Any,
        *,
        settings: RabbitMQSettings | None = None,
        queues: Sequence[str] | str | None = None,
        ocr_sample_path: Path | str | None = None,
        prepared_output_dir: Path | str | None = None,
        task_info_list_provider: Any | None = None,
        task_status_update_provider: Any | None = None,
        bypass_task_gate: bool = False,
    ) -> None:
        self._service = service
        self._settings = settings or RabbitMQSettings()
        self._queues = resolve_consumer_queues(self._settings, queues)
        self._ocr_sample_path = ocr_sample_path
        self._prepared_output_dir = Path(prepared_output_dir) if prepared_output_dir is not None else None
        self._task_info_list_provider = task_info_list_provider
        self._task_status_update_provider = task_status_update_provider
        self._bypass_task_gate = bypass_task_gate

    def handle_delivery(self, channel: Any, method: Any, _properties: Any, body: bytes) -> None:
        try:
            receipt_code = parse_receipt_code(body)
            source = getattr(method, "routing_key", "unknown")
            print(f"[rabbitmq] received receipt_code={receipt_code} source={source}")

            if not self._should_process_receipt(receipt_code):
                channel.basic_ack(delivery_tag=method.delivery_tag)
                return

            prepared_receipt = self._prepare_receipt(receipt_code)
            if prepared_receipt is not None:
                self._export_prepared_receipt(receipt_code, prepared_receipt)
                print(
                    f"[rabbitmq] prepared receipt_code={receipt_code} "
                    f"invoice_count={_resolve_invoice_count(prepared_receipt)}"
                )

            result = self._process_receipt(receipt_code, prepared_receipt)
            print(
                f"[rabbitmq] completed receipt_code={receipt_code} "
                f"invoice_count={_resolve_invoice_count(result)}"
            )
            channel.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as exc:
            print(f"[rabbitmq] failed to process message: {exc}", file=sys.stderr)
            traceback.print_exc()
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def _supports_two_stage_processing(self) -> bool:
        prepare_method = getattr(self._service, "prepare_receipt", None)
        process_prepared_method = getattr(self._service, "process_prepared_receipt", None)
        return callable(prepare_method) and callable(process_prepared_method)

    def _prepare_receipt(self, receipt_code: str) -> dict[str, Any] | None:
        if not self._supports_two_stage_processing():
            return None

        prepare_method = getattr(self._service, "prepare_receipt")
        if self._ocr_sample_path is None:
            return prepare_method(receipt_code)
        return prepare_method(receipt_code, self._ocr_sample_path)

    def _process_receipt(
        self,
        receipt_code: str,
        prepared_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        process_prepared_method = getattr(self._service, "process_prepared_receipt", None)
        if prepared_receipt is not None and callable(process_prepared_method):
            return cast(dict[str, Any], process_prepared_method(prepared_receipt))

        process_method = getattr(self._service, "process_receipt", None)
        if callable(process_method):
            if self._ocr_sample_path is None:
                return cast(dict[str, Any], process_method(receipt_code))
            return cast(dict[str, Any], process_method(receipt_code, self._ocr_sample_path))

        if self._ocr_sample_path is None:
            return cast(dict[str, Any], self._service.evaluate(receipt_code))
        return cast(dict[str, Any], self._service.evaluate(receipt_code, self._ocr_sample_path))

    def _should_process_receipt(self, receipt_code: str) -> bool:
        if self._bypass_task_gate:
            print(f"[rabbitmq] bypassed task gate for receipt_code={receipt_code}")
            return True

        if not callable(self._task_info_list_provider) or not callable(self._task_status_update_provider):
            return True

        task_info_list = self._task_info_list_provider(receipt_code)
        matched_task = _select_pending_ruijie_task(task_info_list)
        if matched_task is None:
            print(f"[rabbitmq] skipped receipt_code={receipt_code} reason=no pending ruijieAI task")
            return False

        system_identifier = _resolve_task_int(matched_task, "systemIdentifier") or 4
        try:
            update_result = self._task_status_update_provider(
                receipt_code,
                new_status=1,
                system_identifier=system_identifier,
            )
        except Exception as exc:
            print(f"[rabbitmq] skipped receipt_code={receipt_code} reason=task status update failed: {exc}")
            return False

        if not _is_task_status_update_success(update_result):
            print(
                f"[rabbitmq] skipped receipt_code={receipt_code} "
                "reason=task status update reported unsuccessful result"
            )
            return False

        return True

    def _export_prepared_receipt(self, receipt_code: str, prepared_receipt: dict[str, Any]) -> None:
        if self._prepared_output_dir is None:
            return

        output_file = self._prepared_output_dir / f"{receipt_code}.prepared-receipt.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(
            json.dumps(prepared_receipt, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        print(f"[rabbitmq] exported prepared receipt to: {output_file}")

    def run_forever(self) -> None:
        connection = create_blocking_connection(self._settings)
        channel = connection.channel()

        try:
            declare_topology(channel, self._settings)
            channel.basic_qos(prefetch_count=self._settings.prefetch_count)

            for queue_name in self._queues:
                channel.basic_consume(
                    queue=queue_name,
                    on_message_callback=self.handle_delivery,
                    auto_ack=False,
                )
                print(f"[rabbitmq] consuming queue={queue_name}")

            channel.start_consuming()
        except KeyboardInterrupt:
            print("[rabbitmq] received interrupt, stopping consumer")
        finally:
            if getattr(channel, "is_open", False):
                try:
                    channel.stop_consuming()
                except Exception:
                    pass
            if getattr(connection, "is_open", False):
                connection.close()


def create_blocking_connection(settings: RabbitMQSettings):
    if pika is None:
        raise RuntimeError("pika is not installed. Please install requirements.txt before starting rabbitmq_worker.py")

    try:
        return pika.BlockingConnection(pika.URLParameters(settings.amqp_url))
    except _resolve_amqp_connection_error() as exc:
        raise RuntimeError(
            "无法连接 RabbitMQ。"
            f" 当前地址: {settings.amqp_url}。"
            " 请确认 RabbitMQ 服务已启动，并且目标主机/端口可访问。"
            " 如果不是本机默认地址，请通过 --amqp-url 或环境变量 RABBITMQ_URL/AMQP_URL 指定正确连接串。"
            " 常见本地检查项: 5672 端口是否监听、用户名密码是否正确、vhost 是否存在。"
            f" 原始错误: {exc}"
        ) from exc


def _resolve_amqp_connection_error() -> type[Exception]:
    if pika is None:
        return Exception

    return cast(type[Exception], getattr(getattr(pika, "exceptions", None), "AMQPConnectionError", Exception))


def _resolve_invoice_count(result: Any) -> int | None:
    if not isinstance(result, dict):
        return None

    invoice_count = result.get("invoiceCount")
    if isinstance(invoice_count, int):
        return invoice_count

    invoice_results = result.get("invoiceResults")
    if isinstance(invoice_results, list):
        return len(invoice_results)

    return None


def _resolve_task_int(task: Any, key: str) -> int | None:
    if not isinstance(task, dict):
        return None

    value = task.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return int(normalized)
        except ValueError:
            return None

    return None


def _select_pending_ruijie_task(task_info_list: Any) -> dict[str, Any] | None:
    if not isinstance(task_info_list, list):
        raise ValueError("task info list service returned invalid payload")

    for task in task_info_list:
        if _resolve_task_int(task, "systemIdentifier") != 4:
            continue
        if _resolve_task_int(task, "anaStatus") != 0:
            continue
        if isinstance(task, dict):
            return task

    return None


def _is_task_status_update_success(update_result: Any) -> bool:
    if isinstance(update_result, dict):
        if "data" not in update_result:
            return True
        return not _is_explicit_unsuccessful_result(update_result.get("data"))

    return not _is_explicit_unsuccessful_result(update_result)


def _is_explicit_unsuccessful_result(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {"", "0", "false", "failed", "fail", "null", "none"}

    return False


def create_worker(
    *,
    graph_path: Path | str = DEFAULT_GRAPH_PATH,
    audit_service_url: str | None = None,
    graph_runtime_url: str | None = None,
    ocr_sample_path: Path | str | None = None,
    settings: RabbitMQSettings | None = None,
    queues: Sequence[str] | str | None = None,
    prepared_output_dir: Path | str | None = None,
    writeback_output_dir: Path | str | None = None,
    bypass_task_gate: bool = False,
) -> ReceiptAuditWorker:
    resolved_audit_service_url = resolve_audit_service_url(audit_service_url)
    receipt_result_sink = None
    if writeback_output_dir is not None:
        receipt_result_sink = build_receipt_writeback_file_sink(writeback_output_dir)

    service = create_receipt_audit_service(
        graph_path=graph_path,
        audit_service_url=resolved_audit_service_url,
        graph_runtime_url=graph_runtime_url,
        receipt_result_sink=receipt_result_sink,
        enable_writeback=True,
    )
    return ReceiptAuditWorker(
        service=service,
        settings=settings,
        queues=queues,
        ocr_sample_path=ocr_sample_path,
        prepared_output_dir=prepared_output_dir,
        task_info_list_provider=partial(fetch_audit_task_info_list, service_url=resolved_audit_service_url),
        task_status_update_provider=partial(update_audit_task_status, service_url=resolved_audit_service_url),
        bypass_task_gate=bypass_task_gate,
    )


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Consume receipt audit tasks from RabbitMQ and execute downstream graph runtime"
    )
    parser.add_argument("--amqp-url", default=resolve_amqp_url())
    parser.add_argument("--graph-path", default=str(DEFAULT_GRAPH_PATH))
    parser.add_argument("--audit-service-url")
    parser.add_argument("--graph-runtime-url")
    parser.add_argument("--ocr-sample-path", default=str(DEFAULT_OCR_PATH))
    parser.add_argument("--prepared-output-dir")
    parser.add_argument("--writeback-output-dir")
    parser.add_argument("--delay-time-millis", type=int, default=resolve_delay_time_millis())
    parser.add_argument(
        "--bypass-task-gate",
        action="store_true",
        help="跳过稽核任务列表查询和状态占用，直接进入下游处理",
    )
    parser.add_argument(
        "--queues",
        default="audit,monthly,delay-process",
        help="逗号分隔，可选 audit、monthly、delay-process",
    )
    return parser


def main_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    settings = RabbitMQSettings(
        amqp_url=resolve_amqp_url(args.amqp_url),
        delay_time_millis=resolve_delay_time_millis(args.delay_time_millis),
    )
    worker = create_worker(
        graph_path=args.graph_path,
        audit_service_url=resolve_audit_service_url(args.audit_service_url),
        graph_runtime_url=args.graph_runtime_url,
        ocr_sample_path=args.ocr_sample_path,
        settings=settings,
        queues=args.queues,
        prepared_output_dir=args.prepared_output_dir,
        writeback_output_dir=args.writeback_output_dir,
        bypass_task_gate=args.bypass_task_gate,
    )
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())