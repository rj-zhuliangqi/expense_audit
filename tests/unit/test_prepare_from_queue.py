import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apps.cli.prepare_from_queue as prepare_from_queue
from expense_audit_orchestrator.profiles import get_profile
from expense_audit_orchestrator.profiles.telecom.writeback import telecom_compliance_rule
from expense_audit_orchestrator.writeback import assemble_result_audit_info


class FakeMethod:
    def __init__(self, delivery_tag: int, routing_key: str = "audit_routing_key") -> None:
        self.delivery_tag = delivery_tag
        self.routing_key = routing_key


class FakeChannel:
    def __init__(self, method: FakeMethod | None, body: bytes) -> None:
        self._method = method
        self._body = body
        self.get_calls: list[dict[str, object]] = []
        self.acked_tags: list[int] = []
        self.nacked_tags: list[tuple[int, bool]] = []

    def basic_get(self, *, queue: str, auto_ack: bool) -> tuple[FakeMethod | None, object, bytes]:
        self.get_calls.append({"queue": queue, "auto_ack": auto_ack})
        return self._method, None, self._body

    def basic_ack(self, *, delivery_tag: int) -> None:
        self.acked_tags.append(delivery_tag)

    def basic_nack(self, *, delivery_tag: int, requeue: bool) -> None:
        self.nacked_tags.append((delivery_tag, requeue))


class FakeConnection:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel
        self.is_open = True
        self.closed = False

    def channel(self) -> FakeChannel:
        return self._channel

    def close(self) -> None:
        self.is_open = False
        self.closed = True


class FakePrepareService:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, object]] = []

    def prepare_receipt(self, receipt_code: str, ocr_sample_path=None) -> dict:
        self.calls.append((receipt_code, ocr_sample_path))
        return self.response


class FakeFullProcessService(FakePrepareService):
    def __init__(self, prepared_response: dict, processed_response: dict) -> None:
        super().__init__(prepared_response)
        self.processed_response = processed_response
        self.process_calls: list[dict] = []

    def process_prepared_receipt(self, prepared_receipt: dict) -> dict:
        self.process_calls.append(prepared_receipt)
        return self.processed_response


