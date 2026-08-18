import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch
from urllib.error import URLError

import rabbitmq_worker


class FakeMethod:
    def __init__(self, delivery_tag: int) -> None:
        self.delivery_tag = delivery_tag


class FakeChannel:
    def __init__(self) -> None:
        self.exchange_declare_calls: list[dict] = []
        self.queue_declare_calls: list[dict] = []
        self.queue_bind_calls: list[dict] = []
        self.acked_tags: list[int] = []
        self.nacked_tags: list[tuple[int, bool]] = []
        self.published_messages: list[dict] = []

    def exchange_declare(self, *, exchange: str, exchange_type: str, durable: bool) -> None:
        self.exchange_declare_calls.append(
            {
                "exchange": exchange,
                "exchange_type": exchange_type,
                "durable": durable,
            }
        )

    def queue_declare(
        self,
        *,
        queue: str,
        durable: bool,
        exclusive: bool = False,
        auto_delete: bool = False,
        arguments: dict | None = None,
    ) -> None:
        self.queue_declare_calls.append(
            {
                "queue": queue,
                "durable": durable,
                "exclusive": exclusive,
                "auto_delete": auto_delete,
                "arguments": arguments,
            }
        )

    def queue_bind(self, *, queue: str, exchange: str, routing_key: str) -> None:
        self.queue_bind_calls.append(
            {
                "queue": queue,
                "exchange": exchange,
                "routing_key": routing_key,
            }
        )

    def basic_ack(self, *, delivery_tag: int) -> None:
        self.acked_tags.append(delivery_tag)

    def basic_nack(self, *, delivery_tag: int, requeue: bool) -> None:
        self.nacked_tags.append((delivery_tag, requeue))

    def basic_publish(self, *, exchange: str, routing_key: str, body: bytes, properties: object | None = None) -> None:
        self.published_messages.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
            }
        )


class FakeProperties:
    def __init__(self, headers: dict | None = None) -> None:
        self.headers = headers or {}


class FakeReceiptAuditService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def evaluate(self, receipt_code: str, ocr_sample_path=None) -> dict:
        self.calls.append((receipt_code, ocr_sample_path))
        return {
            "receiptCode": receipt_code,
            "decisionOutput": {
                "checkStatus": "passed",
                "message": "ok",
            },
        }


class FakeReceiptProcessService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def process_receipt(self, receipt_code: str, ocr_sample_path=None) -> dict:
        self.calls.append((receipt_code, ocr_sample_path))
        return {
            "receiptCode": receipt_code,
            "invoiceCount": 2,
            "invoiceResults": [
                {"decisionOutput": {"checkStatus": "passed", "message": "FID-001"}},
                {"decisionOutput": {"checkStatus": "warning", "message": "FID-002"}},
            ],
        }


class FakeTwoStageReceiptService:
    def __init__(self) -> None:
        self.prepare_calls: list[tuple[str, object]] = []
        self.process_prepared_calls: list[dict] = []

    def prepare_receipt(self, receipt_code: str, ocr_sample_path=None) -> dict:
        self.prepare_calls.append((receipt_code, ocr_sample_path))
        return {
            "receiptCode": receipt_code,
            "invoiceCount": 2,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {"receipt": {"code": receipt_code}},
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {"fid": "FID-002"},
                    "preparedInput": {"receipt": {"code": receipt_code}},
                },
            ],
        }

    def process_prepared_receipt(self, prepared_receipt: dict) -> dict:
        self.process_prepared_calls.append(prepared_receipt)
        return {
            "receiptCode": prepared_receipt["receiptCode"],
            "invoiceCount": prepared_receipt["invoiceCount"],
            "invoiceResults": [
                {"decisionOutput": {"checkStatus": "passed", "message": "FID-001"}},
                {"decisionOutput": {"checkStatus": "warning", "message": "FID-002"}},
            ],
        }


class FakeTransientFailureService:
    def prepare_receipt(self, receipt_code: str, ocr_sample_path=None) -> dict:
        return {
            "receiptCode": receipt_code,
            "invoiceCount": 1,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {"receipt": {"code": receipt_code}},
                }
            ],
        }

    def process_prepared_receipt(self, prepared_receipt: dict) -> dict:
        raise URLError(TimeoutError("timed out"))


