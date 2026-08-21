from __future__ import annotations

import unittest
from typing import Any

from expense_audit_orchestrator.application import (
    _inject_entertainment_invoice_batch_context,
    _inject_w34_invoice_batch_context,
)


class EntertainmentInvoiceBatchContextTests(unittest.TestCase):
    @staticmethod
    def _prepared(
        invoice_no: str,
        *,
        is_taxi: bool = True,
        current_prefix: str | None = None,
        history_numbers: list[str] | None = None,
    ) -> dict[str, Any]:
        serial_data = {
            "invoiceNo": invoice_no,
            "currentPrefix": current_prefix,
            "historyNumbers": history_numbers or [],
            "historyHit": bool(history_numbers),
            "batchHit": False,
            "isTaxiInvoice": is_taxi,
            "relationSubject": "出租车发票",
            "lookupFailed": False,
        }
        return {
            "preparedInput": {
                "serviceData": {"entertainmentInvoiceSerial": serial_data},
                "context": {"serviceData": {"entertainmentInvoiceSerial": dict(serial_data)}},
            }
        }

    @staticmethod
    def _inject(*items: dict[str, Any]) -> list[dict[str, Any]]:
        prepared_receipt = {"invoicePreparations": list(items)}
        _inject_entertainment_invoice_batch_context(prepared_receipt)
        return [
            item["preparedInput"]["serviceData"]["entertainmentInvoiceSerial"]
            for item in items
        ]

    def test_same_prefix_marks_all_taxi_invoices_and_syncs_context(self) -> None:
        first = self._prepared("12345601", current_prefix="123456")
        second = self._prepared("12345602", current_prefix="123456")

        serials = self._inject(first, second)

        self.assertEqual([item["batchHit"] for item in serials], [True, True])
        self.assertEqual(serials[0]["batchPeerInvoiceNumbers"], ["12345602"])
        self.assertEqual(serials[1]["batchPeerInvoiceNumbers"], ["12345601"])
        self.assertEqual(
            serials[0]["relationDescription"],
            "本次核销单其他出租车发票号 12345602",
        )
        for item in (first, second):
            self.assertIs(
                item["preparedInput"]["context"]["serviceData"]["entertainmentInvoiceSerial"],
                item["preparedInput"]["serviceData"]["entertainmentInvoiceSerial"],
            )

    def test_three_same_prefix_and_duplicate_numbers_all_hit(self) -> None:
        serials = self._inject(
            self._prepared("12345601", current_prefix="123456"),
            self._prepared("12345602", current_prefix="123456"),
            self._prepared("12345601", current_prefix="123456"),
        )

        self.assertEqual([item["batchHit"] for item in serials], [True, True, True])
        self.assertEqual(
            [item["batchPeerInvoiceNumbers"] for item in serials],
            [
                ["12345602", "12345601"],
                ["12345601", "12345601"],
                ["12345601", "12345602"],
            ],
        )

    def test_different_prefixes_do_not_hit(self) -> None:
        serials = self._inject(
            self._prepared("12345601", current_prefix="123456"),
            self._prepared("65432101", current_prefix="654321"),
        )

        self.assertEqual([item["batchHit"] for item in serials], [False, False])
        self.assertEqual([item["batchPeerInvoiceNumbers"] for item in serials], [[], []])

    def test_non_taxi_invoice_is_not_a_peer(self) -> None:
        serials = self._inject(
            self._prepared("12345601", current_prefix="123456", is_taxi=True),
            self._prepared("12345602", current_prefix="123456", is_taxi=False),
        )

        self.assertEqual([item["batchHit"] for item in serials], [False, False])
        self.assertEqual([item["batchPeerInvoiceNumbers"] for item in serials], [[], []])

    def test_history_and_batch_numbers_are_shown_in_relation_description(self) -> None:
        serials = self._inject(
            self._prepared(
                "12345601",
                current_prefix="123456",
                history_numbers=["12345699"],
            ),
            self._prepared("12345602", current_prefix="123456"),
        )

        self.assertEqual(
            serials[0]["relationDescription"],
            "本次核销单其他出租车发票号 12345602 及历史发票号 12345699",
        )
        self.assertEqual(
            serials[0]["relatedInvoiceNumbers"],
            ["12345602", "12345699"],
        )

    def test_short_or_missing_invoice_numbers_do_not_hit(self) -> None:
        serials = self._inject(
            self._prepared("", current_prefix=None),
            self._prepared("1234567", current_prefix=None),
        )

        self.assertEqual([item["batchHit"] for item in serials], [False, False])
        self.assertEqual([item["batchPeerInvoiceNumbers"] for item in serials], [[], []])

    def test_context_only_prepared_input_is_restored_and_recomputed(self) -> None:
        first = self._prepared("12345601", current_prefix="stale")
        second = self._prepared("12345602", current_prefix="also-stale")
        first["preparedInput"].pop("serviceData")
        second["preparedInput"]["context"]["serviceData"]["entertainmentInvoiceSerial"][
            "currentPrefix"
        ] = "also-stale"

        serials = self._inject(first, second)

        self.assertEqual([item["currentPrefix"] for item in serials], ["123456", "123456"])
        self.assertEqual([item["batchHit"] for item in serials], [True, True])
        self.assertIn("serviceData", first["preparedInput"])


