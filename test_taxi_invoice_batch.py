from __future__ import annotations

import unittest
from typing import Any

from expense_audit_orchestrator.application import _inject_taxi_invoice_batch_context


class TaxiInvoiceBatchContextTests(unittest.TestCase):
    @staticmethod
    def _prepared(
        invoice_no: str,
        *,
        is_taxi: bool = True,
        current_prefix: str | None = None,
    ) -> dict[str, Any]:
        serial_data = {
            "invoiceNo": invoice_no,
            "currentPrefix": current_prefix,
            "historyNumbers": [],
            "historyHit": False,
            "batchHit": False,
            "isTaxiInvoice": is_taxi,
            "lookupFailed": False,
        }
        return {
            "preparedInput": {
                "serviceData": {"taxiInvoiceSerial": serial_data},
                "context": {"serviceData": {"taxiInvoiceSerial": dict(serial_data)}},
            }
        }

    @staticmethod
    def _inject(*items: dict[str, Any]) -> list[dict[str, Any]]:
        prepared_receipt = {"invoicePreparations": list(items)}
        _inject_taxi_invoice_batch_context(prepared_receipt)
        return [item["preparedInput"]["serviceData"]["taxiInvoiceSerial"] for item in items]

    def test_same_prefix_marks_every_taxi_invoice_and_syncs_context(self) -> None:
        first = self._prepared("12345601", current_prefix="123456")
        second = self._prepared("12345602", current_prefix="123456")

        serials = self._inject(first, second)

        self.assertTrue(all(item["batchHit"] for item in serials))
        self.assertEqual(serials[0]["batchPeerInvoiceNumbers"], ["12345602"])
        self.assertEqual(serials[1]["batchPeerInvoiceNumbers"], ["12345601"])
        self.assertEqual(
            serials[0]["relationDescription"],
            "本次核销单其他出租车发票号 12345602",
        )
        self.assertEqual(
            serials[0]["relatedInvoiceNumbers"],
            ["12345602"],
        )
        for item in (first, second):
            self.assertIs(
                item["preparedInput"]["context"]["serviceData"]["taxiInvoiceSerial"],
                item["preparedInput"]["serviceData"]["taxiInvoiceSerial"],
            )
            self.assertTrue(item["preparedInput"]["context"]["serviceData"]["taxiInvoiceSerial"]["batchHit"])

    def test_three_same_prefix_marks_all_invoices(self) -> None:
        items = [
            self._prepared("12345601", current_prefix="123456"),
            self._prepared("12345602", current_prefix="123456"),
            self._prepared("12345603", current_prefix="123456"),
        ]

        serials = self._inject(*items)

        self.assertEqual([item["batchHit"] for item in serials], [True, True, True])
        self.assertEqual(
            [item["batchPeerInvoiceNumbers"] for item in serials],
            [
                ["12345602", "12345603"],
                ["12345601", "12345603"],
                ["12345601", "12345602"],
            ],
        )


    def test_history_numbers_are_included_in_relation_description(self) -> None:
        item = self._prepared("12345601", current_prefix="123456")
        serial_data = item["preparedInput"]["serviceData"]["taxiInvoiceSerial"]
        serial_data["historyNumbers"] = ["12345699"]
        serial_data["historyHit"] = True

        serials = self._inject(item)

        self.assertEqual(serials[0]["historyPeerInvoiceNumbers"], ["12345699"])
        self.assertEqual(serials[0]["relatedInvoiceNumbers"], ["12345699"])
        self.assertEqual(serials[0]["relationDescription"], "历史发票号 12345699")

    def test_history_and_batch_numbers_are_both_included(self) -> None:
        first = self._prepared("12345601", current_prefix="123456")
        second = self._prepared("12345602", current_prefix="123456")
        first["preparedInput"]["serviceData"]["taxiInvoiceSerial"]["historyNumbers"] = [
            "12345699"
        ]
        first["preparedInput"]["serviceData"]["taxiInvoiceSerial"]["historyHit"] = True

        serials = self._inject(first, second)

        self.assertEqual(
            serials[0]["relationDescription"],
            "本次核销单其他出租车发票号 12345602 及历史发票号 12345699",
        )
        self.assertEqual(serials[0]["relatedInvoiceNumbers"], ["12345602", "12345699"])

    def test_different_prefixes_do_not_hit(self) -> None:
        serials = self._inject(
            self._prepared("12345601", current_prefix="123456"),
            self._prepared("65432101", current_prefix="654321"),
        )

        self.assertEqual([item["batchHit"] for item in serials], [False, False])
        self.assertEqual([item["batchPeerInvoiceNumbers"] for item in serials], [[], []])

    def test_non_taxi_invoice_does_not_join_taxi_group(self) -> None:
        serials = self._inject(
            self._prepared("12345601", current_prefix="123456", is_taxi=True),
            self._prepared("12345602", current_prefix="123456", is_taxi=False),
        )

        self.assertEqual([item["batchHit"] for item in serials], [False, False])
        self.assertEqual([item["batchPeerInvoiceNumbers"] for item in serials], [[], []])

    def test_duplicate_invoice_numbers_are_batch_hits(self) -> None:
        serials = self._inject(
            self._prepared("12345601", current_prefix="123456"),
            self._prepared("12345601", current_prefix="123456"),
        )

        self.assertEqual([item["batchHit"] for item in serials], [True, True])

    def test_missing_or_short_invoice_number_does_not_hit(self) -> None:
        serials = self._inject(
            self._prepared("", current_prefix=None),
            self._prepared("1234567", current_prefix=None),
        )

        self.assertEqual([item["batchHit"] for item in serials], [False, False])
        self.assertEqual([item["batchPeerInvoiceNumbers"] for item in serials], [[], []])


if __name__ == "__main__":
    unittest.main()
