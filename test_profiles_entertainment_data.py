import unittest

from expense_audit_orchestrator.profiles.entertainment.client import EntertainmentApiClient
from expense_audit_orchestrator.profiles.entertainment.data import (
    build_e15_invoice_type_enricher,
    build_entertainment_receipt_enricher,
    load_e15_invoice_type_map,
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
