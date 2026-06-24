import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


class CallAuditInvoiceFilesCliTests(unittest.TestCase):
    def test_main_cli_prints_invoice_files_json(self) -> None:
        import call_audit_invoice_files

        invoice_files = [
            {
                "fid": "FID-001",
                "fileName": "invoice-1.pdf",
                "type": 0,
            }
        ]

        with patch.object(
            call_audit_invoice_files.audit_client,
            "fetch_audit_invoice_files",
            return_value=invoice_files,
        ) as mock_fetch:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = call_audit_invoice_files.main_cli(
                    [
                        "REC-001",
                        "--service-url",
                        "https://service.example/api/audit-service",
                    ]
                )

        self.assertEqual(exit_code, 0)
        mock_fetch.assert_called_once_with(
            "REC-001",
            a_type=0,
            service_url="https://service.example/api/audit-service",
            timeout=20.0,
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["instanceCode"], "REC-001")
        self.assertEqual(payload["invoiceFiles"], invoice_files)
        self.assertNotIn("invoiceFileInfo", payload)

    def test_main_cli_fetches_unique_file_info_when_requested(self) -> None:
        import call_audit_invoice_files

        invoice_files = [
            {"fid": "FID-001", "fileName": "invoice-1.pdf", "type": 0},
            {"fid": "FID-001", "fileName": "invoice-1-dup.pdf", "type": 0},
            {"fid": "FID-002", "fileName": "invoice-2.pdf", "type": 0},
        ]
        file_info_by_fid = {
            "FID-001": [{"fid": "FID-001", "fileUrl": "https://files.example/FID-001.pdf"}],
            "FID-002": [{"fid": "FID-002", "fileUrl": "https://files.example/FID-002.pdf"}],
        }

        with patch.object(
            call_audit_invoice_files.audit_client,
            "fetch_audit_invoice_files",
            return_value=invoice_files,
        ) as mock_fetch_files:
            with patch.object(
                call_audit_invoice_files.audit_client,
                "fetch_audit_invoice_file_info",
                side_effect=lambda fid, **_kwargs: file_info_by_fid[fid],
            ) as mock_fetch_file_info:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = call_audit_invoice_files.main_cli(
                        [
                            "REC-001",
                            "--service-url",
                            "https://service.example/api/audit-service",
                            "--with-file-info",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        mock_fetch_files.assert_called_once_with(
            "REC-001",
            a_type=0,
            service_url="https://service.example/api/audit-service",
            timeout=20.0,
        )
        self.assertEqual(mock_fetch_file_info.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in mock_fetch_file_info.call_args_list],
            ["FID-001", "FID-002"],
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["invoiceFiles"], invoice_files)
        self.assertEqual(payload["invoiceFileInfo"], file_info_by_fid)


if __name__ == "__main__":
    unittest.main()