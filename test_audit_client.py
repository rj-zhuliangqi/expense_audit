import json
import io
import os
import unittest
from hashlib import md5
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from dotenv import dotenv_values

from expense_audit_orchestrator import audit_client


class FakeHttpResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class AuditClientHeaderTests(unittest.TestCase):
    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_field_mappings_requests_bill_mapping_path(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "status": "200",
                "err": "操作成功",
                "data": [
                    {
                        "fieldName": "invoiceNo",
                        "fieldLable": "发票号码",
                        "belongTable": "bill",
                        "status": True,
                    }
                ],
            }
        )

        result = audit_client.fetch_field_mappings(
            "bill",
            service_url="https://service.example",
        )

        self.assertEqual(result[0]["fieldName"], "invoiceNo")
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertTrue(request.full_url.endswith("/api/audit-service/audit/fie-id-mapping/bill"))

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_field_mappings_accepts_item_mapping_status_envelope(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "status": "200",
                "err": "操作成功",
                "data": [
                    {
                        "fieldName": "totalAmount",
                        "fieldLable": "价税合计",
                        "belongTable": "item",
                        "status": True,
                    }
                ],
            }
        )

        result = audit_client.fetch_field_mappings(
            "item",
            service_url="https://service.example",
        )

        self.assertEqual(
            result,
            [
                {
                    "fieldName": "totalAmount",
                    "fieldLable": "价税合计",
                    "belongTable": "item",
                    "status": True,
                }
            ],
        )

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_expense_invoice_types_prefers_company_list_and_falls_back_to_legacy_companylist(
        self,
        mock_urlopen,
    ) -> None:
        primary_url = "https://service.example/api/audit-service/audit/company-list?eiCode=EI001"
        mock_urlopen.side_effect = [
            HTTPError(primary_url, 404, "Not Found", None, io.BytesIO(b"{}")),
            FakeHttpResponse(
                {
                    "code": 0,
                    "message": "success",
                    "data": [{"eiCode": "EI001", "invoiceType": "26"}],
                }
            ),
        ]

        result = audit_client.fetch_expense_invoice_types(
            "EI001",
            service_url="https://service.example",
        )

        self.assertEqual(result, [{"eiCode": "EI001", "invoiceType": "26"}])
        requests = [call.args[0] for call in mock_urlopen.call_args_list]
        request_urls = [request.full_url if isinstance(request, Request) else request for request in requests]
        self.assertTrue(request_urls[0].endswith("/api/audit-service/audit/company-list?eiCode=EI001"))
        self.assertTrue(request_urls[1].endswith("/api/audit-service/audit/companylist?eiCode=EI001"))

    def test_repo_env_uses_current_invoice_secret_key_name(self) -> None:
        env_path = audit_client.PROJECT_ROOT / ".env"
        if not env_path.exists():
            self.skipTest("repo .env file is not present")

        env_values = dotenv_values(env_path)

        self.assertIn("AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET", env_values)

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_files_generates_signature_from_sysid_and_secret(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": [{"fid": "FID-SIGNED-001"}],
            }
        )

        timestamp = 1781234033849
        sysid = "7abd94b75b1442c28814cf3cb4caaf43"
        secret = "EA22325909026BEBB74B0D8658011162"
        expected_digest = md5(f"{sysid}{timestamp}{secret}".encode("utf-8")).hexdigest().upper()

        with patch.dict(
            "os.environ",
            {
                "SYS_ID": sysid,
                "accessKeySecret": secret,
            },
            clear=True,
        ):
            with patch.object(audit_client, "_PROJECT_ENV_LOADED", True):
                with patch.object(
                    audit_client,
                    "_current_timestamp_millis",
                    return_value=timestamp,
                    create=True,
                ):
                    result = audit_client.fetch_audit_invoice_files(
                        "rjw260327000006",
                        service_url="https://service-uate-gw.ruijie.com.cn/api/audit-service",
                    )

        self.assertEqual(result[0]["fid"], "FID-SIGNED-001")
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(
            headers["sign-server-auth"],
            f"{sysid}|{timestamp}|{expected_digest}",
        )
        self.assertEqual(headers["sysid"], sysid)

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_files_loads_header_values_from_dotenv(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": [{"fid": "FID-DOTENV-001"}],
            }
        )

        timestamp = 1781234033849
        sysid = "dotenv-sysid"
        secret = "dotenv-secret"
        expected_digest = md5(f"{sysid}{timestamp}{secret}".encode("utf-8")).hexdigest().upper()

        def populate_env_from_dotenv(*_args, **_kwargs) -> None:
            os.environ["AUDIT_INVOICE_FILE_SYSID"] = sysid
            os.environ["AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET"] = secret

        with patch.dict("os.environ", {}, clear=True):
            with patch.object(audit_client, "_PROJECT_ENV_LOADED", False):
                with patch(
                    "expense_audit_orchestrator.audit_client.load_dotenv",
                    side_effect=populate_env_from_dotenv,
                    create=True,
                ) as mock_load_dotenv:
                    with patch.object(audit_client, "_current_timestamp_millis", return_value=timestamp):
                        result = audit_client.fetch_audit_invoice_files(
                            "rjw260327000006",
                            service_url="https://service-uate-gw.ruijie.com.cn/api/audit-service",
                        )

        self.assertEqual(result[0]["fid"], "FID-DOTENV-001")
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["sign-server-auth"], f"{sysid}|{timestamp}|{expected_digest}")
        self.assertEqual(headers["sysid"], sysid)
        mock_load_dotenv.assert_called_once()

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_files_supports_legacy_secret_env_key(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": [{"fid": "FID-LEGACY-ENV-001"}],
            }
        )

        timestamp = 1781234033849
        sysid = "legacy-dotenv-sysid"
        secret = "legacy-dotenv-secret"
        expected_digest = md5(f"{sysid}{timestamp}{secret}".encode("utf-8")).hexdigest().upper()

        with patch.dict(
            "os.environ",
            {
                "AUDIT_INVOICE_FILE_SYSID": sysid,
                "AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET_ENV": secret,
            },
            clear=True,
        ):
            with patch.object(audit_client, "_PROJECT_ENV_LOADED", True):
                with patch.object(audit_client, "_current_timestamp_millis", return_value=timestamp):
                    result = audit_client.fetch_audit_invoice_files(
                        "rjw260327000006",
                        service_url="https://service-uate-gw.ruijie.com.cn/api/audit-service",
                    )

        self.assertEqual(result[0]["fid"], "FID-LEGACY-ENV-001")
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["sign-server-auth"], f"{sysid}|{timestamp}|{expected_digest}")
        self.assertEqual(headers["sysid"], sysid)

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_files_backfills_empty_env_values_from_dotenv(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": [{"fid": "FID-EMPTY-ENV-001"}],
            }
        )

        timestamp = 1781234033849
        sysid = "dotenv-backfill-sysid"
        secret = "dotenv-backfill-secret"
        expected_digest = md5(f"{sysid}{timestamp}{secret}".encode("utf-8")).hexdigest().upper()

        with patch.dict(
            "os.environ",
            {
                "AUDIT_INVOICE_FILE_SYSID": "",
                "AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET": "",
            },
            clear=True,
        ):
            with patch.object(audit_client, "_PROJECT_ENV_LOADED", False):
                with patch("expense_audit_orchestrator.audit_client.load_dotenv"):
                    with patch(
                        "expense_audit_orchestrator.audit_client.dotenv_values",
                        return_value={
                            "AUDIT_INVOICE_FILE_SYSID": sysid,
                            "AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET": secret,
                        },
                    ):
                        with patch.object(audit_client, "_current_timestamp_millis", return_value=timestamp):
                            result = audit_client.fetch_audit_invoice_files(
                                "rjw260327000006",
                                service_url="https://service-uate-gw.ruijie.com.cn/api/audit-service",
                            )

        self.assertEqual(result[0]["fid"], "FID-EMPTY-ENV-001")
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["sign-server-auth"], f"{sysid}|{timestamp}|{expected_digest}")
        self.assertEqual(headers["sysid"], sysid)

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_files_ignores_static_signature_env_and_generates_dynamic_signature(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": [{"fid": "FID-001"}],
            }
        )

        timestamp = 1781234033849
        sysid = "7abd94b75b1442c28814cf3cb4caaf43"
        secret = "EA22325909026BEBB74B0D8658011162"
        expected_digest = md5(f"{sysid}{timestamp}{secret}".encode("utf-8")).hexdigest().upper()

        with patch.dict(
            "os.environ",
            {
                "AUDIT_INVOICE_FILE_SYSID": sysid,
                "AUDIT_INVOICE_FILE_ACCESS_KEY_SECRET": secret,
                "AUDIT_INVOICE_FILE_SIGN_SERVER_AUTH": "stale-static-signature-must-not-be-used",
            },
            clear=False,
        ):
            with patch.object(audit_client, "_current_timestamp_millis", return_value=timestamp):
                result = audit_client.fetch_audit_invoice_files(
                    "rjw260327000006",
                    service_url="https://service-uate-gw.ruijie.com.cn/api/audit-service",
                )

        self.assertEqual(result[0]["fid"], "FID-001")
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertIn("/audit/invoice-file?", request.full_url)
        self.assertIn("instanceCode=rjw260327000006", request.full_url)
        self.assertIn("aType=0", request.full_url)
        self.assertEqual(headers["accept"], "*/*")
        self.assertEqual(headers["accept-encoding"], "gzip, deflate, br")
        self.assertEqual(headers["connection"], "keep-alive")
        self.assertEqual(headers["user-agent"], "PostmanRuntime-ApipostRuntime/1.1.0")
        self.assertEqual(headers["sign-server-auth"], f"{sysid}|{timestamp}|{expected_digest}")
        self.assertEqual(headers["sysid"], sysid)

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_file_info_allows_explicit_header_override(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": [{"fid": "17804597785350144", "fileUrl": "https://files.example/17804597785350144"}],
            }
        )

        result = audit_client.fetch_audit_invoice_file_info(
            "17804597785350144",
            service_url="https://service-uate-gw.ruijie.com.cn/api/audit-service",
            headers={
                "sign-server-auth": "override-signature",
                "sysid": "override-sysid",
            },
        )

        self.assertEqual(result[0]["fid"], "17804597785350144")
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertTrue(request.full_url.endswith("/audit/invoice-file-info/17804597785350144"))
        self.assertEqual(headers["sign-server-auth"], "override-signature")
        self.assertEqual(headers["sysid"], "override-sysid")
        self.assertEqual(headers["user-agent"], "PostmanRuntime-ApipostRuntime/1.1.0")

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_file_info_accepts_real_service_object_payload(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "status": "200",
                "err": "操作成功",
                "data": {
                    "fid": "17804597785350144",
                    "fileUrl": "https://files.example/17804597785350144",
                    "fileBase64": "BASE64-17804597785350144",
                },
            }
        )

        result = audit_client.fetch_audit_invoice_file_info(
            "17804597785350144",
            service_url="https://service-uate-gw.ruijie.com.cn/api/audit-service",
            headers={
                "sign-server-auth": "override-signature",
                "sysid": "override-sysid",
            },
        )

        self.assertEqual(
            result,
            [
                {
                    "fid": "17804597785350144",
                    "fileUrl": "https://files.example/17804597785350144",
                    "fileBase64": "BASE64-17804597785350144",
                }
            ],
        )

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_files_accepts_real_service_status_envelope(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "status": "200",
                "err": "操作成功",
                "data": [{"fid": "FID-REAL-001"}],
            }
        )

        result = audit_client.fetch_audit_invoice_files(
            "rjw260327000006",
            service_url="https://service-uate-gw.ruijie.com.cn/api/audit-service",
            headers={
                "sign-server-auth": "override-signature",
                "sysid": "override-sysid",
            },
        )

        self.assertEqual(result, [{"fid": "FID-REAL-001"}])

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_task_info_list_requests_signed_task_list_endpoint(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "status": "200",
                "err": "操作成功",
                "data": [
                    {
                        "miInstanceCode": "rjw260617000005",
                        "anaStatus": 0,
                        "systemIdentifier": 4,
                    }
                ],
            }
        )

        result = audit_client.fetch_audit_task_info_list(
            "rjw260617000005",
            service_url="https://service.example",
            headers={
                "sign-server-auth": "override-signature",
                "sysid": "override-sysid",
            },
        )

        self.assertEqual(
            result,
            [
                {
                    "miInstanceCode": "rjw260617000005",
                    "anaStatus": 0,
                    "systemIdentifier": 4,
                }
            ],
        )
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertTrue(request.full_url.endswith("/api/audit-service/audit/task-info-list/rjw260617000005"))
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["sign-server-auth"], "override-signature")
        self.assertEqual(headers["sysid"], "override-sysid")

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_update_audit_task_status_posts_signed_status_payload(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": True,
            }
        )

        response = audit_client.update_audit_task_status(
            "rjw260617000005",
            new_status=1,
            system_identifier=4,
            service_url="https://service.example",
            headers={
                "sign-server-auth": "override-signature",
                "sysid": "override-sysid",
            },
        )

        self.assertEqual(response, {"code": 0, "message": "success", "data": True})
        request = mock_urlopen.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.full_url, "https://service.example/api/audit-service/audit/task-status-update")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "instanceCode": "rjw260617000005",
                "newStatus": 1,
                "systemIdentifier": 4,
            },
        )
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["sign-server-auth"], "override-signature")
        self.assertEqual(headers["sysid"], "override-sysid")

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_update_audit_task_status_can_include_fail_reason(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 0,
                "message": "success",
                "data": True,
            }
        )

        response = audit_client.update_audit_task_status(
            "rjw260617000006",
            new_status=2,
            system_identifier=4,
            fail_reason="URLError: timed out",
            service_url="https://service.example",
        )

        self.assertEqual(response, {"code": 0, "message": "success", "data": True})
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "instanceCode": "rjw260617000006",
                "newStatus": 2,
                "systemIdentifier": 4,
                "failReason": "URLError: timed out",
            },
        )

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_task_info_list_raises_on_failure_envelope(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "status": "500",
                "err": "task lookup failed",
                "data": [],
            }
        )

        with self.assertRaises(ValueError) as context:
            audit_client.fetch_audit_task_info_list(
                "rjw260617000005",
                service_url="https://service.example",
            )

        self.assertIn("task lookup failed", str(context.exception))

    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_update_audit_task_status_raises_on_failure_envelope(self, mock_urlopen) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            {
                "code": 1,
                "message": "task status update failed",
                "data": False,
            }
        )

        with self.assertRaises(ValueError) as context:
            audit_client.update_audit_task_status(
                "rjw260617000005",
                new_status=1,
                system_identifier=4,
                service_url="https://service.example",
            )

        self.assertIn("task status update failed", str(context.exception))

    @patch.dict(
        os.environ,
        {
            "AUDIT_SERVICE_TIMEOUT": "12.5",
            "AUDIT_SERVICE_MAX_RETRIES": "1",
            "AUDIT_SERVICE_RETRY_BACKOFF_SECONDS": "0",
        },
        clear=False,
    )
    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_invoice_files_retries_timeout_and_uses_env_timeout(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            URLError(TimeoutError("timed out")),
            FakeHttpResponse(
                {
                    "code": 0,
                    "message": "success",
                    "data": [{"fid": "FID-RETRY-001"}],
                }
            ),
        ]

        result = audit_client.fetch_audit_invoice_files(
            "rjw260327000006",
            service_url="https://service.example",
        )

        self.assertEqual(result, [{"fid": "FID-RETRY-001"}])
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertEqual(mock_urlopen.call_args_list[0].kwargs["timeout"], 12.5)
        self.assertEqual(mock_urlopen.call_args_list[1].kwargs["timeout"], 12.5)

    @patch.dict(
        os.environ,
        {
            "AUDIT_SERVICE_MAX_RETRIES": "1",
            "AUDIT_SERVICE_RETRY_BACKOFF_SECONDS": "0",
        },
        clear=False,
    )
    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_update_audit_task_status_retries_retryable_http_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            HTTPError(
                "https://service.example/api/audit-service/audit/task-status-update",
                503,
                "Service Unavailable",
                None,
                io.BytesIO(b"{}"),
            ),
            FakeHttpResponse(
                {
                    "code": 0,
                    "message": "success",
                    "data": True,
                }
            ),
        ]

        result = audit_client.update_audit_task_status(
            "rjw260617000005",
            new_status=1,
            system_identifier=4,
            service_url="https://service.example",
        )

        self.assertEqual(result, {"code": 0, "message": "success", "data": True})
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch.dict(
        os.environ,
        {
            "AUDIT_SERVICE_MAX_RETRIES": "2",
            "AUDIT_SERVICE_RETRY_BACKOFF_SECONDS": "0",
        },
        clear=False,
    )
    @patch("expense_audit_orchestrator.audit_client.urlopen")
    def test_fetch_audit_task_info_list_does_not_retry_non_retryable_http_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = HTTPError(
            "https://service.example/api/audit-service/audit/task-info-list/rjw260617000005",
            400,
            "Bad Request",
            None,
            io.BytesIO(b"{}"),
        )

        with self.assertRaises(HTTPError):
            audit_client.fetch_audit_task_info_list(
                "rjw260617000005",
                service_url="https://service.example",
            )

        self.assertEqual(mock_urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()