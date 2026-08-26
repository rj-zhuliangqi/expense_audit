import unittest
from unittest.mock import patch

from expense_audit_orchestrator import audit_client
from expense_audit_orchestrator.profiles.entertainment.client import EntertainmentApiClient
from expense_audit_orchestrator.profiles.entertainment.data import (
    build_e15_invoice_type_enricher,
    build_entertainment_receipt_enricher,
    build_entertainment_taxi_invoice_serial_enricher,
    build_w34_invoice_serial_enricher,
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

    def test_enricher_without_instance_code_marks_w33_lookup_as_error(self) -> None:
        class FailingClient:
            def fetch_business_fee_details(self, instance_code):
                raise AssertionError("should not be called")

        result = build_entertainment_receipt_enricher(client=FailingClient())("R", {})

        self.assertEqual(result["giftDetailLookupStatus"], "error")
        self.assertIn("缺少核销单号", result["giftDetailLookupError"])
        self.assertFalse(result["hasGiftItem"])

    def test_enricher_marks_business_fee_detail_service_error(self) -> None:
        class FailingClient:
            def fetch_business_fee_details(self, instance_code):
                raise RuntimeError("business fee detail service unavailable")

        result = build_entertainment_receipt_enricher(client=FailingClient())(
            "R",
            {"auditInfo": {"instanceCode": "INS-ERROR"}},
        )

        self.assertEqual(result["giftDetailLookupStatus"], "error")
        self.assertEqual(
            result["giftDetailLookupError"],
            "business fee detail service unavailable",
        )
        self.assertFalse(result["hasGiftItem"])
        self.assertEqual(result["giftReceptionCount"], 0)

    def test_w34_enricher_queries_history_with_expected_arguments_and_filters_self(self) -> None:
        calls = []

        def provider(cheque_no, instance_code, accounting_code):
            calls.append((cheque_no, instance_code, accounting_code))
            return [
                "12345601",
                "12345611",
                {"invoiceNo": "12345612"},
                {"unknown": "ignored"},
            ]

        enricher = build_w34_invoice_serial_enricher(
            service_url="https://service.example",
            provider=provider,
        )
        result = enricher(
            "RECEIPT-001",
            "invoice.pdf",
            {
                "chequeNo": "12345601",
                "invoiceType": "RJ-001",
                "accountingCode": "AC-001",
            },
            {"auditInfo": {"instanceCode": "INSTANCE-001"}},
        )

        self.assertEqual(calls, [("12345601", "INSTANCE-001", "AC-001")])
        self.assertEqual(result["invoiceNo"], "12345601")
        self.assertTrue(result["isApplicable"])
        self.assertEqual(result["historyNumbers"], ["12345611", "12345612"])
        self.assertTrue(result["historyHit"])
        self.assertFalse(result["lookupFailed"])

    def test_w34_applies_to_the_three_configured_invoice_type_codes(self) -> None:
        calls = []

        def provider(cheque_no, instance_code, accounting_code):
            calls.append(cheque_no)
            return []

        enricher = build_w34_invoice_serial_enricher(provider=provider)
        for invoice_type in ("RJ-001", "1-003", "1-002"):
            with self.subTest(invoice_type=invoice_type):
                result = enricher(
                    "RECEIPT-002",
                    "invoice.pdf",
                    {"chequeNo": "12345601", "invoiceType": invoice_type},
                    {"auditInfo": {"instanceCode": "INSTANCE-002"}},
                )
                self.assertTrue(result["isApplicable"])
        self.assertEqual(calls, ["12345601", "12345601", "12345601"])

    def test_w34_non_applicable_invoice_does_not_query_history(self) -> None:
        def provider(*args):
            raise AssertionError(f"W34 history lookup should not run: {args}")

        result = build_w34_invoice_serial_enricher(provider=provider)(
            "RECEIPT-003",
            "invoice.pdf",
            {"chequeNo": "12345601", "invoiceType": "8"},
            {"auditInfo": {"instanceCode": "INSTANCE-003"}},
        )

        self.assertFalse(result["isApplicable"])
        self.assertEqual(result["historyNumbers"], [])
        self.assertFalse(result["historyHit"])

    def test_e42_taxi_enricher_uses_taxi_history_interface_by_default(self) -> None:
        with patch.object(
            audit_client,
            "fetch_taxi_invoice_serial_numbers",
            return_value=["12345699"],
        ) as taxi_provider, patch.object(
            audit_client,
            "fetch_invoice_serial_numbers",
            side_effect=AssertionError("E42 must not query generic W34 history interface"),
        ):
            enricher = build_entertainment_taxi_invoice_serial_enricher(
                service_url="https://service.example"
            )
            result = enricher(
                "RECEIPT-004",
                "invoice.pdf",
                {
                    "chequeNo": "12345601",
                    "invoiceType": "8",
                    "accountingCode": "ACCT-004",
                },
                {"auditInfo": {"instanceCode": "INSTANCE-004"}},
            )

        taxi_provider.assert_called_once_with(
            "12345601",
            "INSTANCE-004",
            "ACCT-004",
            service_url="https://service.example",
        )
        self.assertTrue(result["isTaxiInvoice"])
        self.assertEqual(result["currentPrefix"], "123456")
        self.assertEqual(result["historyNumbers"], ["12345699"])
        self.assertTrue(result["historyHit"])
        self.assertFalse(result["lookupFailed"])

    def test_e42_taxi_enricher_marks_history_lookup_failure(self) -> None:
        def provider(*args):
            raise RuntimeError("service unavailable")

        result = build_entertainment_taxi_invoice_serial_enricher(provider=provider)(
            "RECEIPT-005",
            "invoice.pdf",
            {"chequeNo": "12345601", "invoiceType": "8"},
            {"auditInfo": {"instanceCode": "INSTANCE-005"}},
        )

        self.assertEqual(result["historyNumbers"], [])
        self.assertFalse(result["historyHit"])
        self.assertTrue(result["lookupFailed"])

    def test_e42_short_taxi_invoice_does_not_query_history(self) -> None:
        def provider(*args):
            raise AssertionError(f"short invoice must not query history: {args}")

        result = build_entertainment_taxi_invoice_serial_enricher(provider=provider)(
            "RECEIPT-006",
            "invoice.pdf",
            {"chequeNo": "1234567", "invoiceType": "8"},
            {"auditInfo": {"instanceCode": "INSTANCE-006"}},
        )

        self.assertTrue(result["isTaxiInvoice"])
        self.assertIsNone(result["currentPrefix"])
        self.assertFalse(result["lookupFailed"])

    def test_w34_history_lookup_failure_degrades_without_blocking(self) -> None:
        def provider(*args):
            raise RuntimeError("service unavailable")

        result = build_w34_invoice_serial_enricher(provider=provider)(
            "RECEIPT-005",
            "invoice.pdf",
            {"chequeNo": "12345601", "invoiceType": "1-003"},
            {"auditInfo": {"instanceCode": "INSTANCE-005"}},
        )

        self.assertEqual(result["historyNumbers"], [])
        self.assertFalse(result["historyHit"])
        self.assertTrue(result["lookupFailed"])

    def test_invoice_serial_prefix_preserves_leading_zero_and_long_values(self) -> None:
        self.assertEqual(normalize_invoice_serial_prefix("0012345601"), "001234")
        self.assertEqual(normalize_invoice_serial_prefix("123456789012345601"), "123456")
        self.assertIsNone(normalize_invoice_serial_prefix("1234567"))


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

    def test_profile_contains_entertainment_invoice_serial_enricher(self) -> None:
        from expense_audit_orchestrator.profiles import get_profile

        profile = get_profile("entertainment", service_url="https://service.example")

        self.assertIn("w34InvoiceSerial", profile.invoice_enrichers)
        self.assertIn("entertainmentInvoiceSerial", profile.invoice_enrichers)
