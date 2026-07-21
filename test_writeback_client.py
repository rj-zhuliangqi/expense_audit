import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from expense_audit_orchestrator.profiles.telecom.writeback import telecom_compliance_rule
from expense_audit_orchestrator.writeback_client import (
    AuditInfoWritebackClient,
    build_receipt_writeback_file_sink,
    build_receipt_writeback_sink,
)


class FakeHttpResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class WritebackClientTests(unittest.TestCase):
    def test_receipt_writeback_file_sink_exports_payload(self) -> None:
        receipt_result = {
            "receiptCode": "REC-WRITEBACK-FILE-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-WRITEBACK-FILE-001"},
                "auditInvoiceFiles": [
                    {
                        "aifid": "AIFID-001",
                        "fid": "FID-001",
                        "fileName": "origin.pdf",
                        "aiid": "AIID-001",
                    }
                ],
            },
            "receiptContext": {"receiptCode": "REC-WRITEBACK-FILE-001"},
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "aifid": "AIFID-001",
                            "fid": "FID-001",
                            "fileName": "origin.pdf",
                            "aiid": "AIID-001",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
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
                                "aifid": "AIFID-001",
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
                                "status": {
                                    "code": "200",
                                    "message": "success",
                                    "finishedAt": "2026-06-16T12:00:00+00:00",
                                },
                            },
                        },
                    },
                    "decisionOutput": {"checkStatus": "passed", "message": "ok"},
                    "decisionStatus": "passed",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            sink = build_receipt_writeback_file_sink(
                temp_dir,
                compliance_rule=telecom_compliance_rule,
            )

            sink(receipt_result)

            output_file = Path(temp_dir) / "REC-WRITEBACK-FILE-001.writeback-payload.json"
            self.assertTrue(output_file.exists())
            payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["instanceCode"], "REC-WRITEBACK-FILE-001")
            self.assertEqual(payload["auditInvoiceFiles"][0]["type"], 1)
            self.assertTrue(payload["auditInvoiceInfoContents"][0]["aiicid"])
            self.assertEqual(payload["auditInvoiceInfos"][0]["atcrid"], "ATCRID-001")
            self.assertEqual(
                [c["compliance"] for c in payload["auditInvoiceInfoContents"]],
                [True, False],
            )
            self.assertEqual(len(payload["auditTruthCheckResultBills"]), 1)
            self.assertEqual(len(payload["auditTruthCheckResultItems"]), 1)
            self.assertEqual(len(payload["auditTruthCheckResultItemCols"]), 1)
            self.assertTrue(payload["auditTruthCheckResultBills"][0]["atcrbid"])
            self.assertTrue(payload["auditTruthCheckResultItems"][0]["atcriid"])
            self.assertTrue(payload["auditTruthCheckResultItemCols"][0]["atcricid"])
            self.assertTrue(all("code" not in item for item in payload["auditTruthCheckResultItemCols"]))

    @patch("expense_audit_orchestrator.writeback_client.urlopen")
    def test_receipt_writeback_sink_assembles_and_posts_payload(
        self,
        mock_urlopen,
    ) -> None:
        mock_urlopen.return_value = FakeHttpResponse({"code": 0, "message": "success", "data": True})
        client = AuditInfoWritebackClient(service_url="https://service.example")
        sink = build_receipt_writeback_sink(client)

        receipt_result = {
            "receiptCode": "REC-WRITEBACK-CHAIN-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-WRITEBACK-CHAIN-001"},
                "auditInvoiceFiles": [
                    {
                        "aifid": "AIFID-001",
                        "fid": "FID-001",
                        "fileName": "origin.pdf",
                        "aiid": "AIID-001",
                    }
                ],
            },
            "receiptContext": {"receiptCode": "REC-WRITEBACK-CHAIN-001"},
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "aifid": "AIFID-001",
                            "fid": "FID-001",
                            "fileName": "origin.pdf",
                            "aiid": "AIID-001",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
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
                    "decisionOutput": {
                        "amount_result": {
                            "audit_content": "检查使用发票合计金额是否充足",
                            "audit_type": "general-rules",
                            "distinguish_content": "金额不足",
                            "distinguish_result": "REJECT",
                            "instance_code": "REC-WRITEBACK-CHAIN-001",
                            "invoice_file_id": "AIFID-001",
                            "invoice_info_id": "AIIID-001",
                            "message": "金额不够",
                            "reason_code": "E31",
                        }
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        sink(receipt_result)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://service.example/api/audit-service/audit/audit-info-save")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["instanceCode"], "REC-WRITEBACK-CHAIN-001")
        self.assertEqual(payload["auditInvoiceFiles"][0]["fid"], "FID-001")
        self.assertTrue(payload["auditTruthCheckLogs"][0]["atclid"])

    @patch.dict(
        "os.environ",
        {
            "WRITEBACK_MAX_RETRIES": "2",
            "WRITEBACK_RETRY_BACKOFF_SECONDS": "0",
        },
        clear=False,
    )
    @patch("expense_audit_orchestrator.writeback_client.urlopen")
    def test_save_result_audit_info_retries_timeout_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            URLError(TimeoutError("timed out")),
            FakeHttpResponse({"code": 0, "message": "success", "data": True}),
        ]

        client = AuditInfoWritebackClient(service_url="https://service.example")
        response = client.save_result_audit_info({"instanceCode": "REC-RETRY-001"})

        self.assertEqual(response, {"code": 0, "message": "success", "data": True})
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch.dict(
        "os.environ",
        {
            "WRITEBACK_MAX_RETRIES": "1",
            "WRITEBACK_RETRY_BACKOFF_SECONDS": "0",
        },
        clear=False,
    )
    @patch("expense_audit_orchestrator.writeback_client.urlopen")
    def test_save_result_audit_info_drops_ai_advice_once_on_500(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            HTTPError(
                "https://service.example/api/audit-service/audit/audit-info-save",
                500,
                "Internal Server Error",
                None,
                io.BytesIO(b"{\"message\":\"boom\"}"),
            ),
            FakeHttpResponse({"code": 0, "message": "success", "data": True}),
        ]

        client = AuditInfoWritebackClient(service_url="https://service.example")
        response = client.save_result_audit_info(
            {
                "instanceCode": "REC-FALLBACK-001",
                "aiAuditAdvice": "too long advice",
            }
        )

        self.assertEqual(response, {"code": 0, "message": "success", "data": True})
        self.assertEqual(mock_urlopen.call_count, 2)
        second_request = mock_urlopen.call_args_list[1].args[0]
        second_payload = json.loads(second_request.data.decode("utf-8"))
        self.assertEqual(second_payload["instanceCode"], "REC-FALLBACK-001")
        self.assertNotIn("aiAuditAdvice", second_payload)


if __name__ == "__main__":
    unittest.main()