class RabbitMQWorkerTests(unittest.TestCase):
    def test_resolve_audit_service_url_rejects_local_mock_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError) as context:
                rabbitmq_worker.resolve_audit_service_url(None)

        self.assertIn("不允许使用本地 mock", str(context.exception))

    def test_resolve_audit_service_url_uses_explicit_real_service_url(self) -> None:
        resolved = rabbitmq_worker.resolve_audit_service_url("https://service-uate-gw.ruijie.com.cn")

        self.assertEqual(resolved, "https://service-uate-gw.ruijie.com.cn")

    @patch("apps.workers.rabbitmq_worker.create_worker")
    def test_main_cli_requires_explicit_real_audit_service_url(self, mock_create_worker: MagicMock) -> None:
        with patch.dict("os.environ", {"RABBITMQ_URL": "amqp://guest:guest@example:5672/%2F"}, clear=True):
            with self.assertRaises(ValueError) as context:
                rabbitmq_worker.main_cli(["--queues", "audit"])

        self.assertIn("不允许使用本地 mock", str(context.exception))
        mock_create_worker.assert_not_called()

    @patch("apps.workers.rabbitmq_worker.create_worker")
    def test_main_cli_can_bypass_task_gate(self, mock_create_worker: MagicMock) -> None:
        fake_worker = MagicMock()
        mock_create_worker.return_value = fake_worker

        rabbitmq_worker.main_cli(
            [
                "--amqp-url",
                "amqp://guest:guest@example:5672/%2F",
                "--audit-service-url",
                "https://service.example",
                "--queues",
                "audit",
                "--bypass-task-gate",
            ]
        )

        self.assertTrue(mock_create_worker.call_args.kwargs["bypass_task_gate"])
        fake_worker.run_forever.assert_called_once_with()

    @patch("apps.workers.rabbitmq_worker.create_receipt_audit_service")
    def test_create_worker_enables_writeback_in_main_chain(self, mock_create_service) -> None:
        service = FakeTwoStageReceiptService()
        mock_create_service.return_value = service

        worker = rabbitmq_worker.create_worker(
            audit_service_url="https://service.example",
            graph_runtime_url="http://127.0.0.1:8090",
        )

        self.assertIs(worker._service, service)
        self.assertTrue(mock_create_service.call_args.kwargs["enable_writeback"])
        self.assertTrue(callable(worker._task_info_list_provider))
        self.assertTrue(callable(worker._task_status_update_provider))

    def test_default_settings_use_dedicated_ai_verification_queue(self) -> None:
        settings = rabbitmq_worker.RabbitMQSettings()

        self.assertEqual(settings.exchange_name, "audit_exchange")
        self.assertEqual(settings.queue_name, "audit_ai_verification_queue")
        self.assertEqual(settings.routing_key, "audit_ai_verification_routing_key")

    def test_parse_receipt_code_accepts_plain_text_and_json_payload(self) -> None:
        self.assertEqual(rabbitmq_worker.parse_receipt_code(b"REC-001"), "REC-001")
        self.assertEqual(
            rabbitmq_worker.parse_receipt_code(b'{"receiptCode": "REC-JSON-001"}'),
            "REC-JSON-001",
        )
        self.assertEqual(
            rabbitmq_worker.parse_receipt_code(b'{"instanceCode": "REC-INSTANCE-001"}'),
            "REC-INSTANCE-001",
        )

    def test_declare_topology_creates_delay_queue_with_dead_letter_args(self) -> None:
        channel = FakeChannel()
        settings = rabbitmq_worker.RabbitMQSettings(delay_time_millis=120000)

        rabbitmq_worker.declare_topology(channel, settings)

        self.assertEqual(
            channel.exchange_declare_calls,
            [
                {
                    "exchange": settings.exchange_name,
                    "exchange_type": "direct",
                    "durable": True,
                }
            ],
        )
        self.assertIn(
            {
                "queue": settings.delay_queue_name,
                "durable": True,
                "exclusive": False,
                "auto_delete": False,
                "arguments": {
                    "x-dead-letter-exchange": settings.exchange_name,
                    "x-dead-letter-routing-key": settings.delay_process_routing_key,
                    "x-message-ttl": 120000,
                },
            },
            channel.queue_declare_calls,
        )
        self.assertIn(
            {
                "queue": settings.delay_process_queue_name,
                "exchange": settings.exchange_name,
                "routing_key": settings.delay_process_routing_key,
            },
            channel.queue_bind_calls,
        )
        self.assertIn(
            {
                "queue": settings.delay_queue_name,
                "exchange": settings.exchange_name,
                "routing_key": settings.delay_routing_key,
            },
            channel.queue_bind_calls,
        )

    def test_handle_delivery_executes_receipt_audit_and_acknowledges_message(self) -> None:
        service = FakeReceiptAuditService()
        worker = rabbitmq_worker.ReceiptAuditWorker(service=service)
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(7), None, b'{"receiptCode": "REC-HANDLER-001"}')

        self.assertEqual(service.calls, [("REC-HANDLER-001", None)])
        self.assertEqual(channel.acked_tags, [7])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_prefers_process_receipt_when_service_supports_it(self) -> None:
        service = FakeReceiptProcessService()
        worker = rabbitmq_worker.ReceiptAuditWorker(service=service)
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(9), None, b'{"receiptCode": "REC-PROCESS-001"}')

        self.assertEqual(service.calls, [("REC-PROCESS-001", None)])
        self.assertEqual(channel.acked_tags, [9])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_prefers_two_stage_prepare_then_process_when_supported(self) -> None:
        service = FakeTwoStageReceiptService()
        worker = rabbitmq_worker.ReceiptAuditWorker(service=service)
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(11), None, b'{"receiptCode": "REC-TWO-STAGE-001"}')

        self.assertEqual(service.prepare_calls, [("REC-TWO-STAGE-001", None)])
        self.assertEqual(len(service.process_prepared_calls), 1)
        self.assertEqual(service.process_prepared_calls[0]["receiptCode"], "REC-TWO-STAGE-001")
        self.assertEqual(service.process_prepared_calls[0]["invoiceCount"], 2)
        self.assertEqual(channel.acked_tags, [11])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_acks_and_skips_when_no_pending_ruijie_task_matches(self) -> None:
        service = FakeTwoStageReceiptService()
        task_info_list_provider = MagicMock(
            return_value=[
                {
                    "miInstanceCode": "REC-SKIP-001",
                    "anaStatus": 2,
                    "systemIdentifier": 4,
                }
            ]
        )
        task_status_update_provider = MagicMock()
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_info_list_provider=task_info_list_provider,
            task_status_update_provider=task_status_update_provider,
        )
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(13), None, b'{"receiptCode": "REC-SKIP-001"}')

        task_info_list_provider.assert_called_once_with("REC-SKIP-001")
        task_status_update_provider.assert_not_called()
        self.assertEqual(service.prepare_calls, [])
        self.assertEqual(service.process_prepared_calls, [])
        self.assertEqual(channel.acked_tags, [13])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_acks_and_stops_when_task_status_update_returns_business_failure(self) -> None:
        service = FakeTwoStageReceiptService()
        task_info_list_provider = MagicMock(
            return_value=[
                {
                    "miInstanceCode": "REC-STATUS-FAIL-001",
                    "anaStatus": 0,
                    "systemIdentifier": 4,
                }
            ]
        )
        task_status_update_provider = MagicMock(side_effect=ValueError("status update failed"))
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_info_list_provider=task_info_list_provider,
            task_status_update_provider=task_status_update_provider,
        )
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(14), None, b'{"receiptCode": "REC-STATUS-FAIL-001"}')

        task_info_list_provider.assert_called_once_with("REC-STATUS-FAIL-001")
        task_status_update_provider.assert_called_once_with(
            "REC-STATUS-FAIL-001",
            new_status=1,
            system_identifier=4,
        )
        self.assertEqual(service.prepare_calls, [])
        self.assertEqual(service.process_prepared_calls, [])
        self.assertEqual(channel.acked_tags, [14])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_acks_and_stops_when_task_status_update_returns_false_data(self) -> None:
        service = FakeTwoStageReceiptService()
        task_info_list_provider = MagicMock(
            return_value=[
                {
                    "miInstanceCode": "REC-STATUS-FALSE-001",
                    "anaStatus": 0,
                    "systemIdentifier": 4,
                }
            ]
        )
        task_status_update_provider = MagicMock(return_value={"code": 0, "message": "success", "data": False})
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_info_list_provider=task_info_list_provider,
            task_status_update_provider=task_status_update_provider,
        )
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(141), None, b'{"receiptCode": "REC-STATUS-FALSE-001"}')

        task_info_list_provider.assert_called_once_with("REC-STATUS-FALSE-001")
        task_status_update_provider.assert_called_once_with(
            "REC-STATUS-FALSE-001",
            new_status=1,
            system_identifier=4,
        )
        self.assertEqual(service.prepare_calls, [])
        self.assertEqual(service.process_prepared_calls, [])
        self.assertEqual(channel.acked_tags, [141])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_acks_and_stops_when_task_status_update_raises_upstream_error(self) -> None:
        service = FakeTwoStageReceiptService()
        task_info_list_provider = MagicMock(
            return_value=[
                {
                    "miInstanceCode": "REC-STATUS-ERROR-001",
                    "anaStatus": 0,
                    "systemIdentifier": 4,
                }
            ]
        )
        task_status_update_provider = MagicMock(side_effect=RuntimeError("task status endpoint unavailable"))
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_info_list_provider=task_info_list_provider,
            task_status_update_provider=task_status_update_provider,
        )
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(142), None, b'{"receiptCode": "REC-STATUS-ERROR-001"}')

        task_info_list_provider.assert_called_once_with("REC-STATUS-ERROR-001")
        task_status_update_provider.assert_called_once_with(
            "REC-STATUS-ERROR-001",
            new_status=1,
            system_identifier=4,
        )
        self.assertEqual(service.prepare_calls, [])
        self.assertEqual(service.process_prepared_calls, [])
        self.assertEqual(channel.acked_tags, [142])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_rejects_when_task_lookup_raises_upstream_error(self) -> None:
        service = FakeTwoStageReceiptService()
        task_info_list_provider = MagicMock(side_effect=RuntimeError("task lookup failed"))
        task_status_update_provider = MagicMock()
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_info_list_provider=task_info_list_provider,
            task_status_update_provider=task_status_update_provider,
        )
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(15), None, b'{"receiptCode": "REC-TASK-ERROR-001"}')

        task_info_list_provider.assert_called_once_with("REC-TASK-ERROR-001")
        task_status_update_provider.assert_not_called()
        self.assertEqual(service.prepare_calls, [])
        self.assertEqual(service.process_prepared_calls, [])
        self.assertEqual(channel.acked_tags, [])
        self.assertEqual(channel.nacked_tags, [(15, False)])

    def test_handle_delivery_runs_pipeline_after_claiming_pending_ruijie_task(self) -> None:
        service = FakeTwoStageReceiptService()
        task_info_list_provider = MagicMock(
            return_value=[
                {
                    "miInstanceCode": "REC-CLAIM-001",
                    "anaStatus": 0,
                    "systemIdentifier": 4,
                }
            ]
        )
        task_status_update_provider = MagicMock(return_value={"code": 0, "message": "success", "data": True})
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_info_list_provider=task_info_list_provider,
            task_status_update_provider=task_status_update_provider,
        )
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(16), None, b'{"receiptCode": "REC-CLAIM-001"}')

        task_info_list_provider.assert_called_once_with("REC-CLAIM-001")
        task_status_update_provider.assert_called_once_with(
            "REC-CLAIM-001",
            new_status=1,
            system_identifier=4,
        )
        self.assertEqual(service.prepare_calls, [("REC-CLAIM-001", None)])
        self.assertEqual(len(service.process_prepared_calls), 1)
        self.assertEqual(channel.acked_tags, [16])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_runs_pipeline_when_any_task_row_matches(self) -> None:
        service = FakeTwoStageReceiptService()
        task_info_list_provider = MagicMock(
            return_value=[
                {
                    "miInstanceCode": "REC-MULTI-001",
                    "anaStatus": 2,
                    "systemIdentifier": 4,
                },
                {
                    "miInstanceCode": "REC-MULTI-001",
                    "anaStatus": 0,
                    "systemIdentifier": 4,
                },
            ]
        )
        task_status_update_provider = MagicMock(return_value={"code": 0, "message": "success", "data": True})
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_info_list_provider=task_info_list_provider,
            task_status_update_provider=task_status_update_provider,
        )
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(17), None, b'{"receiptCode": "REC-MULTI-001"}')

        task_info_list_provider.assert_called_once_with("REC-MULTI-001")
        task_status_update_provider.assert_called_once_with(
            "REC-MULTI-001",
            new_status=1,
            system_identifier=4,
        )
        self.assertEqual(service.prepare_calls, [("REC-MULTI-001", None)])
        self.assertEqual(len(service.process_prepared_calls), 1)
        self.assertEqual(channel.acked_tags, [17])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_can_bypass_task_gate_and_run_pipeline(self) -> None:
        service = FakeTwoStageReceiptService()
        task_info_list_provider = MagicMock(return_value=[])
        task_status_update_provider = MagicMock()
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_info_list_provider=task_info_list_provider,
            task_status_update_provider=task_status_update_provider,
            bypass_task_gate=True,
        )
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(171), None, b'{"receiptCode": "REC-BYPASS-001"}')

        task_info_list_provider.assert_not_called()
        task_status_update_provider.assert_not_called()
        self.assertEqual(service.prepare_calls, [("REC-BYPASS-001", None)])
        self.assertEqual(len(service.process_prepared_calls), 1)
        self.assertEqual(channel.acked_tags, [171])
        self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_can_export_prepared_receipt_file(self) -> None:
        service = FakeTwoStageReceiptService()
        channel = FakeChannel()

        with tempfile.TemporaryDirectory() as temp_dir:
            worker = rabbitmq_worker.ReceiptAuditWorker(
                service=service,
                prepared_output_dir=temp_dir,
            )

            worker.handle_delivery(channel, FakeMethod(12), None, b'{"receiptCode": "REC-EXPORT-001"}')

            output_file = Path(temp_dir) / "REC-EXPORT-001.prepared-receipt.json"
            self.assertTrue(output_file.exists())
            payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["receiptCode"], "REC-EXPORT-001")
            self.assertEqual(payload["invoiceCount"], 2)
            self.assertEqual(channel.acked_tags, [12])
            self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_exports_prepared_receipt_with_unserializable_profile(self) -> None:
        """回归测试：prepared_receipt 含 ExpenseProfile 等不可序列化对象时，
        导出不应崩溃，且 process_prepared_receipt 仍应正常执行。"""
        from expense_audit_orchestrator.profiles import ExpenseProfile

        class FakeServiceWithProfile:
            def __init__(self) -> None:
                self.process_prepared_calls: list[dict] = []

            def prepare_receipt(self, receipt_code: str, ocr_sample_path=None) -> dict:
                return {
                    "receiptCode": receipt_code,
                    "invoiceCount": 1,
                    "invoicePreparations": [
                        {
                            "invoiceKey": "FID-001",
                            "invoiceFile": {"fid": "FID-001"},
                            "preparedInput": {"receipt": {"code": receipt_code}},
                        }
                    ],
                    # 模拟动态路由场景：resolvedProfile 是 ExpenseProfile 对象
                    "resolvedProfile": ExpenseProfile(name="telecom"),
                }

            def process_prepared_receipt(self, prepared_receipt: dict) -> dict:
                self.process_prepared_calls.append(prepared_receipt)
                return {
                    "receiptCode": prepared_receipt["receiptCode"],
                    "invoiceCount": 1,
                    "invoiceResults": [
                        {"decisionOutput": {"checkStatus": "passed", "message": "FID-001"}},
                    ],
                }

        service = FakeServiceWithProfile()
        channel = FakeChannel()

        with tempfile.TemporaryDirectory() as temp_dir:
            worker = rabbitmq_worker.ReceiptAuditWorker(
                service=service,
                prepared_output_dir=temp_dir,
            )

            worker.handle_delivery(
                channel, FakeMethod(15), None, b'{"receiptCode": "REC-PROFILE-001"}'
            )

            # 导出文件应成功生成（ExpenseProfile 被转成可读元数据）
            output_file = Path(temp_dir) / "REC-PROFILE-001.prepared-receipt.json"
            self.assertTrue(output_file.exists())
            payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["receiptCode"], "REC-PROFILE-001")
            # ExpenseProfile 应被序列化为含 name 的元数据，而非整个对象
            self.assertEqual(payload["resolvedProfile"]["name"], "telecom")
            self.assertEqual(payload["resolvedProfile"]["__dataclass__"], "ExpenseProfile")
            # process_prepared_receipt 仍应被调用且收到原始对象（非元数据）
            self.assertEqual(len(service.process_prepared_calls), 1)
            self.assertEqual(
                service.process_prepared_calls[0]["resolvedProfile"].name, "telecom"
            )
            # 消息应被 ack（不应因导出问题被 nack）
            self.assertEqual(channel.acked_tags, [15])
            self.assertEqual(channel.nacked_tags, [])

    def test_handle_delivery_rejects_invalid_payload_without_requeue(self) -> None:
        service = FakeReceiptAuditService()
        worker = rabbitmq_worker.ReceiptAuditWorker(service=service)
        channel = FakeChannel()

        worker.handle_delivery(channel, FakeMethod(8), None, b'{}')

        self.assertEqual(service.calls, [])
        self.assertEqual(channel.acked_tags, [])
        self.assertEqual(channel.nacked_tags, [(8, False)])

    def test_handle_delivery_schedules_retry_to_delay_queue_for_transient_error(self) -> None:
        service = FakeTransientFailureService()
        worker = rabbitmq_worker.ReceiptAuditWorker(service=service, max_retry_count=2)
        channel = FakeChannel()

        worker.handle_delivery(
            channel,
            FakeMethod(18),
            FakeProperties(headers={}),
            b'{"receiptCode": "REC-RETRY-TO-DELAY-001"}',
        )

        self.assertEqual(channel.acked_tags, [18])
        self.assertEqual(channel.nacked_tags, [])
        self.assertEqual(len(channel.published_messages), 1)
        self.assertEqual(channel.published_messages[0]["routing_key"], worker._settings.delay_routing_key)

    def test_handle_delivery_marks_failed_when_retry_exhausted(self) -> None:
        service = FakeTransientFailureService()
        task_status_update_provider = MagicMock(return_value={"code": 0, "message": "success", "data": True})
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_status_update_provider=task_status_update_provider,
            max_retry_count=2,
            failed_task_status=2,
        )
        channel = FakeChannel()

        worker.handle_delivery(
            channel,
            FakeMethod(19),
            FakeProperties(headers={rabbitmq_worker.RETRY_COUNT_HEADER: 2}),
            b'{"receiptCode": "REC-RETRY-EXHAUSTED-001"}',
        )

        self.assertEqual(channel.acked_tags, [])
        self.assertEqual(channel.nacked_tags, [(19, False)])
        task_status_update_provider.assert_called_once()
        called_kwargs = task_status_update_provider.call_args.kwargs
        self.assertEqual(called_kwargs["new_status"], 2)
        self.assertEqual(called_kwargs["system_identifier"], 4)

    def test_create_blocking_connection_reports_actionable_error_when_broker_unreachable(self) -> None:
        settings = rabbitmq_worker.RabbitMQSettings(amqp_url="amqp://guest:guest@127.0.0.1:5672/%2F")

        class FakeAMQPConnectionError(Exception):
            pass

        class FakePika:
            class exceptions:
                AMQPConnectionError = FakeAMQPConnectionError

            @staticmethod
            def URLParameters(url: str) -> str:
                return url

            @staticmethod
            def BlockingConnection(_parameters: object) -> None:
                raise FakeAMQPConnectionError("boom")

        with patch.object(rabbitmq_worker, "pika", FakePika):
            with self.assertRaises(RuntimeError) as context:
                rabbitmq_worker.create_blocking_connection(settings)

        self.assertIn("无法连接 RabbitMQ", str(context.exception))
        self.assertIn(settings.amqp_url, str(context.exception))
        self.assertIn("5672", str(context.exception))


