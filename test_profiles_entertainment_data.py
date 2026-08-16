import unittest

from expense_audit_orchestrator.profiles.entertainment.client import EntertainmentApiClient
from expense_audit_orchestrator.profiles.entertainment.data import (
    build_e15_invoice_type_enricher,
    build_entertainment_invoice_serial_enricher,
    build_entertainment_receipt_enricher,
    load_e15_invoice_type_map,
    normalize_invoice_serial_prefix,
)


class EntertainmentApiClientTests(unittest.TestCase):
    def test_fetch_business_fee_details_uses_expected_endpoint(self) -> None:
        calls = []

        def request(path, description, timeout):
            calls.append((path, description, timeout))
            return [{"bfdItemcode": "3", "bfdReceivenumber": "8"}]

        result = EntertainmentApiClient(request_list=request).fetch_business_fee_details("INS/001")

        self.assertEqual(result[0]["bfdReceivenumber"], "8")
        self.assertEqual(calls[0][0], "/api/audit-service/audit/bussness-fee-details/INS%2F001")


class E15InvoiceTypeDataTests(unittest.TestCase):
    def test_loads_six_configured_invoice_types(self) -> None:
        invoice_types = load_e15_invoice_type_map()
        self.assertEqual(
            {item["code"] for item in invoice_types},
            {"10", "9", "16", "29", "28", "26"},
        )

    def test_matches_ocr_code_and_returns_canonical_type(self) -> None:
        enricher = build_e15_invoice_type_enricher()
        result = enricher("R", "invoice.pdf", {"invoiceType": "26"}, {})
        self.assertEqual(result, {
            "isApplicable": True,
            "invoiceTypeCode": "26",
            "invoiceTypeName": "数电普票",
        })

    def test_matches_rj_code_via_expense_invoice_type_mapping(self) -> None:
        enricher = build_e15_invoice_type_enricher()
        result = enricher(
            "R",
            "invoice.pdf",
            {"invoiceType": "RJ-010"},
            {
                "expenseInvoiceTypes": [
                    {
                        "invoiceType": "RJ-010",
                        "manufacturerBillCode": "29",
                        "manufacturerBillName": "数电铁路",
                    }
                ]
            },
        )
        self.assertTrue(result["isApplicable"])
        self.assertEqual(result["invoiceTypeCode"], "29")
        self.assertEqual(result["invoiceTypeName"], "数电铁路")

    def test_non_configured_invoice_type_is_not_applicable(self) -> None:
        enricher = build_e15_invoice_type_enricher()
        result = enricher("R", "invoice.pdf", {"invoiceType": "2"}, {})
        self.assertFalse(result["isApplicable"])


class EntertainmentDataTests(unittest.TestCase):
    def test_enricher_filters_gift_details_and_sums_reception_count(self) -> None:
        class FakeClient:
            def fetch_business_fee_details(self, instance_code):
                self.instance_code = instance_code
                return [
                    {"bfdItemcode": "3", "bfdReceivenumber": "8"},
                    {"bfdItemname": "赠送纪念品", "bfdReceivenumber": "2"},
                    {"bfdItemcode": "1", "bfdReceivenumber": "99"},
                ]

        client = FakeClient()
        result = build_entertainment_receipt_enricher(client=client)(
            "R",
            {"auditInfo": {"instanceCode": "INS-1"}},
        )

        self.assertEqual(client.instance_code, "INS-1")
        self.assertTrue(result["hasGiftItem"])
        self.assertEqual(result["giftReceptionCount"], 10)
        self.assertEqual(len(result["giftBusinessFeeDetails"]), 2)

    def test_enricher_without_instance_code_does_not_call_service(self) -> None:
        class FailingClient:
            def fetch_business_fee_details(self, instance_code):
                raise AssertionError("should not be called")

        self.assertEqual(
            build_entertainment_receipt_enricher(client=FailingClient())("R", {}),
            {},
        )


if __name__ == "__main__":
    unittest.main()

class EntertainmentServiceUrlBindingTests(unittest.TestCase):
    def test_profile_binds_explicit_service_url_to_client(self) -> None:
        from unittest.mock import patch
        from expense_audit_orchestrator.profiles import get_profile

        with patch(
            "expense_audit_orchestrator.profiles.entertainment.data.EntertainmentApiClient"
        ) as client_class:
            get_profile("entertainment", service_url="http://mock-service")

        client_class.assert_called_once_with(service_url="http://mock-service")