class W34InvoiceBatchContextTests(unittest.TestCase):
    @staticmethod
    def _prepared(
        invoice_no: str,
        *,
        applicable: bool = True,
        history_numbers: list[str] | None = None,
    ) -> dict[str, Any]:
        serial_data = {
            "invoiceNo": invoice_no,
            "isApplicable": applicable,
            "historyNumbers": history_numbers or [],
            "historyHit": bool(history_numbers),
            "batchHit": False,
            "relationSubject": "发票",
            "lookupFailed": False,
        }
        return {
            "preparedInput": {
                "serviceData": {"w34InvoiceSerial": serial_data},
                "context": {"serviceData": {"w34InvoiceSerial": dict(serial_data)}},
            }
        }

    @staticmethod
    def _inject(*items: dict[str, Any]) -> list[dict[str, Any]]:
        prepared_receipt = {"invoicePreparations": list(items)}
        _inject_w34_invoice_batch_context(prepared_receipt)
        return [item["preparedInput"]["serviceData"]["w34InvoiceSerial"] for item in items]

    def test_difference_at_most_ten_marks_both_invoices(self) -> None:
        serials = self._inject(
            self._prepared("90000000000000000010"),
            self._prepared("90000000000000000020"),
        )

        self.assertEqual([item["batchHit"] for item in serials], [True, True])
        self.assertEqual(
            [item["batchPeerInvoiceNumbers"] for item in serials],
            [["90000000000000000020"], ["90000000000000000010"]],
        )

    def test_difference_greater_than_ten_does_not_hit(self) -> None:
        serials = self._inject(
            self._prepared("10000000000000000000"),
            self._prepared("10000000000000000011"),
        )

        self.assertEqual([item["batchHit"] for item in serials], [False, False])
        self.assertEqual([item["batchPeerInvoiceNumbers"] for item in serials], [[], []])

    def test_non_w34_invoice_type_is_not_a_peer(self) -> None:
        serials = self._inject(
            self._prepared("1234567890", applicable=True),
            self._prepared("1234567891", applicable=False),
        )

        self.assertEqual([item["batchHit"] for item in serials], [False, False])
        self.assertEqual([item["batchPeerInvoiceNumbers"] for item in serials], [[], []])

    def test_history_numbers_are_preserved_for_cross_receipt_graph_check(self) -> None:
        serials = self._inject(
            self._prepared("1234567890", history_numbers=["1234567899"]),
        )

        self.assertEqual(serials[0]["historyNumbers"], ["1234567899"])
        self.assertTrue(serials[0]["historyHit"])
        self.assertEqual(serials[0]["batchPeerInvoiceNumbers"], [])


if __name__ == "__main__":
    unittest.main()
