"""Tests for the standalone prepared receipt viewer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tools.prepared_receipt_viewer.app import create_app


class PreparedReceiptViewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.prepared_dir = Path(self.temp_dir.name)
        self.receipt_code = "rjw260705000001"
        payload = {
            "receiptCode": self.receipt_code,
            "serviceData": {"shouldNotBeReturned": True},
            "invoiceCount": 2,
            "invoicePreparations": [
                {"preparedInput": {"invoiceNo": "one", "value": 1}},
                {"preparedInput": {"invoiceNo": "two", "value": 2}},
                {"other": "missing prepared input"},
            ],
        }
        self.receipt_path = self.prepared_dir / f"{self.receipt_code}.prepared-receipt.json"
        self.receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        self.client = TestClient(create_app(self.prepared_dir))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_returns_only_prepared_inputs_as_an_array(self) -> None:
        response = self.client.get(f"/api/receipts/{self.receipt_code}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {"invoiceNo": "one", "value": 1},
                {"invoiceNo": "two", "value": 2},
            ],
        )
        self.assertNotIn("receiptCode", response.text)
        self.assertNotIn("serviceData", response.text)

    def test_returns_not_found_for_unknown_receipt(self) -> None:
        response = self.client.get("/api/receipts/unknown")

        self.assertEqual(response.status_code, 404)

    def test_rejects_invalid_receipt_code(self) -> None:
        response = self.client.get("/api/receipts/../secret")

        self.assertIn(response.status_code, (400, 404))

    def test_returns_validation_error_for_invalid_json(self) -> None:
        self.receipt_path.write_text("{not-json", encoding="utf-8")

        response = self.client.get(f"/api/receipts/{self.receipt_code}")

        self.assertEqual(response.status_code, 422)

    def test_returns_validation_error_for_missing_preparations(self) -> None:
        self.receipt_path.write_text(json.dumps({"receiptCode": self.receipt_code}), encoding="utf-8")

        response = self.client.get(f"/api/receipts/{self.receipt_code}")

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
