import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse
from urllib.request import Request

from fastapi.testclient import TestClient

import main
from expense_audit_orchestrator import bootstrap as orchestrator_bootstrap
from expense_audit_orchestrator.application import ReceiptAuditService
from expense_audit_orchestrator.bootstrap import create_receipt_audit_service as create_orchestrator_service
from expense_audit_orchestrator.core import ReceiptDataPreparer as OrchestratorReceiptDataPreparer
from expense_audit_orchestrator.profiles import ExpenseProfile
from graph_runtime.core import DEFAULT_GRAPH_PATH, load_decision, load_decision_from_content
from graph_runtime.api import create_app as create_graph_runtime_app
from graph_runtime.application import normalize_decision_output
from node_gateway.api import NODE_GATEWAY_LLM_EVALUATE_PATH, create_app as create_node_gateway_app


class FakeHttpResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeDecisionEngine:
    def __init__(self, result: dict | None = None) -> None:
        self._result = result or {"checkStatus": "passed", "message": "ok"}
        self.received_input = None

    def evaluate(self, rule_input: dict, options: dict | None = None) -> dict:
        self.received_input = rule_input
        self.received_options = options
        return {"result": self._result}


class FakeDataPreparer(OrchestratorReceiptDataPreparer):
    def __init__(self, prepared_input: dict) -> None:
        self.prepared_input = prepared_input
        self.calls: list[tuple[str, object]] = []

    def prepare(self, receipt_code: str, ocr_sample_path=main.DEFAULT_OCR_PATH) -> dict[str, Any]:
        self.calls.append((receipt_code, ocr_sample_path))
        return self.prepared_input


class FakeEvaluationService:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, object]] = []

    def evaluate(self, receipt_code: str, ocr_sample_path=None) -> dict:
        self.calls.append((receipt_code, ocr_sample_path))
        return self.response


class FakeGraphRuntimeClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict:
        self.calls.append(
            {
                "prepared_input": prepared_input,
                "graph_path": graph_path,
                "graph_content": graph_content,
            }
        )
        return self.response


class FakeSplitDataPreparer:
    def __init__(self, receipt_context: dict[str, Any], prepared_inputs_by_fid: dict[str, dict[str, Any]]) -> None:
        self.receipt_context = receipt_context
        self.prepared_inputs_by_fid = prepared_inputs_by_fid
        self.prepare_receipt_context_calls: list[str] = []
        self.prepare_invoice_input_calls: list[tuple[str, str, object]] = []

    def prepare_receipt_context(
        self,
        receipt_code: str,
        *,
        receipt_enrichers_override: object = None,
    ) -> dict[str, Any]:
        del receipt_enrichers_override  # mock 不关心 enricher 覆盖
        self.prepare_receipt_context_calls.append(receipt_code)
        return self.receipt_context

    def prepare_invoice_input(
        self,
        receipt_code: str,
        invoice_file: dict[str, Any],
        receipt_context: dict[str, Any],
        ocr_sample_path=main.DEFAULT_OCR_PATH,
    ) -> dict[str, Any]:
        del receipt_context
        fid = invoice_file["fid"]
        self.prepare_invoice_input_calls.append((receipt_code, fid, ocr_sample_path))
        return self.prepared_inputs_by_fid[fid]


MINIMAL_GRAPH_CONTENT = {
    "contentType": "application/vnd.gorules.decision",
    "nodes": [
        {
            "type": "inputNode",
            "content": {"schema": ""},
            "id": "input",
            "name": "request",
            "position": {"x": 0, "y": 0},
        },
        {
            "type": "outputNode",
            "content": {"schema": ""},
            "id": "output",
            "name": "response",
            "position": {"x": 200, "y": 0},
        },
    ],
    "edges": [
        {
            "id": "edge-1",
            "sourceId": "input",
            "targetId": "output",
            "type": "edge",
        }
    ],
}


def build_single_expression_graph(expression_value: str) -> dict[str, Any]:
    return {
        "contentType": "application/vnd.gorules.decision",
        "nodes": [
            {
                "type": "inputNode",
                "content": {"schema": ""},
                "id": "input",
                "name": "request",
                "position": {"x": 0, "y": 0},
            },
            {
                "type": "expressionNode",
                "content": {
                    "expressions": [
                        {
                            "id": "expr-1",
                            "key": "isWriteOff",
                            "value": expression_value,
                        }
                    ],
                    "passThrough": True,
                    "inputField": None,
                    "outputPath": None,
                    "executionMode": "single",
                },
                "id": "expr",
                "name": "expr",
                "position": {"x": 100, "y": 0},
            },
            {
                "type": "outputNode",
                "content": {"schema": ""},
                "id": "output",
                "name": "response",
                "position": {"x": 200, "y": 0},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "sourceId": "input",
                "targetId": "expr",
                "type": "edge",
            },
            {
                "id": "e2",
                "sourceId": "expr",
                "targetId": "output",
                "type": "edge",
            },
        ],
    }