class EntertainmentInvoiceSerialDataTests(unittest.TestCase):
    def test_normalizes_invoice_prefix_as_string(self) -> None:
        self.assertEqual(normalize_invoice_serial_prefix("0012345601"), "001234")
        self.assertEqual(normalize_invoice_serial_prefix("12345678901234567890"), "123456")
        self.assertIsNone(normalize_invoice_serial_prefix("1234567"))

    def test_queries_history_with_invoice_priority_and_context_parameters(self) -> None:
        calls = []

        def provider(cheque_no, instance_code, accounting_code):
            calls.append((cheque_no, instance_code, accounting_code))
            return ["12345699"]

        enricher = build_entertainment_invoice_serial_enricher(provider=provider)
        result = enricher(
            "REC-ENT-001",
            "invoice.pdf",
            {
                "chequeNo": "12345601",
                "invoiceNo": "99999999",
                "serialNo": "88888888",
                "accountingCode": "111",
            },
            {"auditInfo": {"instanceCode": "INS-001", "accountingCode": "222"}},
        )

        self.assertEqual(calls, [("12345601", "INS-001", "111")])
        self.assertEqual(result["invoiceNo"], "12345601")
        self.assertEqual(result["currentPrefix"], "123456")
        self.assertEqual(result["historyNumbers"], ["12345699"])
        self.assertTrue(result["historyHit"])
        self.assertTrue(result["isEntertainmentInvoice"])
        self.assertFalse(result["lookupFailed"])

    def test_falls_back_from_cheque_no_to_invoice_no_and_serial_no(self) -> None:
        calls = []

        def provider(cheque_no, instance_code, accounting_code):
            calls.append(cheque_no)
            return []

        enricher = build_entertainment_invoice_serial_enricher(provider=provider)
        invoice_result = enricher(
            "REC",
            "invoice.pdf",
            {"chequeNo": "", "invoiceNo": "12345601", "serialNo": "88888888"},
            {"auditInfo": {"instanceCode": "INS"}},
        )
        serial_result = enricher(
            "REC",
            "invoice.pdf",
            {"serialNo": "12345602"},
            {"auditInfo": {"instanceCode": "INS"}},
        )

        self.assertEqual(calls, ["12345601", "12345602"])
        self.assertEqual(invoice_result["invoiceNo"], "12345601")
        self.assertEqual(serial_result["invoiceNo"], "12345602")

    def test_normalizes_mapping_history_items_and_empty_history(self) -> None:
        enricher = build_entertainment_invoice_serial_enricher(
            provider=lambda *_args: [
                {"chequeNo": "12345699"},
                {"invoiceNo": "12345698"},
                {"serialNo": "12345697"},
                {},
                None,
            ]
        )
        result = enricher(
            "REC",
            "invoice.pdf",
            {"invoiceNo": "12345601"},
            {"auditInfo": {"instanceCode": "INS"}},
        )
        self.assertEqual(result["historyNumbers"], ["12345699", "12345698", "12345697"])
        self.assertTrue(result["historyHit"])

        empty = build_entertainment_invoice_serial_enricher(provider=lambda *_args: [])(
            "REC",
            "invoice.pdf",
            {"invoiceNo": "12345601"},
            {"auditInfo": {"instanceCode": "INS"}},
        )
        self.assertEqual(empty["historyNumbers"], [])
        self.assertFalse(empty["historyHit"])

    def test_lookup_failure_degrades_without_blocking_data_preparation(self) -> None:
        def provider(*_args):
            raise RuntimeError("service unavailable")

        result = build_entertainment_invoice_serial_enricher(provider=provider)(
            "REC",
            "invoice.pdf",
            {"invoiceNo": "12345601"},
            {"auditInfo": {"instanceCode": "INS"}},
        )

        self.assertEqual(result["historyNumbers"], [])
        self.assertFalse(result["historyHit"])
        self.assertTrue(result["lookupFailed"])

    def test_missing_or_short_invoice_number_does_not_call_history_service(self) -> None:
        calls = []

        def provider(*args):
            calls.append(args)
            return ["unexpected"]

        enricher = build_entertainment_invoice_serial_enricher(provider=provider)
        for ocr_data in ({}, {"invoiceNo": "1234567"}):
            result = enricher("REC", "invoice.pdf", ocr_data, {})
            self.assertFalse(result["historyHit"])
            self.assertFalse(result["lookupFailed"])
            self.assertIsNone(result["currentPrefix"])

        self.assertEqual(calls, [])