class PrepareFromQueueCliTests(unittest.TestCase):
    @patch("apps.cli.prepare_from_queue.create_receipt_audit_service")
    @patch("apps.cli.prepare_from_queue.create_blocking_connection")
    def test_main_cli_consumes_one_message_and_requeues_by_default(
        self,
        mock_create_blocking_connection,
        mock_create_service,
    ) -> None:
        response = {
            "receiptCode": "REC-QUEUE-001",
            "invoiceCount": 1,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {"receipt": {"code": "REC-QUEUE-001"}},
                }
            ],
        }
        service = FakePrepareService(response)
        mock_create_service.return_value = service

        channel = FakeChannel(FakeMethod(7), b'{"receiptCode": "REC-QUEUE-001"}')
        connection = FakeConnection(channel)
        mock_create_blocking_connection.return_value = connection

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "prepared-receipt.json"

            exit_code = prepare_from_queue.main_cli(
                [
                    "--amqp-url",
                    "amqp://guest:guest@example:5672/%2F",
                    "--prepared-output-path",
                    str(output_path),
                ]
            )

            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), response)

        self.assertEqual(exit_code, 0)
        self.assertEqual(service.calls[0][0], "REC-QUEUE-001")
        self.assertEqual(channel.acked_tags, [])
        self.assertEqual(channel.nacked_tags, [(7, True)])
        self.assertTrue(connection.closed)

    @patch("apps.cli.prepare_from_queue.create_receipt_audit_service")
    @patch("apps.cli.prepare_from_queue.create_blocking_connection")
    def test_main_cli_acks_message_when_requested(
        self,
        mock_create_blocking_connection,
        mock_create_service,
    ) -> None:
        response = {
            "receiptCode": "REC-QUEUE-ACK-001",
            "invoiceCount": 1,
            "invoicePreparations": [],
        }
        service = FakePrepareService(response)
        mock_create_service.return_value = service

        channel = FakeChannel(FakeMethod(9), b'REC-QUEUE-ACK-001')
        connection = FakeConnection(channel)
        mock_create_blocking_connection.return_value = connection

        exit_code = prepare_from_queue.main_cli(
            [
                "--amqp-url",
                "amqp://guest:guest@example:5672/%2F",
                "--ack-on-success",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service.calls[0][0], "REC-QUEUE-ACK-001")
        self.assertEqual(channel.acked_tags, [9])
        self.assertEqual(channel.nacked_tags, [])

    @patch("apps.cli.prepare_from_queue.create_receipt_audit_service")
    @patch("apps.cli.prepare_from_queue.create_blocking_connection")
    def test_main_cli_can_print_receipt_code_only_without_preparing(
        self,
        mock_create_blocking_connection,
        mock_create_service,
    ) -> None:
        channel = FakeChannel(FakeMethod(13), b'{"receiptCode": "REC-CODE-ONLY-001"}')
        connection = FakeConnection(channel)
        mock_create_blocking_connection.return_value = connection

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = prepare_from_queue.main_cli(
                [
                    "--amqp-url",
                    "amqp://guest:guest@example:5672/%2F",
                    "--receipt-code-only",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("REC-CODE-ONLY-001", stdout.getvalue())
        self.assertEqual(channel.acked_tags, [])
        self.assertEqual(channel.nacked_tags, [(13, True)])
        mock_create_service.assert_not_called()

    @patch("apps.cli.prepare_from_queue.create_receipt_audit_service")
    @patch("apps.cli.prepare_from_queue.create_blocking_connection")
    def test_main_cli_can_export_writeback_payload_without_real_callback(
        self,
        mock_create_blocking_connection,
        mock_create_service,
    ) -> None:
        prepared_receipt = {
            "receiptCode": "REC-WRITEBACK-EXPORT-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-WRITEBACK-EXPORT-001"},
                "auditInvoiceFiles": [
                    {
                        "afiid": "AFID-001",
                        "fid": "FID-001",
                        "fileName": "origin.pdf",
                        "aiid": "AIID-001",
                    }
                ],
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "afiid": "AFID-001",
                            "fid": "FID-001",
                            "fileName": "origin.pdf",
                            "aiid": "AIID-001",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "goodsName": "*电信服务*通信服务费、*电信服务*违约金",
                        "items": [
                            {"goodsName": "*电信服务*通信服务费"},
                            {"goodsName": "*电信服务*违约金"},
                        ],
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "truthCheckFieldMappings": {
                                "bill": [
                                    {
                                        "fieldName": "invoiceNo",
                                        "fieldLable": "发票号码",
                                        "belongTable": "bill",
                                        "status": True,
                                    }
                                ],
                                "item": [
                                    {
                                        "fieldName": "totalAmount",
                                        "fieldLable": "价税合计",
                                        "belongTable": "item",
                                        "status": True,
                                    }
                                ],
                            },
                            "currentInvoiceInfo": {"aiiid": "AIIID-001", "atcrid": "ATCRID-001"},
                            "currentAuditInvoiceFile": {
                                "afiid": "AFID-001",
                                "fid": "FID-001",
                                "fileName": "origin.pdf",
                                "aiid": "AIID-001",
                            },
                            "ocrEnvelope": {
                                "upload": {"fileDownUrl": "https://files.example/FID-001.pdf"},
                                "recognition": {
                                    "rawPayload": {
                                        "data": [
                                            {
                                                "invoiceNo": "INV-001",
                                                "totalAmount": 476.1,
                                                "items": [{"detailAmount": "888.8"}],
                                            }
                                        ]
                                    }
                                },
                                "status": {"code": "200", "message": "success", "finishedAt": "2026-06-16T12:00:00+00:00"},
                            },
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-WRITEBACK-EXPORT-001",
            "serviceData": prepared_receipt["serviceData"],
            "receiptContext": {"receiptCode": "REC-WRITEBACK-EXPORT-001"},
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "amount_result": {
                            "audit_content": "检查使用发票合计金额是否充足",
                            "audit_type": "general-rules",
                            "distinguish_content": "金额不足",
                            "distinguish_result": "REJECT",
                            "instance_code": "REC-WRITEBACK-EXPORT-001",
                            "invoice_file_id": "AFID-001",
                            "invoice_info_id": "AIIID-001",
                            "message": "金额不够",
                            "reason_code": "E31",
                        },
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }
        service = FakeFullProcessService(prepared_receipt, processed_receipt)
        mock_create_service.return_value = service

        channel = FakeChannel(FakeMethod(15), b'REC-WRITEBACK-EXPORT-001')
        connection = FakeConnection(channel)
        mock_create_blocking_connection.return_value = connection

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "writeback-payload.json"

            exit_code = prepare_from_queue.main_cli(
                [
                    "--amqp-url",
                    "amqp://guest:guest@example:5672/%2F",
                    "--writeback-output-path",
                    str(output_path),
                ]
            )

            telecom_profile = get_profile("telecom")
            expected_payload = assemble_result_audit_info(
                prepared_receipt,
                processed_receipt,
                compliance_rule=telecom_compliance_rule,
                audit_risk_catalog=telecom_profile.audit_risk_catalog,
                expense_profile=telecom_profile.name,
            )
            actual_payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [c["compliance"] for c in actual_payload["auditInvoiceInfoContents"]],
                [True, False],
            )
            self.assertTrue(actual_payload["auditTruthCheckLogs"][0]["atclid"])
            self.assertTrue(actual_payload["auditInvoiceFiles"][0]["aifid"])
            self.assertEqual(actual_payload["auditInvoiceInfos"][0]["atcrid"], "ATCRID-001")
            self.assertEqual(len(actual_payload["auditTruthCheckResultBills"]), 1)
            self.assertEqual(
                [row["code"] for row in actual_payload["auditTruthCheckResultBills"]],
                ["invoiceNo"],
            )
            self.assertEqual(len(actual_payload["auditTruthCheckResultItems"]), 1)
            self.assertEqual(len(actual_payload["auditTruthCheckResultItemCols"]), 1)
            self.assertTrue(actual_payload["auditTruthCheckResultBills"][0]["atcrbid"])
            self.assertTrue(actual_payload["auditTruthCheckResultItems"][0]["atcriid"])
            self.assertTrue(actual_payload["auditTruthCheckResultItemCols"][0]["atcricid"])
            self.assertTrue(all("code" not in item for item in actual_payload["auditTruthCheckResultItemCols"]))
            actual_payload["auditTruthCheckLogs"][0]["atclid"] = "DYNAMIC"
            actual_payload["auditInvoiceFiles"][0]["aifid"] = "DYNAMIC"
            expected_payload["auditTruthCheckLogs"][0]["atclid"] = "DYNAMIC"
            expected_payload["auditInvoiceFiles"][0]["aifid"] = "DYNAMIC"
            if actual_payload["auditInvoiceInfoContents"]:
                for actual_content, expected_content in zip(
                    actual_payload["auditInvoiceInfoContents"],
                    expected_payload["auditInvoiceInfoContents"],
                    strict=False,
                ):
                    self.assertTrue(actual_content["aiicid"])
                    actual_content["aiicid"] = "DYNAMIC"
                    expected_content["aiicid"] = "DYNAMIC"
            for actual_row, expected_row in zip(
                actual_payload["auditTruthCheckResultBills"],
                expected_payload["auditTruthCheckResultBills"],
                strict=False,
            ):
                actual_row["atcrbid"] = "DYNAMIC"
                expected_row["atcrbid"] = "DYNAMIC"
            for actual_row, expected_row in zip(
                actual_payload["auditTruthCheckResultItems"],
                expected_payload["auditTruthCheckResultItems"],
                strict=False,
            ):
                actual_row["atcriid"] = "DYNAMIC"
                expected_row["atcriid"] = "DYNAMIC"
            for actual_row, expected_row in zip(
                actual_payload["auditTruthCheckResultItemCols"],
                expected_payload["auditTruthCheckResultItemCols"],
                strict=False,
            ):
                actual_row["atcricid"] = "DYNAMIC"
                expected_row["atcricid"] = "DYNAMIC"
            self.assertEqual(actual_payload, expected_payload)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(service.process_calls), 1)
        self.assertEqual(channel.acked_tags, [])
        self.assertEqual(channel.nacked_tags, [(15, True)])


if __name__ == "__main__":
    unittest.main()