class ReceiptPipelineTests(unittest.TestCase):
    def test_main_no_longer_exposes_runtime_startup_entrypoints(self) -> None:
        self.assertFalse(hasattr(main, "app"))
        self.assertFalse(hasattr(main, "create_app"))
        self.assertFalse(hasattr(main, "main_cli"))

    def test_main_no_longer_exposes_run_once_entrypoint(self) -> None:
        self.assertFalse(hasattr(main, "run_once"))

    def test_main_app_exposes_graph_runtime_endpoint_for_uploaded_graph_content(self) -> None:
        prepared_input = {
            "invoiceType": "2",
            "context": {"receiptCode": "REC-001"},
            "serviceData": {"auditInfo": {"instanceCode": "REC-001"}},
        }

        client = TestClient(create_graph_runtime_app())
        response = client.post(
            "/api/v1/graph-runtime/evaluations",
            json={
                "graphContent": MINIMAL_GRAPH_CONTENT,
                "preparedInput": prepared_input,
                "includePreparedInput": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checkStatus"], response.json()["decisionOutput"]["checkStatus"])
        self.assertIn("decisionOutput", response.json())
        self.assertEqual(response.json()["preparedInput"], prepared_input)

    def test_graph_runtime_app_does_not_expose_receipt_orchestration_route(self) -> None:
        client = TestClient(create_graph_runtime_app())

        response = client.post(
            "/api/v1/expense-audits/evaluations",
            json={
                "receiptCode": "REC-ONLY",
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_graph_runtime_endpoint_rejects_receipt_code_mode(self) -> None:
        client = TestClient(create_graph_runtime_app())

        response = client.post(
            "/api/v1/graph-runtime/evaluations",
            json={
                "graphContent": MINIMAL_GRAPH_CONTENT,
                "receiptCode": "REC-NOT-ALLOWED",
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_graph_runtime_writeoff_expression_tolerates_missing_or_null_usage_history(self) -> None:
        graph_content = json.loads(Path(DEFAULT_GRAPH_PATH).read_text(encoding="utf-8"))
        expression_value = None
        for node in graph_content.get("nodes", []):
            if not isinstance(node, dict):
                continue
            content = node.get("content")
            if not isinstance(content, dict):
                continue
            for expression in content.get("expressions", []):
                if isinstance(expression, dict) and expression.get("key") == "isWriteOff":
                    expression_value = expression.get("value")
                    break
            if expression_value is not None:
                break

        self.assertIsNotNone(expression_value)

        client = TestClient(create_graph_runtime_app())
        graph = build_single_expression_graph(expression_value)

        for prepared_input in (
            {"invoiceNo": "INV-001", "serviceData": {}},
            {"invoiceNo": "INV-001", "serviceData": {"invoiceUsageHistory": None}},
        ):
            with self.subTest(prepared_input=prepared_input):
                response = client.post(
                    "/api/v1/graph-runtime/evaluations",
                    json={
                        "graphContent": graph,
                        "preparedInput": prepared_input,
                        "includePreparedInput": True,
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["decisionOutput"]["isWriteOff"])

    def test_main_app_does_not_expose_node_gateway_route(self) -> None:
        client = TestClient(create_graph_runtime_app())

        response = client.post(
            NODE_GATEWAY_LLM_EVALUATE_PATH,
            json={
                "prompt": "should stay on standalone node gateway service",
            },
        )

        self.assertEqual(response.status_code, 404)

    @patch("expense_audit_orchestrator.bootstrap.create_graph_runtime_client")
    @patch("expense_audit_orchestrator.bootstrap.create_kingdee_ocr_provider_from_env")
    def test_orchestrator_defaults_to_graph_runtime_url_from_env(
        self,
        mock_create_kingdee_provider,
        mock_create_runtime_client,
    ) -> None:
        runtime_client = MagicMock()
        mock_create_kingdee_provider.return_value = MagicMock(return_value={"invoiceType": "26"})
        mock_create_runtime_client.return_value = runtime_client

        with patch.dict("os.environ", {"GRAPH_RUNTIME_URL": "http://127.0.0.1:8092"}, clear=False):
            service = create_orchestrator_service()

        self.assertIsInstance(service, ReceiptAuditService)
        mock_create_runtime_client.assert_called_once_with("http://127.0.0.1:8092")

    @patch("expense_audit_orchestrator.bootstrap.create_graph_runtime_client")
    @patch("expense_audit_orchestrator.bootstrap.create_kingdee_ocr_provider_from_env")
    def test_orchestrator_uses_kingdee_ocr_provider_when_available(
        self,
        mock_create_kingdee_provider,
        mock_create_runtime_client,
    ) -> None:
        runtime_client = MagicMock()
        kingdee_provider = MagicMock(return_value={"invoiceType": "26"})
        mock_create_runtime_client.return_value = runtime_client
        mock_create_kingdee_provider.return_value = kingdee_provider

        service = create_orchestrator_service()

        self.assertIsInstance(service, ReceiptAuditService)
        self.assertIs(service._data_preparer.ocr_provider, kingdee_provider)
        mock_create_kingdee_provider.assert_called_once_with()

    @patch("expense_audit_orchestrator.bootstrap.create_graph_runtime_client")
    @patch("expense_audit_orchestrator.bootstrap.create_kingdee_ocr_provider_from_env")
    def test_orchestrator_forwards_receipt_result_sink(
        self,
        mock_create_kingdee_provider,
        mock_create_runtime_client,
    ) -> None:
        runtime_client = MagicMock()
        mock_create_runtime_client.return_value = runtime_client
        mock_create_kingdee_provider.return_value = MagicMock(return_value={"invoiceType": "26"})

        published_receipts: list[dict[str, Any]] = []

        def capture_receipt_result(receipt_result: dict[str, Any]) -> None:
            published_receipts.append(receipt_result)

        service = create_orchestrator_service(receipt_result_sink=capture_receipt_result)

        self.assertIsInstance(service, ReceiptAuditService)
        self.assertIs(service._receipt_result_sink, capture_receipt_result)
        self.assertEqual(published_receipts, [])

    @patch("expense_audit_orchestrator.bootstrap.build_receipt_writeback_sink")
    @patch("expense_audit_orchestrator.bootstrap.build_receipt_writeback_file_sink")
    def test_writeback_output_dir_does_not_disable_real_writeback_sink(
        self,
        mock_build_file_sink,
        mock_build_writeback_sink,
    ) -> None:
        invoked_sinks: list[str] = []

        def file_sink(_receipt_result: dict[str, Any]) -> None:
            invoked_sinks.append("file")

        def writeback_sink(_receipt_result: dict[str, Any]) -> None:
            invoked_sinks.append("writeback")

        mock_build_file_sink.return_value = file_sink
        mock_build_writeback_sink.return_value = writeback_sink

        sink = orchestrator_bootstrap._resolve_receipt_result_sink(
            receipt_result_sink=None,
            enable_writeback=True,
            writeback_client=None,
            writeback_output_dir="output/worker-debug/writeback",
            audit_service_url="https://service.example",
            profile=ExpenseProfile(name="test"),
        )

        self.assertIsNotNone(sink)
        sink({"receiptCode": "REC-001"})

        mock_build_file_sink.assert_called_once()
        mock_build_writeback_sink.assert_called_once()
        self.assertEqual(invoked_sinks, ["file", "writeback"])

    @patch("expense_audit_orchestrator.bootstrap.fetch_audit_invoice_file_info")
    @patch("expense_audit_orchestrator.bootstrap.fetch_audit_invoice_files")
    @patch("expense_audit_orchestrator.bootstrap.create_graph_runtime_client")
    @patch("expense_audit_orchestrator.bootstrap.create_kingdee_ocr_provider_from_env")
    def test_orchestrator_passes_audit_service_url_to_invoice_file_providers(
        self,
        mock_create_kingdee_provider,
        mock_create_runtime_client,
        mock_fetch_audit_invoice_files,
        mock_fetch_audit_invoice_file_info,
    ) -> None:
        runtime_client = MagicMock()
        mock_create_runtime_client.return_value = runtime_client
        mock_create_kingdee_provider.return_value = MagicMock(return_value={"invoiceType": "26"})
        mock_fetch_audit_invoice_files.return_value = []
        mock_fetch_audit_invoice_file_info.return_value = []

        service = create_orchestrator_service(audit_service_url="https://service.example")

        self.assertIsInstance(service, ReceiptAuditService)
        service._data_preparer.audit_invoice_files_provider("REC-001", 0)
        service._data_preparer.audit_invoice_file_info_provider("FID-001")

        mock_fetch_audit_invoice_files.assert_called_once_with(
            "REC-001",
            0,
            service_url="https://service.example",
        )
        mock_fetch_audit_invoice_file_info.assert_called_once_with(
            "FID-001",
            service_url="https://service.example",
        )

    @patch("expense_audit_orchestrator.bootstrap.create_graph_runtime_client")
    @patch("expense_audit_orchestrator.bootstrap.create_kingdee_ocr_provider_from_env")
    def test_orchestrator_does_not_fallback_when_kingdee_provider_is_unavailable(
        self,
        mock_create_kingdee_provider,
        mock_create_runtime_client,
    ) -> None:
        mock_create_runtime_client.return_value = MagicMock()
        mock_create_kingdee_provider.return_value = None

        with self.assertRaisesRegex(ValueError, "kingdee ocr provider is required"):
            create_orchestrator_service()

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_info_returns_data_payload(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": {
                    "instanceCode": "REC-001",
                    "eiCode": "FEE-PROJ-1001",
                },
            }
        )

        result = main.fetch_audit_info("REC-001")

        self.assertEqual(result["instanceCode"], "REC-001")
        self.assertEqual(result["eiCode"], "FEE-PROJ-1001")
        requested_url = mock_urlopen.call_args.args[0]
        if hasattr(requested_url, 'full_url'):
            requested_url = requested_url.full_url
        self.assertTrue(requested_url.endswith("/api/audit-service/audit/info/REC-001"))

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_service_helpers_request_expected_endpoints(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            FakeHttpResponse({"code": 0, "message": "success", "data": [{"value": "black"}]}),
            FakeHttpResponse({"code": 0, "message": "success", "data": [{"cCode": "RJCW01"}]}),
            FakeHttpResponse({"code": 0, "message": "success", "data": [{"eiCode": "FEE-PROJ-1001"}]}),
            FakeHttpResponse({"code": 0, "message": "success", "data": [{"miInstanceCode": "REC-001"}]}),
        ]

        company_blacklist = main.fetch_company_blacklist()
        company_list = main.fetch_company_list()
        expense_invoice_types = main.fetch_expense_invoice_types("FEE-PROJ-1001")
        invoice_info = main.fetch_invoice_info("26357000000141826844", "REC-001", accounting_code="ACCT-01")

        self.assertEqual(company_blacklist[0]["value"], "black")
        self.assertEqual(company_list[0]["cCode"], "RJCW01")
        self.assertEqual(expense_invoice_types[0]["eiCode"], "FEE-PROJ-1001")
        self.assertEqual(invoice_info[0]["miInstanceCode"], "REC-001")
        requested_urls = [call.args[0] for call in mock_urlopen.call_args_list]
        requested_urls = [url.full_url if hasattr(url, 'full_url') else url for url in requested_urls]
        self.assertTrue(requested_urls[0].endswith("/api/audit-service/audit/company-black-list"))
        self.assertTrue(requested_urls[1].endswith("/api/audit-service/audit/company-list"))
        self.assertTrue(requested_urls[2].endswith("/api/audit-service/audit/company-list?eiCode=FEE-PROJ-1001"))
        self.assertIn("/api/audit-service/audit/invoice-info?", requested_urls[3])
        self.assertIn("chequeNo=26357000000141826844", requested_urls[3])
        self.assertIn("instanceCode=REC-001", requested_urls[3])
        self.assertIn("accountingCode=ACCT-01", requested_urls[3])

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_files_requests_expected_endpoint(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": [
                    {
                        "afiid": "AFID-001",
                        "miInstanceCode": "REC-001",
                        "fid": "FID-001",
                        "type": 0,
                        "fileName": "origin.pdf",
                        "aiid": "AIID-001",
                    }
                ],
            }
        )

        result = main.fetch_audit_invoice_files("REC-001")

        self.assertEqual(result[0]["miInstanceCode"], "REC-001")
        self.assertEqual(result[0]["type"], 0)
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        requested_url = request.full_url
        self.assertIn("/api/audit-service/audit/invoice-file?", requested_url)
        self.assertIn("instanceCode=REC-001", requested_url)
        self.assertIn("aType=0", requested_url)

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_file_info_requests_expected_endpoint(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": [
                    {
                        "fileUrl": "https://files.example/FID-001.pdf",
                        "fileBase64": "BASE64-FID-001",
                        "fid": "FID-001",
                    }
                ],
            }
        )

        result = main.fetch_audit_invoice_file_info("FID-001")

        self.assertEqual(result[0]["fid"], "FID-001")
        self.assertEqual(result[0]["fileBase64"], "BASE64-FID-001")
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        requested_url = request.full_url
        self.assertTrue(requested_url.endswith("/api/audit-service/audit/invoice-file-info/FID-001"))

    def test_data_preparer_collects_upstream_service_data(self) -> None:
        def unexpected_invoice_file_provider(receipt_code: str) -> str:
            raise AssertionError(f"invoice_file_provider should not be used: {receipt_code}")

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=unexpected_invoice_file_provider,
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH: {
                "invoiceType": "2",
                "orgName": "锐捷网络股份有限公司",
                "invoiceNo": "26357000000141826844",
            },
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
            },
            company_blacklist_provider=lambda: [{"value": "福建示例供应商有限公司"}],
            company_list_provider=lambda: [{"cCode": "RJCW01", "cName": "锐捷网络股份有限公司"}],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "2"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [
                {
                    "chequeNo": cheque_no,
                    "miInstanceCode": instance_code,
                    "accountingCode": accounting_code,
                }
            ],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "afiid": "AFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin.pdf",
                    "aiid": "AIID-001",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileUrl": f"https://files.example/{fid}.pdf",
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            field_mappings_provider=lambda belong_table: [],
            extra_enrichers={
                "budgetInfo": lambda receipt_code, file_path, ocr_data, service_data: {
                    "ownerDepartment": "销售部"
                }
            },
        )

        prepared_input = preparer.prepare("REC-001")

        self.assertEqual(prepared_input["context"]["receiptCode"], "REC-001")
        self.assertEqual(prepared_input["receipt"]["filePath"], "base64://BASE64-FID-001")
        self.assertEqual(prepared_input["serviceData"]["auditInfo"]["eiCode"], "FEE-PROJ-1001")
        self.assertEqual(
            prepared_input["serviceData"]["budgetInfo"]["ownerDepartment"],
            "销售部",
        )

    def test_prepare_receipt_context_collects_truthcheck_field_mappings(self) -> None:
        captured_belong_tables: list[str] = []

        def provide_field_mappings(belong_table: str) -> list[dict[str, Any]]:
            captured_belong_tables.append(belong_table)
            return [
                {
                    "fieldName": f"{belong_table}Field",
                    "fieldLable": f"{belong_table}标签",
                    "belongTable": belong_table,
                    "status": True,
                }
            ]

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: (_ for _ in ()).throw(
                AssertionError(f"invoice_file_provider should not be used: {receipt_code}")
            ),
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH: {
                "invoiceType": "26",
                "invoiceNo": "INV-TRUTHCHECK-001",
            },
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
            },
            company_blacklist_provider=lambda: [],
            company_list_provider=lambda: [],
            expense_invoice_types_provider=lambda ei_code: [],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "afiid": "AFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin.pdf",
                    "aiid": "AIID-001",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            receipt_enrichers={"telecom_list": lambda rc, sd: []},
            field_mappings_provider=provide_field_mappings,
        )

        receipt_context = preparer.prepare_receipt_context("REC-TRUTHCHECK-MAP-001")

        self.assertEqual(captured_belong_tables, ["bill", "item"])
        self.assertEqual(
            receipt_context["serviceData"]["truthCheckFieldMappings"]["bill"][0]["fieldName"],
            "billField",
        )
        self.assertEqual(
            receipt_context["serviceData"]["truthCheckFieldMappings"]["item"][0]["fieldName"],
            "itemField",
        )

    def test_data_preparer_supports_context_aware_ocr_provider(self) -> None:
        captured: dict[str, Any] = {}

        def context_aware_ocr_provider(
            file_path: str,
            ocr_sample_path=main.DEFAULT_OCR_PATH,
            *,
            receipt_code: str,
            audit_info: dict[str, Any],
            company_list: list[dict[str, Any]],
        ) -> dict[str, Any]:
            captured["file_path"] = file_path
            captured["ocr_sample_path"] = ocr_sample_path
            captured["receipt_code"] = receipt_code
            captured["audit_info"] = audit_info
            captured["company_list"] = company_list
            return {
                "invoiceType": "26",
                "orgName": audit_info["verifiUserCompanyName"],
                "invoiceNo": "26357000000141826844",
                "buyerTaxNo": company_list[0]["companyTax"],
            }

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: (_ for _ in ()).throw(
                AssertionError(f"invoice_file_provider should not be used: {receipt_code}")
            ),
            ocr_provider=context_aware_ocr_provider,
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
                "verifiUserCompanyName": "锐捷网络股份有限公司",
            },
            company_blacklist_provider=lambda: [],
            company_list_provider=lambda: [
                {
                    "cCode": "RJCW01",
                    "cName": "锐捷网络股份有限公司",
                    "companyTax": "91110108668444162H",
                }
            ],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "26"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "afiid": "AFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin.pdf",
                    "aiid": "AIID-001",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            receipt_enrichers={"telecom_list": lambda rc, sd: []},
            field_mappings_provider=lambda belong_table: [],
        )

        prepared_input = preparer.prepare("REC-CTX")

        self.assertEqual(captured["receipt_code"], "REC-CTX")
        self.assertEqual(captured["file_path"], "base64://BASE64-FID-001")
        self.assertEqual(captured["audit_info"]["verifiUserCompanyName"], "锐捷网络股份有限公司")
        self.assertEqual(captured["company_list"][0]["companyTax"], "91110108668444162H")
        self.assertEqual(prepared_input["buyerTaxNo"], "91110108668444162H")

    def test_prepare_invoice_input_passes_invoice_file_name_to_context_aware_ocr_provider(self) -> None:
        captured: dict[str, Any] = {}

        def context_aware_ocr_provider(
            file_path: str,
            ocr_sample_path=main.DEFAULT_OCR_PATH,
            *,
            receipt_code: str,
            audit_info: dict[str, Any],
            company_list: list[dict[str, Any]],
            file_name: str,
        ) -> dict[str, Any]:
            captured["file_path"] = file_path
            captured["ocr_sample_path"] = ocr_sample_path
            captured["receipt_code"] = receipt_code
            captured["audit_info"] = audit_info
            captured["company_list"] = company_list
            captured["file_name"] = file_name
            return {
                "invoiceType": "26",
                "invoiceNo": "NO-FID-IMG-001",
            }

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: f"/tmp/{receipt_code}.pdf",
            ocr_provider=context_aware_ocr_provider,
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
                "verifiUserCompanyName": "锐捷网络股份有限公司",
            },
            company_blacklist_provider=lambda: [],
            company_list_provider=lambda: [
                {
                    "cCode": "RJCW01",
                    "cName": "锐捷网络股份有限公司",
                    "companyTax": "91110108668444162H",
                }
            ],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "26"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "afiid": "AFID-IMG-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-IMG-001",
                    "type": a_type,
                    "fileName": "origin-001.jpg",
                    "aiid": "AIID-001",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            receipt_enrichers={"telecom_list": lambda rc, sd: []},
            field_mappings_provider=lambda belong_table: [],
        )

        receipt_context = preparer.prepare_receipt_context("REC-CTX-BASE64")
        prepared_input = preparer.prepare_invoice_input(
            "REC-CTX-BASE64",
            receipt_context["invoiceFiles"][0],
            receipt_context,
        )

        self.assertEqual(captured["receipt_code"], "REC-CTX-BASE64")
        self.assertEqual(captured["file_path"], "base64://BASE64-FID-IMG-001")
        self.assertEqual(captured["file_name"], "origin-001.jpg")
        self.assertEqual(captured["audit_info"]["instanceCode"], "REC-CTX-BASE64")
        self.assertEqual(captured["company_list"][0]["companyTax"], "91110108668444162H")
        self.assertEqual(prepared_input["receipt"]["filePath"], "base64://BASE64-FID-IMG-001")

    def test_prepare_invoice_input_preserves_ocr_envelope_and_uses_normalized_payload(self) -> None:
        ocr_envelope = {
            "provider": "kingdee",
            "request": {
                "receiptCode": "REC-OCR-ENV-001",
                "fileName": "origin.pdf",
            },
            "upload": {
                "fileType": "1",
                "fileDownUrl": "https://kingdee.example/file/FID-001.pdf",
            },
            "recognition": {
                "rawPayload": {
                    "status": True,
                    "data": {
                        "invoiceNo": "OCR-ENV-001",
                    },
                },
                "normalized": {
                    "invoiceType": "26",
                    "invoiceNo": "OCR-ENV-001",
                    "buyerTaxNo": "913500007549617646",
                    "items": [
                        {
                            "goodsName": "*电信服务*通信服务费",
                            "detailAmount": "476.1",
                            "taxRate": "0",
                        }
                    ],
                },
            },
            "status": {
                "code": "200",
                "message": "success",
                "finishedAt": "2026-06-16T12:00:00+00:00",
            },
        }

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: (_ for _ in ()).throw(
                AssertionError(f"invoice_file_provider should not be used: {receipt_code}")
            ),
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH, **kwargs: ocr_envelope,
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
                "verifiUserCompanyName": "锐捷网络股份有限公司",
            },
            company_blacklist_provider=lambda: [],
            company_list_provider=lambda: [
                {
                    "cCode": "RJCW01",
                    "cName": "锐捷网络股份有限公司",
                    "companyTax": "913500007549617646",
                }
            ],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "26"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "afiid": "AFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin.pdf",
                    "aiid": "AIID-001",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            receipt_enrichers={"telecom_list": lambda rc, sd: []},
            field_mappings_provider=lambda belong_table: [],
        )

        prepared_input = preparer.prepare("REC-OCR-ENV-001")

        self.assertEqual(prepared_input["invoiceNo"], "OCR-ENV-001")
        self.assertEqual(
            prepared_input["serviceData"]["ocrEnvelope"]["upload"]["fileDownUrl"],
            "https://kingdee.example/file/FID-001.pdf",
        )
        self.assertEqual(
            prepared_input["serviceData"]["ocrEnvelope"]["recognition"]["rawPayload"]["data"]["invoiceNo"],
            "OCR-ENV-001",
        )

    def test_prepare_invoice_input_generates_current_invoice_atcrid(self) -> None:
        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: (_ for _ in ()).throw(
                AssertionError(f"invoice_file_provider should not be used: {receipt_code}")
            ),
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH: {
                "invoiceType": "26",
                "invoiceNo": "INV-ATCRID-001",
            },
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
                "verifiUserId": "user-001",
                "verifiUserName": "测试用户",
            },
            company_blacklist_provider=lambda: [],
            company_list_provider=lambda: [],
            expense_invoice_types_provider=lambda ei_code: [],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "afiid": "AFID-ATCRID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-ATCRID-001",
                    "type": a_type,
                    "fileName": "origin.pdf",
                    "aiid": "AIID-ATCRID-001",
                    "createTime": "2026-06-17 10:00:00",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            receipt_enrichers={"telecom_list": lambda rc, sd: []},
            field_mappings_provider=lambda belong_table: [],
        )

        prepared_input = preparer.prepare("REC-ATCRID-001")

        self.assertTrue(prepared_input["serviceData"]["currentInvoiceInfo"]["atcrid"])
        self.assertEqual(
            prepared_input["context"]["serviceData"]["currentInvoiceInfo"]["atcrid"],
            prepared_input["serviceData"]["currentInvoiceInfo"]["atcrid"],
        )

    def test_data_preparer_collects_all_mock_service_data(self) -> None:
        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: "/tmp/invoice.pdf",
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH: {
                "invoiceType": "26",
                "orgName": "锐捷网络股份有限公司",
                "invoiceNo": "26357000000141826844",
            },
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
            },
            company_blacklist_provider=lambda: [
                {
                    "value": "福建示例供应商有限公司",
                }
            ],
            company_list_provider=lambda: [
                {
                    "cCode": "RJCW01",
                    "cName": "锐捷网络股份有限公司",
                }
            ],
            expense_invoice_types_provider=lambda ei_code: [
                {
                    "eiCode": ei_code,
                    "invoiceType": "26",
                }
            ],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [
                {
                    "chequeNo": cheque_no,
                    "miInstanceCode": instance_code,
                    "accountingCode": accounting_code,
                }
            ],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "afiid": "AFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin.pdf",
                    "aiid": "AIID-001",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileUrl": f"https://files.example/{fid}.pdf",
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            field_mappings_provider=lambda belong_table: [],
        )

        prepared_input = preparer.prepare("REC-001")

        self.assertEqual(
            prepared_input["serviceData"]["companyBlacklist"][0]["value"],
            "福建示例供应商有限公司",
        )
        self.assertEqual(
            prepared_input["serviceData"]["companyList"][0]["cCode"],
            "RJCW01",
        )
        self.assertEqual(
            prepared_input["serviceData"]["expenseInvoiceTypes"][0]["eiCode"],
            "FEE-PROJ-1001",
        )
        self.assertEqual(
            prepared_input["serviceData"]["invoiceUsageHistory"][0]["chequeNo"],
            "26357000000141826844",
        )
        self.assertEqual(
            prepared_input["serviceData"]["currentInvoiceInfo"]["aiiid"],
            prepared_input["invoice_info_id"],
        )
        self.assertEqual(
            prepared_input["context"]["serviceData"]["companyList"],
            prepared_input["serviceData"]["companyList"],
        )
        self.assertEqual(
            prepared_input["context"]["serviceData"]["invoiceUsageHistory"],
            prepared_input["serviceData"]["invoiceUsageHistory"],
        )
        self.assertEqual(
            prepared_input["context"]["serviceData"]["currentInvoiceInfo"],
            prepared_input["serviceData"]["currentInvoiceInfo"],
        )

    def test_data_preparer_collects_audit_invoice_file_data(self) -> None:
        captured_calls: list[tuple[str, int]] = []

        def provide_audit_invoice_files(instance_code: str, a_type: int = 0) -> list[dict[str, Any]]:
            captured_calls.append((instance_code, a_type))
            return [
                {
                    "afiid": "AFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin.pdf",
                    "aiid": "AIID-001",
                }
            ]

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: "/tmp/invoice.pdf",
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH: {
                "invoiceType": "26",
                "orgName": "锐捷网络股份有限公司",
                "invoiceNo": "26357000000141826844",
            },
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
            },
            company_blacklist_provider=lambda: [{"value": "福建示例供应商有限公司"}],
            company_list_provider=lambda: [{"cCode": "RJCW01", "cName": "锐捷网络股份有限公司"}],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "26"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [
                {
                    "chequeNo": cheque_no,
                    "miInstanceCode": instance_code,
                    "accountingCode": accounting_code,
                }
            ],
            audit_invoice_files_provider=provide_audit_invoice_files,
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileUrl": f"https://files.example/{fid}.pdf",
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            field_mappings_provider=lambda belong_table: [],
        )

        prepared_input = preparer.prepare("REC-001")

        self.assertEqual(captured_calls, [("REC-001", 0)])
        self.assertEqual(
            prepared_input["serviceData"]["auditInvoiceFiles"][0]["fileName"],
            "origin.pdf",
        )
        self.assertEqual(
            prepared_input["context"]["serviceData"]["auditInvoiceFiles"],
            prepared_input["serviceData"]["auditInvoiceFiles"],
        )

    def test_data_preparer_collects_audit_invoice_file_info_data(self) -> None:
        captured_fids: list[str] = []

        def provide_audit_invoice_files(instance_code: str, a_type: int = 0) -> list[dict[str, Any]]:
            return [
                {
                    "afiid": "AFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin.pdf",
                    "aiid": "AIID-001",
                },
                {
                    "afiid": "AFID-002",
                    "miInstanceCode": instance_code,
                    "fid": "FID-002",
                    "type": a_type,
                    "fileName": "clip.pdf",
                    "aiid": "AIID-001",
                },
            ]

        def provide_audit_invoice_file_info(fid: str) -> list[dict[str, Any]]:
            captured_fids.append(fid)
            return [
                {
                    "fileUrl": f"https://files.example/{fid}.pdf",
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ]

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: "/tmp/invoice.pdf",
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH: {
                "invoiceType": "26",
                "orgName": "锐捷网络股份有限公司",
                "invoiceNo": "26357000000141826844",
            },
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
            },
            company_blacklist_provider=lambda: [{"value": "福建示例供应商有限公司"}],
            company_list_provider=lambda: [{"cCode": "RJCW01", "cName": "锐捷网络股份有限公司"}],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "26"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [
                {
                    "chequeNo": cheque_no,
                    "miInstanceCode": instance_code,
                    "accountingCode": accounting_code,
                }
            ],
            audit_invoice_files_provider=provide_audit_invoice_files,
            audit_invoice_file_info_provider=provide_audit_invoice_file_info,
            field_mappings_provider=lambda belong_table: [],
        )

        prepared_input = preparer.prepare("REC-001")

        self.assertEqual(captured_fids, ["FID-001", "FID-002"])
        self.assertEqual(
            prepared_input["serviceData"]["auditInvoiceFileInfo"][1]["fid"],
            "FID-002",
        )
        self.assertEqual(
            prepared_input["context"]["serviceData"]["auditInvoiceFileInfo"],
            prepared_input["serviceData"]["auditInvoiceFileInfo"],
        )

    def test_build_rule_input_exposes_all_service_data_in_context(self) -> None:
        service_data = {
            "auditInfo": {
                "instanceCode": "REC-001",
                "eiCode": "FEE-PROJ-1001",
                "instanceComCode": "112",
            },
            "companyBlacklist": [{"value": "福建示例供应商有限公司"}],
            "companyList": [{"cCode": "RJCW01", "cName": "锐捷网络股份有限公司"}],
            "expenseInvoiceTypes": [{"invoiceType": "26"}],
            "invoiceUsageHistory": [{"miInstanceCode": "REC-001"}],
            "currentInvoiceInfo": {"aiiid": "AIIID-001", "miInstanceCode": "REC-001"},
        }

        prepared_input = main.build_rule_input(
            "REC-001",
            {
                "invoiceType": "26",
                "orgName": "锐捷网络股份有限公司",
                "invoiceNo": "26357000000141826844",
            },
            file_path="/tmp/invoice.pdf",
            service_data=service_data,
        )

        self.assertEqual(prepared_input["context"]["serviceData"]["auditInfo"], service_data["auditInfo"])
        self.assertEqual(prepared_input["context"]["serviceData"]["companyBlacklist"], service_data["companyBlacklist"])
        self.assertEqual(prepared_input["context"]["serviceData"]["companyList"], service_data["companyList"])
        self.assertEqual(
            prepared_input["context"]["serviceData"]["expenseInvoiceTypes"],
            service_data["expenseInvoiceTypes"],
        )
        self.assertEqual(prepared_input["context"]["serviceData"]["invoiceUsageHistory"], service_data["invoiceUsageHistory"])
        self.assertEqual(prepared_input["context"]["serviceData"]["currentInvoiceInfo"], service_data["currentInvoiceInfo"])
        self.assertEqual(prepared_input["instanceComCode"], "112")

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_default_data_preparer_collects_all_mock_service_data(self, mock_urlopen) -> None:
        requested_urls: list[str] = []
        payloads = {
            "/api/audit-service/audit/info/REC-001": {
                "code": 0,
                "message": "success",
                "data": {
                    "instanceCode": "REC-001",
                    "eiCode": "FEE-PROJ-1001",
                    "verifiUserCompanyName": "锐捷网络股份有限公司",
                },
            },
            "/api/audit-service/audit/company-black-list": {
                "code": 0,
                "message": "success",
                "data": [{"value": "福建示例供应商有限公司"}],
            },
            "/api/audit-service/audit/company-list": {
                "code": 0,
                "message": "success",
                "data": [{"cCode": "RJCW01", "cName": "锐捷网络股份有限公司"}],
            },
            "/api/audit-service/audit/company-list?eiCode=FEE-PROJ-1001": {
                "code": 0,
                "message": "success",
                "data": [{"invoiceType": "26", "eiCode": "FEE-PROJ-1001"}],
            },
            "/api/audit-service/audit/invoice-info?chequeNo=26357000000141826844&instanceCode=REC-001&accountingCode=RJCW01": {
                "code": 0,
                "message": "success",
                "data": [{"miInstanceCode": "REC-001", "miApplyUserName": "王丽"}],
            },
            "/api/audit-service/audit/invoice-file?instanceCode=REC-001&aType=0": {
                "code": 0,
                "message": "success",
                "data": [
                    {
                        "afiid": "AFID-001",
                        "miInstanceCode": "REC-001",
                        "fid": "FID-001",
                        "type": 0,
                        "fileName": "REC-001-origin.pdf",
                        "aiid": "AIID-001",
                    }
                ],
            },
            "/api/audit-service/audit/invoice-file-info/FID-001": {
                "code": 0,
                "message": "success",
                "data": [
                    {
                        "fileUrl": "https://files.example/FID-001.pdf",
                        "fileBase64": "BASE64-FID-001",
                        "fid": "FID-001",
                    }
                ],
            },
            "/api/audit-service/audit/fie-id-mapping/bill": {
                "status": "200",
                "err": "操作成功",
                "data": [],
            },
            "/api/audit-service/audit/fie-id-mapping/item": {
                "status": "200",
                "err": "操作成功",
                "data": [],
            },
        }

        def fake_urlopen(url: str, timeout: float = 5.0):
            del timeout
            request_url = url.full_url if isinstance(url, Request) else url
            requested_urls.append(request_url)
            parsed = urlparse(request_url)
            normalized = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            payload = payloads.get(normalized)
            if payload is None:
                raise AssertionError(f"unexpected url requested: {normalized}")
            return FakeHttpResponse(payload)

        mock_urlopen.side_effect = fake_urlopen

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: (_ for _ in ()).throw(
                AssertionError(f"invoice_file_provider should not be used: {receipt_code}")
            ),
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH: {
                "invoiceType": "26",
                "orgName": "锐捷网络股份有限公司",
                "invoiceNo": "26357000000141826844",
            },
            receipt_enrichers={
                "telecom_list": lambda receipt_code, service_data: [["电信", "深圳"]]
            },
        )

        prepared_input = preparer.prepare("REC-001")

        self.assertEqual(
            set(prepared_input["serviceData"]),
            {
                "auditInfo",
                "companyBlacklist",
                "companyList",
                "expenseInvoiceTypes",
                "invoiceUsageHistory",
                "currentInvoiceInfo",
                "auditInvoiceFiles",
                "auditInvoiceFileInfo",
                "truthCheckFieldMappings",
                "telecom_list",
            },
        )
        self.assertEqual(prepared_input["context"]["serviceData"]["companyList"][0]["cCode"], "RJCW01")
        self.assertEqual(prepared_input["receipt"]["filePath"], "base64://BASE64-FID-001")
        self.assertEqual(prepared_input["serviceData"]["invoiceUsageHistory"][0]["miApplyUserName"], "王丽")
        self.assertEqual(
            prepared_input["serviceData"]["currentInvoiceInfo"]["aiiid"],
            prepared_input["invoice_info_id"],
        )
        self.assertEqual(prepared_input["serviceData"]["auditInvoiceFiles"][0]["fid"], "FID-001")
        self.assertEqual(
            prepared_input["serviceData"]["auditInvoiceFileInfo"][0]["fileBase64"],
            "BASE64-FID-001",
        )
        self.assertEqual(
            [
                urlparse(url).path + (f"?{urlparse(url).query}" if urlparse(url).query else "")
                for url in requested_urls
            ],
            [
                "/api/audit-service/audit/info/REC-001",
                "/api/audit-service/audit/company-black-list",
                "/api/audit-service/audit/company-list",
                "/api/audit-service/audit/invoice-file?instanceCode=REC-001&aType=0",
                "/api/audit-service/audit/invoice-file-info/FID-001",
                "/api/audit-service/audit/fie-id-mapping/bill",
                "/api/audit-service/audit/fie-id-mapping/item",
                "/api/audit-service/audit/company-list?eiCode=FEE-PROJ-1001",
                "/api/audit-service/audit/invoice-info?chequeNo=26357000000141826844&instanceCode=REC-001&accountingCode=RJCW01",
            ],
        )
        self.assertEqual(prepared_input["serviceData"]["telecom_list"][0], ["电信", "深圳"])
        self.assertEqual(prepared_input["context"]["serviceData"]["telecom_list"][0], ["电信", "深圳"])

    def test_process_receipt_pipeline_uses_prepared_input(self) -> None:
        decision_engine = FakeDecisionEngine()
        prepared_input = {
            "invoiceType": "2",
            "context": {"receiptCode": "REC-001"},
            "serviceData": {"auditInfo": {"instanceCode": "REC-001"}},
        }
        data_preparer = FakeDataPreparer(prepared_input)

        result = main.process_receipt_pipeline(
            "REC-001",
            decision_engine,
            data_preparer=data_preparer,
        )

        self.assertEqual(data_preparer.calls, [("REC-001", main.DEFAULT_OCR_PATH)])
        self.assertIs(decision_engine.received_input, prepared_input)
        self.assertEqual(result["checkStatus"], "passed")

    def test_data_preparer_splits_receipt_context_from_invoice_input(self) -> None:
        invoice_info_calls: list[tuple[str, str, str | None]] = []

        def ocr_provider(
            file_path: str,
            ocr_sample_path=main.DEFAULT_OCR_PATH,
            *,
            file_name: str,
        ) -> dict[str, Any]:
            del file_path, ocr_sample_path
            return {
                "invoiceType": "26",
                "orgName": "锐捷网络股份有限公司",
                "invoiceNo": f"NO-{Path(file_name).stem}",
            }

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: (_ for _ in ()).throw(
                AssertionError(f"invoice_file_provider should not be used: {receipt_code}")
            ),
            ocr_provider=ocr_provider,
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "FEE-PROJ-1001",
                "verifiUserCompanyName": "锐捷网络股份有限公司",
            },
            company_blacklist_provider=lambda: [{"value": "福建示例供应商有限公司"}],
            company_list_provider=lambda: [
                {
                    "cCode": "RJCW01",
                    "cName": "锐捷网络股份有限公司",
                    "companyTax": "913500007549617646",
                }
            ],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "26"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: invoice_info_calls.append(
                (cheque_no, instance_code, accounting_code)
            )
            or [{"chequeNo": cheque_no, "miInstanceCode": instance_code}],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "afiid": "AFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin-001.pdf",
                    "aiid": "AIID-001",
                },
                {
                    "afiid": "AFID-002",
                    "miInstanceCode": instance_code,
                    "fid": "FID-002",
                    "type": a_type,
                    "fileName": "origin-002.pdf",
                    "aiid": "AIID-001",
                },
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileUrl": f"https://files.example/{fid}.pdf",
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            receipt_enrichers={"telecom_list": lambda rc, sd: [["电信", "深圳"]]},
            field_mappings_provider=lambda belong_table: [],
        )

        receipt_context = preparer.prepare_receipt_context("REC-SPLIT")
        invoice_files = receipt_context["invoiceFiles"]
        prepared_input = preparer.prepare_invoice_input("REC-SPLIT", invoice_files[0])

        self.assertEqual(receipt_context["receiptCode"], "REC-SPLIT")
        self.assertEqual(receipt_context["serviceData"]["auditInfo"]["instanceCode"], "REC-SPLIT")
        self.assertEqual(receipt_context["serviceData"]["companyList"][0]["cCode"], "RJCW01")
        self.assertEqual(len(invoice_files), 2)
        self.assertEqual(invoice_files[0]["fid"], "FID-001")
        self.assertEqual(invoice_files[0]["filePath"], "base64://BASE64-FID-001")
        self.assertEqual(invoice_files[0]["auditInvoiceFileInfo"]["fid"], "FID-001")
        self.assertEqual(prepared_input["receipt"]["code"], "REC-SPLIT")
        self.assertEqual(prepared_input["receipt"]["filePath"], "base64://BASE64-FID-001")
        self.assertEqual(prepared_input["serviceData"]["auditInfo"]["instanceCode"], "REC-SPLIT")
        self.assertEqual(prepared_input["serviceData"]["currentAuditInvoiceFile"]["fid"], "FID-001")
        self.assertEqual(prepared_input["serviceData"]["currentAuditInvoiceFileInfo"]["fid"], "FID-001")
        self.assertEqual(prepared_input["serviceData"]["invoiceUsageHistory"][0]["chequeNo"], "NO-origin-001")
        self.assertEqual(
            prepared_input["serviceData"]["currentInvoiceInfo"]["aiiid"],
            prepared_input["invoice_info_id"],
        )
        self.assertEqual(invoice_info_calls, [("NO-origin-001", "REC-SPLIT", "RJCW01")])

    def test_prepare_invoice_input_resolves_accounting_code_from_lowercase_company_list_tax_match(self) -> None:
        invoice_info_calls: list[tuple[str, str, str | None]] = []

        def ocr_provider(
            file_path: str,
            ocr_sample_path=main.DEFAULT_OCR_PATH,
            *,
            file_name: str,
        ) -> dict[str, Any]:
            del file_path, ocr_sample_path, file_name
            return {
                "invoiceType": "26",
                "invoiceNo": "INV-LOWER-001",
                "buyerTaxNo": "91110108668444162H",
                "buyerName": "北京星网锐捷网络技术有限公司",
            }

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: (_ for _ in ()).throw(
                AssertionError(f"invoice_file_provider should not be used: {receipt_code}")
            ),
            ocr_provider=ocr_provider,
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "EI001",
            },
            company_blacklist_provider=lambda: [],
            company_list_provider=lambda: [
                {
                    "ccode": "112",
                    "cname": "北京星网锐捷网络技术有限公司",
                    "companyTax": "91110108668444162H",
                }
            ],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "26"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: invoice_info_calls.append(
                (cheque_no, instance_code, accounting_code)
            )
            or [{"aiiid": "AIIID-LOWER-001", "miInstanceCode": instance_code}],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "aifid": "AIFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin-001.pdf",
                    "aiid": "AIID-001",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileUrl": f"https://files.example/{fid}.pdf",
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            receipt_enrichers={"telecom_list": lambda rc, sd: [["联通", "北京"]]},
            field_mappings_provider=lambda belong_table: [],
        )

        receipt_context = preparer.prepare_receipt_context("REC-LOWER-001")
        prepared_input = preparer.prepare_invoice_input(
            "REC-LOWER-001",
            receipt_context["invoiceFiles"][0],
            receipt_context,
        )

        self.assertEqual(invoice_info_calls, [("INV-LOWER-001", "REC-LOWER-001", "112")])
        self.assertEqual(prepared_input["accountingCode"], "112")
        self.assertEqual(prepared_input["serviceData"]["invoiceUsageHistory"][0]["aiiid"], "AIIID-LOWER-001")
        self.assertEqual(
            prepared_input["serviceData"]["currentInvoiceInfo"]["aiiid"],
            prepared_input["invoice_info_id"],
        )
        self.assertNotEqual(
            prepared_input["serviceData"]["currentInvoiceInfo"]["aiiid"],
            prepared_input["serviceData"]["invoiceUsageHistory"][0]["aiiid"],
        )

    def test_prepare_invoice_input_falls_back_when_invoice_info_provider_raises(self) -> None:
        invoice_info_calls: list[tuple[str, str, str | None]] = []

        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: (_ for _ in ()).throw(
                AssertionError(f"invoice_file_provider should not be used: {receipt_code}")
            ),
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH, *, file_name: {
                "invoiceType": "26",
                "invoiceNo": "INV-TIMEOUT-001",
                "buyerTaxNo": "91110108668444162H",
                "buyerName": "北京星网锐捷网络技术有限公司",
            },
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "EI001",
            },
            company_blacklist_provider=lambda: [],
            company_list_provider=lambda: [
                {
                    "ccode": "112",
                    "cname": "北京星网锐捷网络技术有限公司",
                    "companyTax": "91110108668444162H",
                }
            ],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "26"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: invoice_info_calls.append(
                (cheque_no, instance_code, accounting_code)
            )
            or (_ for _ in ()).throw(TimeoutError("timed out")),
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "aifid": "AIFID-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-001",
                    "type": a_type,
                    "fileName": "origin-001.pdf",
                    "aiid": "AIID-001",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileUrl": f"https://files.example/{fid}.pdf",
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            field_mappings_provider=lambda belong_table: [],
        )

        receipt_context = preparer.prepare_receipt_context("REC-TIMEOUT-001")
        prepared_input = preparer.prepare_invoice_input(
            "REC-TIMEOUT-001",
            receipt_context["invoiceFiles"][0],
            receipt_context,
        )

        self.assertEqual(invoice_info_calls, [("INV-TIMEOUT-001", "REC-TIMEOUT-001", "112")])
        self.assertEqual(prepared_input["serviceData"]["invoiceUsageHistory"], [])

    def test_prepare_invoice_input_injects_runtime_identity_fields_and_generates_invoice_info_id(self) -> None:
        preparer = main.ReceiptDataPreparer(
            invoice_file_provider=lambda receipt_code: (_ for _ in ()).throw(
                AssertionError(f"invoice_file_provider should not be used: {receipt_code}")
            ),
            ocr_provider=lambda file_path, ocr_sample_path=main.DEFAULT_OCR_PATH, *, file_name: {
                "invoiceType": "26",
                "invoiceNo": "INV-RUNTIME-001",
                "buyerTaxNo": "91110108668444162H",
            },
            audit_info_provider=lambda receipt_code: {
                "instanceCode": receipt_code,
                "eiCode": "EI001",
            },
            company_blacklist_provider=lambda: [],
            company_list_provider=lambda: [
                {
                    "ccode": "112",
                    "cname": "北京星网锐捷网络技术有限公司",
                    "companyTax": "91110108668444162H",
                }
            ],
            expense_invoice_types_provider=lambda ei_code: [{"eiCode": ei_code, "invoiceType": "26"}],
            invoice_info_provider=lambda cheque_no, instance_code, accounting_code=None: [],
            audit_invoice_files_provider=lambda instance_code, a_type=0: [
                {
                    "aifid": "AIFID-RUNTIME-001",
                    "miInstanceCode": instance_code,
                    "fid": "FID-RUNTIME-001",
                    "type": a_type,
                    "fileName": "runtime-001.pdf",
                    "aiid": "AIID-RUNTIME-001",
                }
            ],
            audit_invoice_file_info_provider=lambda fid: [
                {
                    "fileUrl": f"https://files.example/{fid}.pdf",
                    "fileBase64": f"BASE64-{fid}",
                    "fid": fid,
                }
            ],
            receipt_enrichers={"telecom_list": lambda rc, sd: []},
            field_mappings_provider=lambda belong_table: [],
        )

        receipt_context = preparer.prepare_receipt_context("REC-RUNTIME-001")
        prepared_input = preparer.prepare_invoice_input(
            "REC-RUNTIME-001",
            receipt_context["invoiceFiles"][0],
            receipt_context,
        )

        self.assertEqual(prepared_input["instance_code"], "REC-RUNTIME-001")
        self.assertEqual(prepared_input["invoice_file_id"], "AIFID-RUNTIME-001")
        self.assertTrue(prepared_input["invoice_info_id"])
        self.assertEqual(prepared_input["serviceData"]["invoiceUsageHistory"], [])
        self.assertEqual(prepared_input["serviceData"]["currentInvoiceInfo"]["aiiid"], prepared_input["invoice_info_id"])


