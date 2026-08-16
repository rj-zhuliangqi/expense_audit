from __future__ import annotations

import unittest
from typing import Any

from expense_audit_orchestrator.application import _inject_entertainment_invoice_batch_context


class EntertainmentInvoiceBatchContextTests(unittest.TestCase):
    @staticmethod
    def _prepared(
        invoice_no: str,
        *,
        is_entertainment: bool = True,
        current_prefix: str | None = None,
        history_numbers: list[str] | None = None,
    ) -> dict[str, Any]:
        serial_data = {
            "invoiceNo": invoice_no,
            "currentPrefix": current_prefix,
            "historyNumbers": history_numbers or [],
            "historyHit": bool(history_numbers),
            "batchHit": False,
            "isEntertainmentInvoice": is_entertainment,
            "lookupFailed": False,
            "relationSubject": "发票",
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

    def test_same_prefix_marks_all_entertainment_invoices_and_syncs_context(self) -> None:
        first = self._prepared("12345601", current_prefix="123456")
        second = self._prepared("12345602", current_prefix="123456")

        serials = self._inject(first, second)

        self.assertEqual([item["batchHit"] for item in serials], [True, True])
        self.assertEqual(serials[0]["batchPeerInvoiceNumbers"], ["12345602"])
        self.assertEqual(serials[1]["batchPeerInvoiceNumbers"], ["12345601"])
        self.assertEqual(
            serials[0]["relationDescription"],
            "本次核销单其他发票号 12345602",
        )
        self.assertIs(
            first["preparedInput"]["context"]["serviceData"]["entertainmentInvoiceSerial"],
            first["preparedInput"]["serviceData"]["entertainmentInvoiceSerial"],
        )

    def test_different_prefix_or_non_entertainment_invoice_does_not_hit(self) -> None:
        serials = self._inject(
            self._prepared("12345601", current_prefix="123456"),
            self._prepared("12345602", current_prefix="123456", is_entertainment=False),
            self._prepared("65432101", current_prefix="654321"),
        )

        self.assertEqual([item["batchHit"] for item in serials], [False, False, False])
        self.assertEqual(
            [item["batchPeerInvoiceNumbers"] for item in serials],
            [[], [], []],
        )

    def test_duplicate_invoice_numbers_are_batch_hits(self) -> None:
        serials = self._inject(
            self._prepared("12345601", current_prefix="123456"),
            self._prepared("12345601", current_prefix="123456"),
        )
        self.assertEqual([item["batchHit"] for item in serials], [True, True])

    def test_history_and_batch_numbers_are_shown(self) -> None:
        first = self._prepared(
            "12345601",
            current_prefix="123456",
            history_numbers=["12345699"],
        )
        second = self._prepared("12345602", current_prefix="123456")

        serial = self._inject(first, second)[0]

        self.assertTrue(serial["historyHit"])
        self.assertEqual(serial["relatedInvoiceNumbers"], ["12345602", "12345699"])
        self.assertEqual(
            serial["relationDescription"],
            "本次核销单其他发票号 12345602 及历史发票号 12345699",
        )

    def test_recomputes_prefix_for_legacy_prepared_receipt(self) -> None:
        first = self._prepared("12345601")
        second = self._prepared("12345602")
        first["preparedInput"]["serviceData"]["entertainmentInvoiceSerial"].pop("currentPrefix")
        second["preparedInput"]["serviceData"]["entertainmentInvoiceSerial"].pop("currentPrefix")

        serials = self._inject(first, second)

        self.assertEqual([item["currentPrefix"] for item in serials], ["123456", "123456"])
        self.assertEqual([item["batchHit"] for item in serials], [True, True])

    def test_short_invoice_number_does_not_hit(self) -> None:
        serials = self._inject(
            self._prepared("1234567"),
            self._prepared("1234567"),
        )
        self.assertEqual([item["batchHit"] for item in serials], [False, False])


if __name__ == "__main__":
    unittest.main()
