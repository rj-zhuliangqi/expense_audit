import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import execute_graph


class FakeEvaluationService:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, object]] = []
        self.prepare_calls: list[tuple[str, object]] = []

    def evaluate(self, receipt_code: str, ocr_sample_path=None) -> dict:
        self.calls.append((receipt_code, ocr_sample_path))
        return self.response

    def prepare_input(self, receipt_code: str, ocr_sample_path=None) -> dict:
        self.prepare_calls.append((receipt_code, ocr_sample_path))
        return self.response


class FakeDecisionEngine:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"checkStatus": "passed", "message": "ok"}
        self.received_input = None

    def evaluate(self, rule_input: dict) -> dict:
        self.received_input = rule_input
        return {"result": self.result}


class FakeGraphRuntimeClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def evaluate(self, *, prepared_input: dict, graph_path=None, graph_content=None) -> dict:
        self.calls.append(
            {
                "prepared_input": prepared_input,
                "graph_path": graph_path,
                "graph_content": graph_content,
            }
        )
        return self.response


class ExecuteGraphCliTests(unittest.TestCase):
    @patch("execute_graph.create_receipt_audit_service")
    def test_main_cli_runs_selected_graph_and_exports_prepared_input(self, mock_create_service) -> None:
        prepared_input = {
            "receipt": {
                "code": "REC-EXEC",
                "filePath": "/tmp/invoice.pdf",
            },
            "context": {"receiptCode": "REC-EXEC"},
            "serviceData": {"auditInfo": {"instanceCode": "REC-EXEC"}},
        }
        fake_service = FakeEvaluationService(
            {
                "receiptCode": "REC-EXEC",
                "checkStatus": "passed",
                "message": "ok",
                "decisionOutput": {
                    "checkStatus": "passed",
                    "message": "ok",
                },
                "preparedInput": prepared_input,
            }
        )
        mock_create_service.return_value = fake_service

        @contextmanager
        def fake_ensure_mock_audit_service_url():
            yield execute_graph.DEFAULT_AUDIT_SERVICE_URL

        with patch(
            "execute_graph.ensure_mock_audit_service_url",
            fake_ensure_mock_audit_service_url,
            create=True,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                output_path = Path(temp_dir) / "prepared-input.json"

                exit_code = execute_graph.main_cli(
                    [
                        "--receipt-code",
                        "REC-EXEC",
                        "--graph-path",
                        "graph-llm-latest.json",
                        "--prepared-output-path",
                        str(output_path),
                    ]
                )

                self.assertTrue(output_path.exists())
                self.assertEqual(
                    json.loads(output_path.read_text(encoding="utf-8")),
                    prepared_input,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(fake_service.calls), 1)
        self.assertEqual(fake_service.calls[0][0], "REC-EXEC")
        self.assertEqual(Path(fake_service.calls[0][1]), execute_graph.DEFAULT_OCR_PATH)
        self.assertEqual(
            Path(mock_create_service.call_args.kwargs["graph_path"]),
            Path("graph-llm-latest.json"),
        )
        self.assertEqual(
            mock_create_service.call_args.kwargs["audit_service_url"],
            execute_graph.DEFAULT_AUDIT_SERVICE_URL,
        )

    @patch("execute_graph.create_receipt_audit_service")
    def test_main_cli_can_export_prepared_input_without_runtime_execution(self, mock_create_service) -> None:
        prepared_input = {
            "receipt": {
                "code": "REC-EXPORT-ONLY",
                "filePath": "/tmp/invoice.pdf",
            },
            "context": {"receiptCode": "REC-EXPORT-ONLY"},
            "serviceData": {"auditInfo": {"instanceCode": "REC-EXPORT-ONLY"}},
        }
        fake_service = FakeEvaluationService(prepared_input)
        mock_create_service.return_value = fake_service

        @contextmanager
        def fake_ensure_mock_audit_service_url():
            yield execute_graph.DEFAULT_AUDIT_SERVICE_URL

        with patch(
            "execute_graph.ensure_mock_audit_service_url",
            fake_ensure_mock_audit_service_url,
            create=True,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                output_path = Path(temp_dir) / "prepared-input.json"

                exit_code = execute_graph.main_cli(
                    [
                        "--receipt-code",
                        "REC-EXPORT-ONLY",
                        "--prepare-only",
                        "--prepared-output-path",
                        str(output_path),
                    ]
                )

                self.assertEqual(
                    json.loads(output_path.read_text(encoding="utf-8")),
                    prepared_input,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(fake_service.prepare_calls), 1)
        self.assertEqual(fake_service.prepare_calls[0][0], "REC-EXPORT-ONLY")
        self.assertEqual(Path(fake_service.prepare_calls[0][1]), execute_graph.DEFAULT_OCR_PATH)
        self.assertEqual(fake_service.calls, [])

    @patch("execute_graph.ensure_mock_audit_service_url")
    @patch("execute_graph.create_receipt_audit_service")
    @patch("execute_graph.create_graph_runtime_client")
    def test_main_cli_can_execute_graph_from_prepared_input_json(
        self,
        mock_create_graph_runtime_client,
        mock_create_service,
        mock_ensure_mock_service,
    ) -> None:
        prepared_input = {
            "invoiceType": "26",
            "receipt": {
                "code": "REC-PREP",
                "filePath": "/tmp/invoice.pdf",
            },
            "serviceData": {
                "telecom_list": [["电信", "深圳"]],
            },
        }
        fake_runtime_client = FakeGraphRuntimeClient(
            {
                "checkStatus": "passed",
                "message": "ok",
                "decisionOutput": {
                    "checkStatus": "passed",
                    "message": "ok",
                },
            }
        )
        mock_create_graph_runtime_client.return_value = fake_runtime_client

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "prepared-input.json"
            input_path.write_text(
                json.dumps(prepared_input, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            exit_code = execute_graph.main_cli(
                [
                    "--graph-path",
                    "graph-llm-latest.json",
                    "--prepared-input-path",
                    str(input_path),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_runtime_client.calls[0]["prepared_input"], prepared_input)
        self.assertEqual(fake_runtime_client.calls[0]["graph_path"], "graph-llm-latest.json")
        mock_create_graph_runtime_client.assert_called_once_with(None)
        mock_create_service.assert_not_called()
        mock_ensure_mock_service.assert_not_called()

    @patch("execute_graph.ensure_mock_audit_service_url")
    @patch("execute_graph.create_receipt_audit_service")
    @patch("execute_graph.create_graph_runtime_client")
    def test_main_cli_can_execute_graph_from_wrapped_prepared_receipt_json(
        self,
        mock_create_graph_runtime_client,
        mock_create_service,
        mock_ensure_mock_service,
    ) -> None:
        prepared_input = {
            "invoiceType": "26",
            "invoiceNo": "26117000000523959007",
            "receipt": {
                "code": "rjw260615000002",
            },
            "serviceData": {
                "invoiceUsageHistory": [],
                "currentInvoiceInfo": {"aiiid": "AIIID-001"},
            },
        }
        fake_runtime_client = FakeGraphRuntimeClient(
            {
                "checkStatus": "passed",
                "message": "ok",
                "decisionOutput": {
                    "checkStatus": "passed",
                    "message": "ok",
                },
            }
        )
        mock_create_graph_runtime_client.return_value = fake_runtime_client

        wrapped_payload = {
            "receiptCode": "rjw260615000002",
            "invoicePreparations": [
                {
                    "preparedInput": prepared_input,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "prepared-receipt.json"
            input_path.write_text(
                json.dumps(wrapped_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            exit_code = execute_graph.main_cli(
                [
                    "--graph-path",
                    "graph-latest-0623-1202.json",
                    "--prepared-input-path",
                    str(input_path),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_runtime_client.calls[0]["prepared_input"], prepared_input)
        self.assertEqual(fake_runtime_client.calls[0]["graph_path"], "graph-latest-0623-1202.json")
        mock_create_graph_runtime_client.assert_called_once_with(None)
        mock_create_service.assert_not_called()
        mock_ensure_mock_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()