class FormalServiceTests(unittest.TestCase):
    def test_main_no_longer_exposes_runtime_cli_wrapper(self) -> None:
        self.assertFalse(hasattr(main, "build_cli_parser"))
        self.assertFalse(hasattr(main, "main_cli"))

    def test_normalize_decision_output_marks_failed_when_any_rule_fails(self) -> None:
        normalized = normalize_decision_output(
            {
                "invoice_type_status": "failed",
                "orgname_status": "passed",
                "buyer_taxno_status": "passed",
                "message": "请重新上传，仅支持数电普通发票和电子普通发票",
            }
        )

        self.assertEqual(normalized["checkStatus"], "failed")
        self.assertEqual(normalized["message"], "请重新上传，仅支持数电普通发票和电子普通发票")

    def test_normalize_decision_output_marks_reject_when_any_nested_rule_rejects(self) -> None:
        normalized = normalize_decision_output(
            {
                "amount_result": {
                    "distinguish_result": "REJECT",
                    "reason_code": "E31",
                    "message": "金额不足",
                },
                "header_result": {
                    "distinguish_result": "PASS",
                    "reason_code": "E01",
                    "message": "抬头一致",
                },
            }
        )

        self.assertEqual(normalized["checkStatus"], "reject")

    def test_receipt_audit_service_returns_structured_evaluation(self) -> None:
        prepared_input = {
            "invoiceType": "26",
            "context": {"receiptCode": "REC-002"},
            "serviceData": {"auditInfo": {"instanceCode": "REC-002"}},
        }
        runtime_result = {
            "receiptCode": "REC-002",
            "checkStatus": "failed",
            "message": "票据类型不支持",
            "decisionOutput": {
                "checkStatus": "failed",
                "message": "票据类型不支持",
            },
            "preparedInput": prepared_input,
            "ruleInput": prepared_input,
        }
        runtime_client = FakeGraphRuntimeClient(runtime_result)
        service = ReceiptAuditService(
            graph_runtime_client=runtime_client,
            data_preparer=FakeDataPreparer(prepared_input),
        )

        result = service.evaluate("REC-002")

        self.assertEqual(result["receiptCode"], "REC-002")
        self.assertEqual(result["decisionOutput"]["checkStatus"], "failed")
        self.assertEqual(result["decisionOutput"]["message"], "票据类型不支持")
        self.assertEqual(result["preparedInput"], prepared_input)
        self.assertEqual(runtime_client.calls[0]["prepared_input"], prepared_input)

    def test_receipt_audit_service_process_receipt_aggregates_invoice_results(self) -> None:
        receipt_context = {
            "receiptCode": "REC-MULTI-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-MULTI-001", "eiCode": "FEE-PROJ-1001"},
                "companyList": [{"cCode": "RJCW01"}],
            },
            "invoiceFiles": [
                {
                    "fid": "FID-001",
                    "filePath": "https://files.example/FID-001.pdf",
                    "auditInvoiceFile": {"fid": "FID-001", "fileName": "001.pdf"},
                    "auditInvoiceFileInfo": {"fid": "FID-001", "fileUrl": "https://files.example/FID-001.pdf"},
                },
                {
                    "fid": "FID-002",
                    "filePath": "https://files.example/FID-002.pdf",
                    "auditInvoiceFile": {"fid": "FID-002", "fileName": "002.pdf"},
                    "auditInvoiceFileInfo": {"fid": "FID-002", "fileUrl": "https://files.example/FID-002.pdf"},
                },
            ],
        }
        prepared_inputs_by_fid = {
            "FID-001": {
                "invoiceType": "26",
                "receipt": {"code": "REC-MULTI-001", "filePath": "https://files.example/FID-001.pdf"},
                "serviceData": {"auditInvoiceFile": {"fid": "FID-001"}},
            },
            "FID-002": {
                "invoiceType": "26",
                "receipt": {"code": "REC-MULTI-001", "filePath": "https://files.example/FID-002.pdf"},
                "serviceData": {"auditInvoiceFile": {"fid": "FID-002"}},
            },
        }

        class SequencedGraphRuntimeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_content
                fid = prepared_input["serviceData"]["auditInvoiceFile"]["fid"]
                self.calls.append({"prepared_input": prepared_input, "graph_path": graph_path})
                return {
                    "receiptCode": "REC-MULTI-001",
                    "decisionOutput": {
                        "checkStatus": "passed" if fid == "FID-001" else "warning",
                        "message": fid,
                    },
                    "preparedInput": prepared_input,
                }

        runtime_client = SequencedGraphRuntimeClient()
        service = ReceiptAuditService(
            graph_runtime_client=runtime_client,
            data_preparer=FakeSplitDataPreparer(receipt_context, prepared_inputs_by_fid),
        )

        result = service.process_receipt("REC-MULTI-001")

        self.assertEqual(result["receiptCode"], "REC-MULTI-001")
        self.assertEqual(result["receiptContext"], receipt_context)
        self.assertEqual(result["invoiceCount"], 2)
        self.assertEqual([item["invoiceFile"]["fid"] for item in result["invoiceResults"]], ["FID-001", "FID-002"])
        self.assertEqual(result["invoiceResults"][0]["decisionOutput"]["message"], "FID-001")
        self.assertEqual(result["invoiceResults"][1]["decisionOutput"]["checkStatus"], "warning")
        self.assertEqual(
            [call["prepared_input"]["serviceData"]["auditInvoiceFile"]["fid"] for call in runtime_client.calls],
            ["FID-001", "FID-002"],
        )

    def test_receipt_audit_service_process_receipt_aggregates_four_invoice_results(self) -> None:
        fids = ["FID-001", "FID-002", "FID-003", "FID-004"]
        status_by_fid = {
            "FID-001": "passed",
            "FID-002": "warning",
            "FID-003": "failed",
            "FID-004": "passed",
        }
        receipt_context = {
            "receiptCode": "REC-MULTI-004",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-MULTI-004", "eiCode": "FEE-PROJ-1001"},
                "companyList": [{"cCode": "RJCW01"}],
            },
            "invoiceFiles": [
                {
                    "fid": fid,
                    "filePath": f"base64://BASE64-{fid}",
                    "auditInvoiceFile": {"fid": fid, "fileName": f"{fid}.pdf"},
                    "auditInvoiceFileInfo": {"fid": fid, "fileBase64": f"BASE64-{fid}"},
                }
                for fid in fids
            ],
        }
        prepared_inputs_by_fid = {
            fid: {
                "invoiceType": "26",
                "receipt": {"code": "REC-MULTI-004", "filePath": f"base64://BASE64-{fid}"},
                "serviceData": {"auditInvoiceFile": {"fid": fid}},
            }
            for fid in fids
        }

        class SequencedGraphRuntimeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_content
                fid = prepared_input["serviceData"]["auditInvoiceFile"]["fid"]
                self.calls.append({"prepared_input": prepared_input, "graph_path": graph_path})
                return {
                    "receiptCode": "REC-MULTI-004",
                    "decisionOutput": {
                        "checkStatus": status_by_fid[fid],
                        "message": fid,
                    },
                    "preparedInput": prepared_input,
                }

        runtime_client = SequencedGraphRuntimeClient()
        service = ReceiptAuditService(
            graph_runtime_client=runtime_client,
            data_preparer=FakeSplitDataPreparer(receipt_context, prepared_inputs_by_fid),
        )

        result = service.process_receipt("REC-MULTI-004")

        self.assertEqual(result["receiptCode"], "REC-MULTI-004")
        self.assertEqual(result["invoiceCount"], 4)
        self.assertEqual([item["invoiceFile"]["fid"] for item in result["invoiceResults"]], fids)
        self.assertEqual([item["decisionOutput"]["checkStatus"] for item in result["invoiceResults"]], [
            "passed",
            "warning",
            "failed",
            "passed",
        ])
        self.assertEqual(
            [call["prepared_input"]["serviceData"]["auditInvoiceFile"]["fid"] for call in runtime_client.calls],
            fids,
        )

    def test_receipt_audit_service_prepare_receipt_aggregates_prepared_inputs_without_runtime(self) -> None:
        receipt_context = {
            "receiptCode": "REC-PREP-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-PREP-001", "eiCode": "FEE-PROJ-1001"},
                "companyList": [{"cCode": "RJCW01"}],
            },
            "invoiceFiles": [
                {
                    "fid": "FID-001",
                    "filePath": "base64://BASE64-FID-001",
                    "auditInvoiceFile": {"fid": "FID-001", "fileName": "FID-001.pdf"},
                    "auditInvoiceFileInfo": {"fid": "FID-001", "fileBase64": "BASE64-FID-001"},
                },
                {
                    "fid": "FID-002",
                    "filePath": "base64://BASE64-FID-002",
                    "auditInvoiceFile": {"fid": "FID-002", "fileName": "FID-002.pdf"},
                    "auditInvoiceFileInfo": {"fid": "FID-002", "fileBase64": "BASE64-FID-002"},
                },
            ],
        }
        prepared_inputs_by_fid = {
            "FID-001": {
                "invoiceType": "26",
                "receipt": {"code": "REC-PREP-001", "filePath": "base64://BASE64-FID-001"},
                "serviceData": {"auditInvoiceFile": {"fid": "FID-001"}},
            },
            "FID-002": {
                "invoiceType": "26",
                "receipt": {"code": "REC-PREP-001", "filePath": "base64://BASE64-FID-002"},
                "serviceData": {"auditInvoiceFile": {"fid": "FID-002"}},
            },
        }

        runtime_client = MagicMock()
        service = ReceiptAuditService(
            graph_runtime_client=runtime_client,
            data_preparer=FakeSplitDataPreparer(receipt_context, prepared_inputs_by_fid),
        )

        result = service.prepare_receipt("REC-PREP-001")

        self.assertEqual(result["receiptCode"], "REC-PREP-001")
        self.assertEqual(result["receiptContext"], receipt_context)
        self.assertEqual(result["invoiceCount"], 2)
        self.assertEqual([item["invoiceFile"]["fid"] for item in result["invoicePreparations"]], ["FID-001", "FID-002"])
        self.assertEqual(
            [item["preparedInput"]["serviceData"]["auditInvoiceFile"]["fid"] for item in result["invoicePreparations"]],
            ["FID-001", "FID-002"],
        )
        runtime_client.evaluate.assert_not_called()

    def test_receipt_audit_service_process_prepared_receipt_executes_runtime_without_repreparing(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-PROCESS-PREP-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-PROCESS-PREP-001"}},
            "receiptContext": {"receiptCode": "REC-PROCESS-PREP-001"},
            "invoiceCount": 2,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {"serviceData": {"auditInvoiceFile": {"fid": "FID-001"}}},
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {"fid": "FID-002"},
                    "preparedInput": {"serviceData": {"auditInvoiceFile": {"fid": "FID-002"}}},
                },
            ],
        }

        class SequencedGraphRuntimeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_content
                fid = prepared_input["serviceData"]["auditInvoiceFile"]["fid"]
                self.calls.append({"prepared_input": prepared_input, "graph_path": graph_path})
                return {
                    "receiptCode": "REC-PROCESS-PREP-001",
                    "decisionOutput": {
                        "checkStatus": "passed" if fid == "FID-001" else "warning",
                        "message": fid,
                    },
                    "preparedInput": prepared_input,
                }

        runtime_client = SequencedGraphRuntimeClient()
        data_preparer = MagicMock()
        service = ReceiptAuditService(
            graph_runtime_client=runtime_client,
            data_preparer=data_preparer,
        )

        result = service.process_prepared_receipt(prepared_receipt)

        self.assertEqual(result["receiptCode"], "REC-PROCESS-PREP-001")
        self.assertEqual(result["invoiceCount"], 2)
        self.assertEqual([item["invoiceFile"]["fid"] for item in result["invoiceResults"]], ["FID-001", "FID-002"])
        self.assertEqual([item["decisionOutput"]["checkStatus"] for item in result["invoiceResults"]], ["passed", "warning"])
        self.assertEqual(
            [call["prepared_input"]["serviceData"]["auditInvoiceFile"]["fid"] for call in runtime_client.calls],
            ["FID-001", "FID-002"],
        )
        data_preparer.prepare_receipt_context.assert_not_called()
        data_preparer.prepare_invoice_input.assert_not_called()

    def test_receipt_audit_service_process_prepared_receipt_continues_after_invoice_runtime_failure(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-PARTIAL-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-PARTIAL-001"}},
            "receiptContext": {"receiptCode": "REC-PARTIAL-001"},
            "invoiceCount": 3,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {"serviceData": {"auditInvoiceFile": {"fid": "FID-001"}}},
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {"fid": "FID-002"},
                    "preparedInput": {"serviceData": {"auditInvoiceFile": {"fid": "FID-002"}}},
                },
                {
                    "invoiceKey": "FID-003",
                    "invoiceFile": {"fid": "FID-003"},
                    "preparedInput": {"serviceData": {"auditInvoiceFile": {"fid": "FID-003"}}},
                },
            ],
        }

        class PartiallyFailingGraphRuntimeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_content
                fid = prepared_input["serviceData"]["auditInvoiceFile"]["fid"]
                self.calls.append({"prepared_input": prepared_input, "graph_path": graph_path})
                if fid == "FID-002":
                    raise RuntimeError("runtime exploded for FID-002")

                return {
                    "receiptCode": "REC-PARTIAL-001",
                    "decisionOutput": {
                        "checkStatus": "warning" if fid == "FID-003" else "passed",
                        "message": fid,
                    },
                    "preparedInput": prepared_input,
                }

        runtime_client = PartiallyFailingGraphRuntimeClient()
        service = ReceiptAuditService(
            graph_runtime_client=runtime_client,
            data_preparer=MagicMock(),
        )

        result = service.process_prepared_receipt(prepared_receipt)

        self.assertEqual(result["receiptCode"], "REC-PARTIAL-001")
        self.assertEqual(result["invoiceCount"], 3)
        self.assertEqual([item["invoiceKey"] for item in result["invoiceResults"]], ["FID-001", "FID-002", "FID-003"])
        self.assertEqual([item["executionStatus"] for item in result["invoiceResults"]], ["SUCCEEDED", "FAILED", "SUCCEEDED"])
        self.assertEqual([item["decisionStatus"] for item in result["invoiceResults"]], ["passed", "failed", "warning"])
        self.assertEqual(result["invoiceResults"][1]["errorMessage"], "runtime exploded for FID-002")
        self.assertEqual(result["invoiceResults"][1]["decisionOutput"]["checkStatus"], "failed")
        self.assertEqual(
            result["summary"],
            {
                "invoiceCount": 3,
                "completedCount": 3,
                "succeededCount": 2,
                "failedCount": 1,
                "warningCount": 1,
                "overallStatus": "PARTIAL_SUCCESS",
            },
        )
        self.assertEqual(
            [call["prepared_input"]["serviceData"]["auditInvoiceFile"]["fid"] for call in runtime_client.calls],
            ["FID-001", "FID-002", "FID-003"],
        )

    def test_receipt_audit_service_process_prepared_receipt_publishes_each_invoice_result_to_sink(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-SINK-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-SINK-001"}},
            "receiptContext": {"receiptCode": "REC-SINK-001"},
            "invoiceCount": 2,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {"serviceData": {"auditInvoiceFile": {"fid": "FID-001"}}},
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {"fid": "FID-002"},
                    "preparedInput": {"serviceData": {"auditInvoiceFile": {"fid": "FID-002"}}},
                },
            ],
        }

        class SequencedGraphRuntimeClient:
            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_path, graph_content
                fid = prepared_input["serviceData"]["auditInvoiceFile"]["fid"]
                return {
                    "receiptCode": "REC-SINK-001",
                    "decisionOutput": {
                        "checkStatus": "passed" if fid == "FID-001" else "warning",
                        "message": fid,
                    },
                    "preparedInput": prepared_input,
                }

        published_results: list[tuple[str, str, str, str]] = []

        def capture_invoice_result(receipt_code: str, invoice_result: dict[str, Any]) -> None:
            published_results.append(
                (
                    receipt_code,
                    invoice_result["invoiceKey"],
                    invoice_result["executionStatus"],
                    invoice_result["decisionStatus"],
                )
            )

        service = ReceiptAuditService(
            graph_runtime_client=SequencedGraphRuntimeClient(),
            data_preparer=MagicMock(),
            invoice_result_sink=capture_invoice_result,
        )

        result = service.process_prepared_receipt(prepared_receipt)
        
        self.assertEqual(result["summary"]["overallStatus"], "SUCCESS")
        self.assertEqual(
            published_results,
            [
                ("REC-SINK-001", "FID-001", "SUCCEEDED", "passed"),
                ("REC-SINK-001", "FID-002", "SUCCEEDED", "warning"),
            ],
        )

    def test_receipt_audit_service_process_prepared_receipt_publishes_summary_once_to_receipt_sink(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-RECEIPT-SINK-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-RECEIPT-SINK-001"}},
            "receiptContext": {"receiptCode": "REC-RECEIPT-SINK-001"},
            "invoiceCount": 2,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {"serviceData": {"auditInvoiceFile": {"fid": "FID-001"}}},
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {"fid": "FID-002"},
                    "preparedInput": {"serviceData": {"auditInvoiceFile": {"fid": "FID-002"}}},
                },
            ],
        }

        class SequencedGraphRuntimeClient:
            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_path, graph_content
                fid = prepared_input["serviceData"]["auditInvoiceFile"]["fid"]
                return {
                    "receiptCode": "REC-RECEIPT-SINK-001",
                    "decisionOutput": {
                        "checkStatus": "passed" if fid == "FID-001" else "warning",
                        "message": fid,
                    },
                    "preparedInput": prepared_input,
                }

        published_receipts: list[dict[str, Any]] = []

        def capture_receipt_result(receipt_result: dict[str, Any]) -> None:
            published_receipts.append(receipt_result)

        service = ReceiptAuditService(
            graph_runtime_client=SequencedGraphRuntimeClient(),
            data_preparer=MagicMock(),
            receipt_result_sink=capture_receipt_result,
        )

        result = service.process_prepared_receipt(prepared_receipt)
        
        self.assertEqual(result["summary"]["overallStatus"], "SUCCESS")
        self.assertEqual(len(published_receipts), 1)
        self.assertEqual(published_receipts[0]["receiptCode"], "REC-RECEIPT-SINK-001")
        self.assertEqual(published_receipts[0]["summary"], result["summary"])
        self.assertEqual(
            [item["invoiceKey"] for item in published_receipts[0]["invoiceResults"]],
            ["FID-001", "FID-002"],
        )

    def test_receipt_audit_service_process_prepared_receipt_deducts_apply_amount_across_invoices(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-DEDUCT-001",
            "serviceData": {"auditInfo": {"applyAmount": 1000.0}},
            "receiptContext": {"receiptCode": "REC-DEDUCT-001"},
            "invoiceCount": 3,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "serviceData": {
                            "auditInfo": {"applyAmount": 1000.0},
                            "auditInvoiceFile": {"fid": "FID-001"},
                        }
                    },
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {"fid": "FID-002"},
                    "preparedInput": {
                        "serviceData": {
                            "auditInfo": {"applyAmount": 1000.0},
                            "auditInvoiceFile": {"fid": "FID-002"},
                        }
                    },
                },
                {
                    "invoiceKey": "FID-003",
                    "invoiceFile": {"fid": "FID-003"},
                    "preparedInput": {
                        "serviceData": {
                            "auditInfo": {"applyAmount": 1000.0},
                            "auditInvoiceFile": {"fid": "FID-003"},
                        }
                    },
                },
            ],
        }

        class DeductingGraphRuntimeClient:
            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_path, graph_content
                fid = prepared_input["serviceData"]["auditInvoiceFile"]["fid"]
                apply_amount = prepared_input["serviceData"]["auditInfo"]["applyAmount"]
                if fid == "FID-001":
                    return {
                        "receiptCode": "REC-DEDUCT-001",
                        "decisionOutput": {
                            "checkStatus": "passed",
                            "message": "ok",
                            "invoice_finalAmount": 500.0,
                        },
                        "preparedInput": prepared_input,
                    }
                if fid == "FID-002":
                    assert apply_amount == 500.0, f'Expected 500.0, got {apply_amount}'
                    return {
                        "receiptCode": "REC-DEDUCT-001",
                        "decisionOutput": {
                            "checkStatus": "passed",
                            "message": "ok",
                            "invoice_finalAmount": 200.0,
                        },
                        "preparedInput": prepared_input,
                    }
                return {
                    "receiptCode": "REC-DEDUCT-001",
                    "decisionOutput": {
                        "checkStatus": "passed",
                        "message": "ok",
                        "invoice_finalAmount": 100.0,
                    },
                    "preparedInput": prepared_input,
                }

        service = ReceiptAuditService(
            graph_runtime_client=DeductingGraphRuntimeClient(),
            data_preparer=MagicMock(),
        )

        result = service.process_prepared_receipt(prepared_receipt)
        
        self.assertEqual(result["summary"]["overallStatus"], "SUCCESS")
        invoice_results = result["invoiceResults"]
        self.assertEqual(len(invoice_results), 3)

        self.assertEqual(
            invoice_results[0]["preparedInput"]["serviceData"]["auditInfo"]["applyAmount"],
            1000.0,
        )
        self.assertEqual(
            invoice_results[1]["preparedInput"]["serviceData"]["auditInfo"]["applyAmount"],
            500.0,
        )
        self.assertEqual(
            invoice_results[2]["preparedInput"]["serviceData"]["auditInfo"]["applyAmount"],
            300.0,
        )

    def test_receipt_audit_service_process_prepared_receipt_skips_deduction_for_failed_invoice(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-FAIL-001",
            "serviceData": {"auditInfo": {"applyAmount": 1000.0}},
            "receiptContext": {"receiptCode": "REC-FAIL-001"},
            "invoiceCount": 2,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "serviceData": {
                            "auditInfo": {"applyAmount": 1000.0},
                            "auditInvoiceFile": {"fid": "FID-001"},
                        }
                    },
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {"fid": "FID-002"},
                    "preparedInput": {
                        "serviceData": {
                            "auditInfo": {"applyAmount": 1000.0},
                            "auditInvoiceFile": {"fid": "FID-002"},
                        }
                    },
                },
            ],
        }

        class FailingThenSuccessGraphRuntimeClient:
            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_path, graph_content
                fid = prepared_input["serviceData"]["auditInvoiceFile"]["fid"]
                if fid == "FID-001":
                    return {
                        "receiptCode": "REC-FAIL-001",
                        "decisionOutput": {
                            "checkStatus": "failed",
                            "message": "ocr error",
                        },
                        "preparedInput": prepared_input,
                    }
                return {
                    "receiptCode": "REC-FAIL-001",
                    "decisionOutput": {
                        "checkStatus": "passed",
                        "message": "ok",
                        "invoice_finalAmount": 300.0,
                    },
                    "preparedInput": prepared_input,
                }

        service = ReceiptAuditService(
            graph_runtime_client=FailingThenSuccessGraphRuntimeClient(),
            data_preparer=MagicMock(),
        )

        result = service.process_prepared_receipt(prepared_receipt)

        self.assertEqual(result["summary"]["overallStatus"], "SUCCESS")
        invoice_results = result["invoiceResults"]
        self.assertEqual(len(invoice_results), 2)

        self.assertEqual(
            invoice_results[0]["preparedInput"]["serviceData"]["auditInfo"]["applyAmount"],
            1000.0,
        )
        self.assertEqual(
            invoice_results[1]["preparedInput"]["serviceData"]["auditInfo"]["applyAmount"],
            1000.0,
        )

    def test_receipt_audit_service_process_prepared_receipt_skips_deduction_for_rejected_invoice(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-REJECT-001",
            "serviceData": {"auditInfo": {"applyAmount": 1000.0}},
            "receiptContext": {"receiptCode": "REC-REJECT-001"},
            "invoiceCount": 2,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "serviceData": {
                            "auditInfo": {"applyAmount": 1000.0},
                            "auditInvoiceFile": {"fid": "FID-001"},
                        }
                    },
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {"fid": "FID-002"},
                    "preparedInput": {
                        "serviceData": {
                            "auditInfo": {"applyAmount": 1000.0},
                            "auditInvoiceFile": {"fid": "FID-002"},
                        }
                    },
                },
            ],
        }

        class RejectingGraphRuntimeClient:
            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_path, graph_content
                fid = prepared_input["serviceData"]["auditInvoiceFile"]["fid"]
                if fid == "FID-001":
                    return {
                        "receiptCode": "REC-REJECT-001",
                        "decisionOutput": {
                            "checkStatus": "failed",
                            "message": "amount exceeded",
                            "invoice_finalAmount": 1200.0,
                        },
                        "preparedInput": prepared_input,
                    }
                return {
                    "receiptCode": "REC-REJECT-001",
                    "decisionOutput": {
                        "checkStatus": "passed",
                        "message": "ok",
                        "invoice_finalAmount": 300.0,
                    },
                    "preparedInput": prepared_input,
                }

        service = ReceiptAuditService(
            graph_runtime_client=RejectingGraphRuntimeClient(),
            data_preparer=MagicMock(),
        )

        result = service.process_prepared_receipt(prepared_receipt)

        self.assertEqual(result["summary"]["overallStatus"], "SUCCESS")
        invoice_results = result["invoiceResults"]
        self.assertEqual(len(invoice_results), 2)

        self.assertEqual(
            invoice_results[0]["preparedInput"]["serviceData"]["auditInfo"]["applyAmount"],
            1000.0,
        )
        self.assertEqual(
            invoice_results[1]["preparedInput"]["serviceData"]["auditInfo"]["applyAmount"],
            1000.0,
        )

    @patch("expense_audit_orchestrator.bootstrap.create_kingdee_ocr_provider_from_env")
    def test_orchestrator_runs_custom_receipt_sink_before_real_writeback(
        self,
        mock_create_kingdee_provider,
    ) -> None:
        mock_create_kingdee_provider.return_value = MagicMock(return_value={"invoiceType": "26"})

        class SuccessfulGraphRuntimeClient:
            def evaluate(self, *, prepared_input: dict[str, Any], graph_path=None, graph_content=None) -> dict[str, Any]:
                del graph_path, graph_content
                return {
                    "receiptCode": "REC-WRITEBACK-ORDER-001",
                    "decisionOutput": {"checkStatus": "passed", "message": "ok"},
                    "preparedInput": prepared_input,
                }

        class FailingWritebackClient:
            def save_result_audit_info(self, payload: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError(f"writeback failed for {payload['instanceCode']}")

        published_receipts: list[dict[str, Any]] = []

        def capture_receipt_result(receipt_result: dict[str, Any]) -> None:
            published_receipts.append(receipt_result)

        service = create_orchestrator_service(
            graph_runtime_client=SuccessfulGraphRuntimeClient(),
            receipt_result_sink=capture_receipt_result,
            enable_writeback=True,
            writeback_client=FailingWritebackClient(),
        )

        prepared_receipt = {
            "receiptCode": "REC-WRITEBACK-ORDER-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-WRITEBACK-ORDER-001"}},
            "receiptContext": {"receiptCode": "REC-WRITEBACK-ORDER-001"},
            "invoiceCount": 1,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "items": [{"goodsName": "*电信服务*通信服务费"}],
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "currentInvoiceInfo": {"aiiid": "AIIID-001"},
                            "currentAuditInvoiceFile": {
                                "aifid": "AIFID-001",
                                "fid": "FID-001",
                                "fileName": "origin.pdf",
                                "aiid": "AIID-001",
                            },
                            "ocrEnvelope": {
                                "upload": {"fileDownUrl": "https://files.example/FID-001.pdf"},
                                "recognition": {"rawPayload": {"data": {"invoiceNo": "INV-001"}}},
                                "status": {
                                    "code": "200",
                                    "message": "success",
                                    "finishedAt": "2026-06-16T12:00:00+00:00",
                                },
                            },
                        },
                    },
                }
            ],
        }

        with self.assertRaises(RuntimeError) as context:
            service.process_prepared_receipt(prepared_receipt)

        self.assertIn("writeback failed", str(context.exception))
        self.assertEqual(len(published_receipts), 1)
        self.assertEqual(published_receipts[0]["receiptCode"], "REC-WRITEBACK-ORDER-001")

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    @patch("expense_audit_orchestrator.bootstrap.create_kingdee_ocr_provider_from_env")
    def test_orchestrator_binds_field_mapping_provider_to_audit_service_url(
        self,
        mock_create_kingdee_provider,
        mock_urlopen,
    ) -> None:
        mock_create_kingdee_provider.return_value = MagicMock(return_value={"invoiceType": "26"})
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "status": "200",
                "err": "操作成功",
                "data": [],
            }
        )

        service = create_orchestrator_service(
            audit_service_url="https://service.example",
            graph_runtime_client=MagicMock(),
        )

        service._data_preparer.field_mappings_provider("bill")

        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(
            request.full_url,
            "https://service.example/api/audit-service/audit/fie-id-mapping/bill",
        )

    def test_load_decision_reuses_compiled_graph_for_same_path(self) -> None:
        graph_path = DEFAULT_GRAPH_PATH

        first = load_decision(graph_path)
        second = load_decision(graph_path)

        self.assertIs(first, second)

    def test_load_decision_from_content_reuses_compiled_graph_for_same_content(self) -> None:
        graph_content = json.loads(DEFAULT_GRAPH_PATH.read_text(encoding="utf-8"))

        first = load_decision_from_content(graph_content)
        second = load_decision_from_content(graph_content)

        self.assertIs(first, second)

    def test_main_app_does_not_expose_evaluate_receipt_endpoint(self) -> None:
        client = TestClient(create_graph_runtime_app())

        response = client.post(
            "/api/v1/expense-audits/evaluations",
            json={
                "receiptCode": "REC-003",
                "includePreparedInput": True,
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_main_app_exposes_health_endpoint(self) -> None:
        client = TestClient(create_graph_runtime_app())

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_api_exposes_llm_evaluate_endpoint(self) -> None:
        captured: dict[str, Any] = {}
        upstream_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "passed": True,
                                "risk_level": "low",
                                "reasons": ["字段匹配"],
                                "score": 98,
                                "raw_label": "pass",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        class FakeLlmResponse:
            status_code = 200
            text = json.dumps(upstream_payload, ensure_ascii=False)

            def json(self) -> dict[str, Any]:
                return upstream_payload

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                captured["timeout"] = kwargs.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def post(self, url: str, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return FakeLlmResponse()

        with patch.dict(
            "os.environ",
            {
                "LLM_API_KEY": "test-key",
                "LLM_BASE_URL": "https://llm.example/v1",
                "LLM_MODEL": "audit-model",
            },
            clear=False,
        ):
            with patch("node_gateway.api.httpx.AsyncClient", FakeAsyncClient):
                client = TestClient(create_node_gateway_app())
                response = client.post(
                    NODE_GATEWAY_LLM_EVALUATE_PATH,
                    json={
                        "prompt": "请根据上游 prompt 判断是否通过",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["llmStatus"], "success")
        self.assertTrue(response.json()["llmResult"]["passed"])
        self.assertEqual(response.json()["llmResult"]["risk_level"], "low")
        self.assertEqual(captured["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(captured["json"]["model"], "audit-model")
        self.assertEqual(captured["json"]["messages"][1]["content"], "请根据上游 prompt 判断是否通过")

    def test_api_exposes_llm_evaluate_endpoint_using_dotenv_defaults(self) -> None:
        captured: dict[str, Any] = {}
        upstream_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "passed": True,
                                "risk_level": "medium",
                                "reasons": ["dotenv 生效"],
                                "score": 88,
                                "raw_label": "review",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        class FakeLlmResponse:
            status_code = 200
            text = json.dumps(upstream_payload, ensure_ascii=False)

            def json(self) -> dict[str, Any]:
                return upstream_payload

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                captured["timeout"] = kwargs.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def post(self, url: str, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return FakeLlmResponse()

        env_path = Path(__file__).resolve().with_name(".env")
        original_content = env_path.read_text(encoding="utf-8") if env_path.exists() else None
        env_path.write_text(
            "LLM_API_KEY=dotenv-key\n"
            "LLM_BASE_URL=https://dotenv.example/v1\n"
            "LLM_MODEL=dotenv-model\n",
            encoding="utf-8",
        )

        try:
            with patch.dict("os.environ", {}, clear=False):
                for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
                    os.environ.pop(key, None)

                with patch("node_gateway.api.httpx.AsyncClient", FakeAsyncClient):
                    client = TestClient(create_node_gateway_app())
                    response = client.post(
                        NODE_GATEWAY_LLM_EVALUATE_PATH,
                        json={
                            "prompt": "请从 .env 读取模型配置",
                        },
                    )
        finally:
            if original_content is None:
                env_path.unlink(missing_ok=True)
            else:
                env_path.write_text(original_content, encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["llmStatus"], "success")
        self.assertEqual(captured["url"], "https://dotenv.example/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer dotenv-key")
        self.assertEqual(captured["json"]["model"], "dotenv-model")

    def test_api_exposes_llm_evaluate_endpoint_accepts_non_json_request_bodies(self) -> None:
        captured_calls: list[dict[str, Any]] = []
        upstream_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "passed": True,
                                "risk_level": "low",
                                "reasons": ["body parsed"],
                                "score": 91,
                                "raw_label": "pass",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        class FakeLlmResponse:
            status_code = 200
            text = json.dumps(upstream_payload, ensure_ascii=False)

            def json(self) -> dict[str, Any]:
                return upstream_payload

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                del args, kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def post(self, url: str, headers=None, json=None):
                captured_calls.append({"url": url, "headers": headers, "json": json})
                return FakeLlmResponse()

        with patch.dict(
            "os.environ",
            {
                "LLM_API_KEY": "test-key",
                "LLM_BASE_URL": "https://llm.example/v1",
                "LLM_MODEL": "audit-model",
            },
            clear=False,
        ):
            with patch("node_gateway.api.httpx.AsyncClient", FakeAsyncClient):
                client = TestClient(create_node_gateway_app())

                form_response = client.post(
                    NODE_GATEWAY_LLM_EVALUATE_PATH,
                    data={"prompt": "form prompt"},
                )
                raw_response = client.request(
                    "POST",
                    NODE_GATEWAY_LLM_EVALUATE_PATH,
                    content='{"prompt":"raw prompt"}',
                )

        self.assertEqual(form_response.status_code, 200)
        self.assertEqual(form_response.json()["llmStatus"], "success")
        self.assertEqual(raw_response.status_code, 200)
        self.assertEqual(raw_response.json()["llmStatus"], "success")
        self.assertEqual(captured_calls[0]["json"]["messages"][1]["content"], "form prompt")
        self.assertEqual(captured_calls[1]["json"]["messages"][1]["content"], "raw prompt")

    def test_api_exposes_llm_evaluate_endpoint_retries_when_result_format_invalid(self) -> None:
        call_count = 0

        class FakeLlmResponse:
            def __init__(self, content: str) -> None:
                self.status_code = 200
                self._payload = {"choices": [{"message": {"content": content}}]}
                self.text = json.dumps(self._payload, ensure_ascii=False)

            def json(self) -> dict[str, Any]:
                return self._payload

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                del args, kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def post(self, url: str, headers=None, json=None):
                del url, headers, json
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return FakeLlmResponse('{"passed":"true"}')
                return FakeLlmResponse('{"passed":true,"finalAmount":12.3}')

        with patch.dict(
            "os.environ",
            {
                "LLM_API_KEY": "test-key",
                "LLM_BASE_URL": "https://llm.example/v1",
                "LLM_MODEL": "audit-model",
            },
            clear=False,
        ):
            with patch("node_gateway.api.httpx.AsyncClient", FakeAsyncClient):
                client = TestClient(create_node_gateway_app())
                response = client.post(
                    NODE_GATEWAY_LLM_EVALUATE_PATH,
                    json={
                        "prompt": "请返回审计结论",
                        "maxRetries": 1,
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["llmStatus"], "success")
        self.assertTrue(response.json()["llmResult"]["passed"])
        self.assertEqual(call_count, 2)

    def test_api_exposes_llm_evaluate_endpoint_returns_error_after_retry_exhausted(self) -> None:
        call_count = 0

        class FakeLlmResponse:
            status_code = 200
            text = '{"choices":[{"message":{"content":"{\\"finalAmount\\":\\"abc\\"}"}}]}'

            def json(self) -> dict[str, Any]:
                return {"choices": [{"message": {"content": '{"finalAmount":"abc"}'}}]}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                del args, kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def post(self, url: str, headers=None, json=None):
                del url, headers, json
                nonlocal call_count
                call_count += 1
                return FakeLlmResponse()

        with patch.dict(
            "os.environ",
            {
                "LLM_API_KEY": "test-key",
                "LLM_BASE_URL": "https://llm.example/v1",
                "LLM_MODEL": "audit-model",
            },
            clear=False,
        ):
            with patch("node_gateway.api.httpx.AsyncClient", FakeAsyncClient):
                client = TestClient(create_node_gateway_app())
                response = client.post(
                    NODE_GATEWAY_LLM_EVALUATE_PATH,
                    json={
                        "prompt": "请返回审计结论",
                        "maxRetries": 1,
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["llmStatus"], "error")
        self.assertIn("invalid llm result format", response.json()["errorMessage"])
        self.assertIn("retries=1", response.json()["errorMessage"])
        self.assertEqual(call_count, 2)


class KingdeeOCRProviderTests(unittest.TestCase):
    def test_resolve_file_type_maps_supported_suffixes_and_rejects_unsupported_ones(self) -> None:
        from expense_audit_orchestrator.kingdee_ocr import _resolve_file_type

        cases = [
            ("/tmp/invoice.pdf", None, "1"),
            ("https://files.example/invoice.jpeg?token=abc", None, "2"),
            ("base64://PAYLOAD", "invoice.ofd", "4"),
            ("base64://PAYLOAD", "invoice.docx", "5"),
            ("base64://PAYLOAD", "invoice.xlsx", "6"),
            ("base64://PAYLOAD", "invoice.pptx", "7"),
            ("base64://PAYLOAD", "invoice.txt", "8"),
            ("base64://PAYLOAD", "invoice.xml", "9"),
        ]

        for file_path, file_name, expected in cases:
            with self.subTest(file_path=file_path, file_name=file_name):
                self.assertEqual(_resolve_file_type(file_path, file_name=file_name), expected)

        with self.assertRaisesRegex(ValueError, "unsupported"):
            _resolve_file_type("/tmp/invoice.zip")

    def test_create_kingdee_ocr_provider_from_env_requires_mandatory_config(self) -> None:
        from expense_audit_orchestrator.kingdee_ocr import create_kingdee_ocr_provider_from_env

        with patch.dict("os.environ", {}, clear=False):
            for key in (
                "KINGDEE_OCR_BASE_URL",
                "KINGDEE_OCR_APP_ID",
                "KINGDEE_OCR_APP_SECRET",
                "KINGDEE_OCR_ACCOUNT_ID",
                "KINGDEE_OCR_TENANT_ID",
                "KINGDEE_OCR_USER",
            ):
                os.environ.pop(key, None)

            with patch("expense_audit_orchestrator.kingdee_ocr.load_dotenv"):
                with self.assertRaisesRegex(ValueError, "KINGDEE_OCR_BASE_URL"):
                    create_kingdee_ocr_provider_from_env()

    def test_create_kingdee_ocr_provider_from_env_loads_project_dotenv(self) -> None:
        from expense_audit_orchestrator.kingdee_ocr import create_kingdee_ocr_provider_from_env

        env_path = Path(__file__).resolve().with_name(".env")
        original_content = env_path.read_text(encoding="utf-8") if env_path.exists() else None
        env_path.write_text(
            "KINGDEE_OCR_BASE_URL=https://dotenv-kingdee.example.com\n"
            "KINGDEE_OCR_APP_ID=dotenv-app\n"
            "KINGDEE_OCR_APP_SECRET=dotenv-secret\n"
            "KINGDEE_OCR_ACCOUNT_ID=dotenv-account\n"
            "KINGDEE_OCR_TENANT_ID=dotenv-tenant\n"
            "KINGDEE_OCR_USER=dotenv-user\n"
            "KINGDEE_OCR_TIMEOUT=12.5\n"
            "KINGDEE_OCR_UPLOAD_FILE_TYPE=1\n",
            encoding="utf-8",
        )

        try:
            with patch.dict("os.environ", {}, clear=False):
                for key in (
                    "KINGDEE_OCR_BASE_URL",
                    "KINGDEE_OCR_APP_ID",
                    "KINGDEE_OCR_APP_SECRET",
                    "KINGDEE_OCR_ACCOUNT_ID",
                    "KINGDEE_OCR_TENANT_ID",
                    "KINGDEE_OCR_USER",
                    "KINGDEE_OCR_TIMEOUT",
                    "KINGDEE_OCR_UPLOAD_FILE_TYPE",
                ):
                    os.environ.pop(key, None)

                provider = create_kingdee_ocr_provider_from_env()
        finally:
            if original_content is None:
                env_path.unlink(missing_ok=True)
            else:
                env_path.write_text(original_content, encoding="utf-8")

        self.assertEqual(provider._base_url, "https://dotenv-kingdee.example.com")
        self.assertEqual(provider._app_id, "dotenv-app")
        self.assertEqual(provider._account_id, "dotenv-account")
        self.assertEqual(provider._user, "dotenv-user")
        self.assertEqual(provider._timeout, 12.5)
        self.assertEqual(provider._upload_file_type, "1")

    def test_kingdee_ocr_provider_returns_ocr_envelope_with_normalized_result(self) -> None:
        import base64

        from expense_audit_orchestrator.kingdee_ocr import KingdeeOCRProvider

        requests: list[tuple[str, dict[str, Any]]] = []

        class FakeHttpxResponse:
            def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
                self._payload = payload
                self.status_code = status_code
                self.text = json.dumps(payload, ensure_ascii=False)

            def json(self) -> dict[str, Any]:
                return self._payload

        class FakeHttpxClient:
            def __init__(self, responses: list[FakeHttpxResponse]) -> None:
                self._responses = responses

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url: str, json: dict[str, Any]) -> FakeHttpxResponse:
                requests.append((url, json))
                if not self._responses:
                    raise AssertionError("unexpected extra request")
                return self._responses.pop(0)

        responses = [
            FakeHttpxResponse(
                {
                    "data": {
                        "appToken": "APP-TOKEN",
                        "expireTime": 1774318362203,
                    },
                    "status": True,
                }
            ),
            FakeHttpxResponse(
                {
                    "data": {
                        "accessToken": "ACCESS-TOKEN",
                        "expireTime": 1774318362304,
                    },
                    "status": True,
                }
            ),
            FakeHttpxResponse(
                {
                    "data": {
                        "fileDownUrl": "21e4d333b1823c01",
                    },
                    "errorCode": "0000",
                    "status": True,
                }
            ),
            FakeHttpxResponse(
                {
                    "data": {
                        "ocrResult": {
                            "invoiceType": "26",
                            "orgName": "锐捷网络股份有限公司",
                            "invoiceNo": "26357000000141826844",
                            "buyerTaxNo": "91110108668444162H",
                        }
                    },
                    "status": True,
                }
            ),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            invoice_path = Path(temp_dir) / "invoice.pdf"
            invoice_path.write_bytes(b"invoice-binary")

            with patch(
                "expense_audit_orchestrator.kingdee_ocr.httpx.Client",
                return_value=FakeHttpxClient(responses),
            ):
                provider = KingdeeOCRProvider(
                    base_url="https://kingdee.example.com",
                    app_id="RJ_EMS",
                    app_secret="secret",
                    account_id="19xx82873856",
                    tenant_id="ruxxxxxx",
                    user="RJ_EMS",
                    user_type="UserName",
                    language="zh-CN",
                    bill_type="er_dailyreimbursebill",
                )

                result = provider(
                    str(invoice_path),
                    main.DEFAULT_OCR_PATH,
                    receipt_code="REC-001",
                    audit_info={
                        "verifiUserCompanyName": "锐捷网络股份有限公司",
                    },
                    company_list=[
                        {
                            "cName": "锐捷网络股份有限公司",
                            "companyTax": "91110108668444162H",
                        }
                    ],
                )

        self.assertEqual(result["provider"], "kingdee")
        self.assertEqual(result["request"]["receiptCode"], "REC-001")
        self.assertEqual(result["upload"]["fileType"], "1")
        self.assertEqual(result["upload"]["fileDownUrl"], "21e4d333b1823c01")
        self.assertEqual(
            result["recognition"]["normalized"],
            {
                "invoiceType": "26",
                "orgName": "锐捷网络股份有限公司",
                "invoiceNo": "26357000000141826844",
                "buyerTaxNo": "91110108668444162H",
            },
        )
        self.assertTrue(result["recognition"]["rawPayload"]["status"])
        self.assertEqual(result["status"]["code"], "200")
        self.assertEqual(
            [url for url, _ in requests],
            [
                "https://kingdee.example.com/api/getAppToken.do",
                "https://kingdee.example.com/api/login.do",
                "https://kingdee.example.com/kapi/app/rim/message?access_token=ACCESS-TOKEN",
                "https://kingdee.example.com/kapi/app/rim/message?access_token=ACCESS-TOKEN",
            ],
        )
        self.assertEqual(requests[0][1]["appId"], "RJ_EMS")
        self.assertEqual(requests[1][1]["apptoken"], "APP-TOKEN")
        self.assertEqual(requests[2][1]["messageType"], "uploadFile")
        self.assertEqual(
            requests[2][1]["data"]["base64"],
            base64.b64encode(b"invoice-binary").decode("ascii"),
        )
        self.assertEqual(requests[3][1]["messageType"], "recognitionCheck")
        self.assertEqual(requests[3][1]["data"]["companyInfo"]["name"], "锐捷网络股份有限公司")
        self.assertEqual(requests[3][1]["data"]["companyInfo"]["taxNo"], "91110108668444162H")


class ProfileRoutingTests(unittest.TestCase):
    """动态路由模式（profile_resolver）集成测试。"""

    def _build_data_preparer_with_audit_info(self, audit_info: dict[str, Any]) -> FakeSplitDataPreparer:
        """构造一个 FakeSplitDataPreparer，其 audit_info_provider 返回指定 audit_info。"""
        receipt_context = {
            "receiptCode": "REC-ROUTE-001",
            "serviceData": {"auditInfo": audit_info},
            "invoiceFiles": [
                {
                    "fid": "FID-ROUTE-001",
                    "filePath": "base64://BASE64",
                    "auditInvoiceFile": {"fid": "FID-ROUTE-001"},
                    "auditInvoiceFileInfo": {"fid": "FID-ROUTE-001"},
                },
            ],
        }
        preparer = FakeSplitDataPreparer(
            receipt_context,
            {"FID-ROUTE-001": {"receipt": {"code": "REC-ROUTE-001"}}},
        )
        # 让 audit_info_provider 返回带 eiCode 的 audit_info（供 _resolve_profile_for_receipt 使用）
        preparer.audit_info_provider = lambda receipt_code: audit_info
        return preparer

    def test_dynamic_routing_resolves_profile_by_ei_code(self) -> None:
        from expense_audit_orchestrator.profiles import ProfileResolver

        audit_info = {"instanceCode": "REC-ROUTE-001", "eiCode": "EI001"}
        preparer = self._build_data_preparer_with_audit_info(audit_info)

        resolver = ProfileResolver(ei_code_map={"EI001": "telecom", "EI002": "travel"})
        runtime_client = MagicMock()
        service = ReceiptAuditService(
            graph_runtime_client=runtime_client,
            data_preparer=preparer,
            profile_resolver=resolver,
        )

        result = service.prepare_receipt("REC-ROUTE-001")

        # resolvedProfile 应被存入 prepared_receipt
        self.assertIsNotNone(result.get("resolvedProfile"))
        self.assertEqual(result["resolvedProfile"].name, "telecom")

    def test_dynamic_routing_unknown_ei_code_raises(self) -> None:
        from expense_audit_orchestrator.profiles import (
            ProfileResolver,
            UnknownExpenseTypeError,
        )

        audit_info = {"instanceCode": "REC-ROUTE-002", "eiCode": "EI999"}
        preparer = self._build_data_preparer_with_audit_info(audit_info)

        resolver = ProfileResolver(ei_code_map={"EI001": "telecom"})
        runtime_client = MagicMock()
        service = ReceiptAuditService(
            graph_runtime_client=runtime_client,
            data_preparer=preparer,
            profile_resolver=resolver,
        )

        with self.assertRaises(UnknownExpenseTypeError):
            service.prepare_receipt("REC-ROUTE-002")

    def test_dynamic_routing_uses_profile_graph_path(self) -> None:
        """动态路由下，process_prepared_receipt 应使用 resolved profile 的 graph_path。"""
        from expense_audit_orchestrator.profiles import ProfileResolver

        # 构造一个 prepared_receipt，含 resolvedProfile（模拟 prepare_receipt 的输出）
        custom_profile = ExpenseProfile(
            name="telecom",
            default_graph_path="/custom/path/graph.json",
        )
        prepared_receipt = {
            "receiptCode": "REC-ROUTE-003",
            "serviceData": {"auditInfo": {"instanceCode": "REC-ROUTE-003"}},
            "receiptContext": {"receiptCode": "REC-ROUTE-003"},
            "invoiceCount": 1,
            "resolvedProfile": custom_profile,
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-003",
                    "invoiceFile": {"fid": "FID-003"},
                    "preparedInput": {"receipt": {"code": "REC-ROUTE-003"}},
                },
            ],
        }

        runtime_client = MagicMock()
        runtime_client.evaluate.return_value = {
            "decisionOutput": {"checkStatus": "passed"},
            "receiptCode": "REC-ROUTE-003",
        }
        service = ReceiptAuditService(
            graph_runtime_client=runtime_client,
            data_preparer=FakeSplitDataPreparer({}, {}),
            profile_resolver=ProfileResolver(ei_code_map={"EI001": "telecom"}),
        )

        service.process_prepared_receipt(prepared_receipt)

        # evaluate 应被调用，且 graph_path 为 resolved profile 的 default_graph_path
        runtime_client.evaluate.assert_called_once()
        call_kwargs = runtime_client.evaluate.call_args.kwargs
        self.assertEqual(call_kwargs["graph_path"], "/custom/path/graph.json")

    def test_profile_resolver_and_graph_path_are_mutually_exclusive(self) -> None:
        from expense_audit_orchestrator.profiles import ProfileResolver

        resolver = ProfileResolver(ei_code_map={"EI001": "telecom"})
        with self.assertRaises(ValueError):
            ReceiptAuditService(
                graph_runtime_client=MagicMock(),
                data_preparer=FakeSplitDataPreparer({}, {}),
                graph_path="/some/graph.json",
                profile_resolver=resolver,
            )

    def test_dynamic_routing_different_ei_codes_select_different_profiles(self) -> None:
        """端到端：不同 eiCode 路由到不同 profile，使用不同图路径。"""
        from expense_audit_orchestrator.profiles import (
            ENTERTAINMENT_GRAPH_PATH,
            PERSONAL_TRANSPORT_GRAPH_PATH,
            ProfileResolver,
        )

        resolver = ProfileResolver(
            ei_code_map={
                "EI001": "telecom",
                "EI024": "personal_transport",
                "EI003": "entertainment",
            }
        )

        # EI001 -> telecom profile
        telecom_preparer = self._build_data_preparer_with_audit_info(
            {"instanceCode": "R1", "eiCode": "EI001"}
        )
        service1 = ReceiptAuditService(
            graph_runtime_client=MagicMock(),
            data_preparer=telecom_preparer,
            profile_resolver=resolver,
        )
        result1 = service1.prepare_receipt("R1")
        self.assertEqual(result1["resolvedProfile"].name, "telecom")

        # EI024 -> personal_transport profile（图路径应为个人交通费图）
        personal_transport_preparer = self._build_data_preparer_with_audit_info(
            {"instanceCode": "R2", "eiCode": "EI024"}
        )
        service2 = ReceiptAuditService(
            graph_runtime_client=MagicMock(),
            data_preparer=personal_transport_preparer,
            profile_resolver=resolver,
        )
        result2 = service2.prepare_receipt("R2")
        self.assertEqual(result2["resolvedProfile"].name, "personal_transport")
        self.assertEqual(
            result2["resolvedProfile"].default_graph_path, PERSONAL_TRANSPORT_GRAPH_PATH
        )

        # EI003 -> entertainment profile（图路径应为招待费图）
        entertainment_preparer = self._build_data_preparer_with_audit_info(
            {"instanceCode": "R3", "eiCode": "EI003"}
        )
        service3 = ReceiptAuditService(
            graph_runtime_client=MagicMock(),
            data_preparer=entertainment_preparer,
            profile_resolver=resolver,
        )
        result3 = service3.prepare_receipt("R3")
        self.assertEqual(result3["resolvedProfile"].name, "entertainment")
        self.assertEqual(result3["resolvedProfile"].default_graph_path, ENTERTAINMENT_GRAPH_PATH)

    def test_employee_context_no_longer_hardcoded(self) -> None:
        """context.employee 应从 auditInfo 提取，不再硬编码假数据。"""
        from expense_audit_orchestrator.core import build_rule_input

        service_data = {
            "auditInfo": {
                "instanceCode": "R-EMP-001",
                "verifiUserName": "刘雪涛",
                "verifiStaffNo": "R06108",
            }
        }
        result = build_rule_input(
            "R-EMP-001",
            {"invoiceType": "26"},
            file_path="base64://x",
            service_data=service_data,
        )
        employee = result["context"]["employee"]
        self.assertEqual(employee["name"], "刘雪涛")
        self.assertEqual(employee["staffNo"], "R06108")
        # 不应包含硬编码的假数据
        self.assertNotIn("department", employee)
        self.assertNotIn("level", employee)

    def test_employee_context_empty_when_no_audit_info(self) -> None:
        """auditInfo 为空时，context.employee 应为空 dict 而非假数据。"""
        from expense_audit_orchestrator.core import build_rule_input

        result = build_rule_input(
            "R-EMP-002",
            {"invoiceType": "26"},
            file_path="base64://x",
            service_data={},
        )
        self.assertEqual(result["context"]["employee"], {})


if __name__ == "__main__":
    unittest.main()