class FakeUnknownExpenseTypeService:
    """模拟 prepare_receipt 抛 UnknownExpenseTypeError 的 service。"""

    def prepare_receipt(self, receipt_code: str, ocr_sample_path=None) -> dict:
        from expense_audit_orchestrator.profiles import UnknownExpenseTypeError

        raise UnknownExpenseTypeError(
            "eiCode 'EI999' is not mapped to any expense profile"
        )

    def process_prepared_receipt(self, prepared_receipt: dict) -> dict:
        raise AssertionError("process_prepared_receipt should not be called")


class UnknownExpenseTypeRoutingTests(unittest.TestCase):
    """未知 eiCode（未映射的费用类型）处理路径测试。"""

    def test_handle_delivery_marks_failed_for_unknown_expense_type(self) -> None:
        service = FakeUnknownExpenseTypeService()
        task_status_update_provider = MagicMock(return_value={"code": 0, "message": "success", "data": True})
        worker = rabbitmq_worker.ReceiptAuditWorker(
            service=service,
            task_status_update_provider=task_status_update_provider,
            max_retry_count=2,
            failed_task_status=2,
        )
        channel = FakeChannel()

        worker.handle_delivery(
            channel,
            FakeMethod(21),
            FakeProperties(headers={}),
            b'{"receiptCode": "REC-UNKNOWN-EITYPE-001"}',
        )

        # 应标记失败并 nack（不重试、不重入队）
        self.assertEqual(channel.acked_tags, [])
        self.assertEqual(channel.nacked_tags, [(21, False)])
        task_status_update_provider.assert_called_once()
        called_kwargs = task_status_update_provider.call_args.kwargs
        self.assertEqual(called_kwargs["new_status"], 2)
        self.assertEqual(called_kwargs["system_identifier"], 4)

    @patch("apps.workers.rabbitmq_worker.create_worker")
    def test_main_cli_enables_dynamic_routing_with_ei_code_map_path(self, mock_create_worker: MagicMock) -> None:
        import json
        import tempfile

        fake_worker = MagicMock()
        mock_create_worker.return_value = fake_worker

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump({"EI001": "telecom"}, fh)
            map_path = fh.name

        try:
            rabbitmq_worker.main_cli(
                [
                    "--amqp-url",
                    "amqp://guest:guest@example:5672/%2F",
                    "--audit-service-url",
                    "https://service.example",
                    "--queues",
                    "audit",
                    "--ei-code-map-path",
                    map_path,
                ]
            )
        finally:
            import os

            os.unlink(map_path)

        # create_worker 应收到非 None 的 profile_resolver
        self.assertIsNotNone(mock_create_worker.call_args.kwargs["profile_resolver"])
        fake_worker.run_forever.assert_called_once_with()

    @patch("apps.workers.rabbitmq_worker.create_worker")
    def test_main_cli_rejects_ei_code_map_path_with_profile(self, mock_create_worker: MagicMock) -> None:
        import json
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump({"EI001": "telecom"}, fh)
            map_path = fh.name

        try:
            with self.assertRaises(SystemExit):
                rabbitmq_worker.main_cli(
                    [
                        "--amqp-url",
                        "amqp://guest:guest@example:5672/%2F",
                        "--audit-service-url",
                        "https://service.example",
                        "--queues",
                        "audit",
                        "--ei-code-map-path",
                        map_path,
                        "--profile",
                        "travel",
                    ]
                )
        finally:
            import os

            os.unlink(map_path)

        mock_create_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()