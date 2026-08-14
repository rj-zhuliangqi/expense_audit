from __future__ import annotations

import unittest
from pathlib import Path

from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision

ROOT = Path(__file__).resolve().parent
GRAPH_PATH = ROOT / "graph-latest-personal-transport-0722.json"


class W19TaxiInvoiceGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.decision = load_decision(GRAPH_PATH)

    @staticmethod
    def _prepared(*, is_taxi: bool, history_hit: bool = False, batch_hit: bool = False) -> dict:
        invoice_no = "12345601"
        return {
            "receipt": {"code": "REC-W19-001"},
            "context": {},
            "invoiceNo": invoice_no,
            "invoiceType": "8" if is_taxi else "72",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "passengerName": "张三",
            "goodsName": "出租车服务" if is_taxi else "交通运输服务",
            "items": [],
            "verifyResult": [],
            "serviceData": {
                "expenseInvoiceTypes": [{"manufacturerBillCode": "电子普票"}],
                "companyList": [],
                "auditInfo": {
                    "instanceComCode": "",
                    "applyAmount": 100,
                    "submitTime": "2026-01-01",
                    "verifiUserName": "张三",
                },
                "companyBlacklist": [],
                "invoiceUsageHistory": [],
                "taxiInvoiceSerial": {
                    "invoiceNo": invoice_no,
                    "currentPrefix": "123456",
                    "historyNumbers": [],
                    "historyHit": history_hit,
                    "batchHit": batch_hit,
                    "isTaxiInvoice": is_taxi,
                    "lookupFailed": False,
                },
            },
        }

    @staticmethod
    def _w19(result: dict) -> dict:
        for value in result["decisionOutput"].values():
            if isinstance(value, dict) and value.get("reason_code") == "W19":
                return value
        raise AssertionError("W19 result not found")

    def test_history_or_batch_hit_rejects_taxi_invoice(self) -> None:
        for history_hit, batch_hit in ((True, False), (False, True)):
            with self.subTest(history_hit=history_hit, batch_hit=batch_hit):
                result = evaluate_prepared_input(
                    self.decision,
                    self._prepared(
                        is_taxi=True,
                        history_hit=history_hit,
                        batch_hit=batch_hit,
                    ),
                    trace=False,
                )
                rule = self._w19(result)
                self.assertEqual(rule["distinguish_result"], "REJECT")
                self.assertNotIn("{发票号}", rule["message"])
                self.assertIn("发票号 12345601", rule["message"])

    def test_taxi_without_hit_passes(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(is_taxi=True),
            trace=False,
        )
        self.assertEqual(self._w19(result)["distinguish_result"], "PASS")

    def test_non_taxi_passes_even_if_flags_are_true(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(is_taxi=False, history_hit=True, batch_hit=True),
            trace=False,
        )
        self.assertEqual(self._w19(result)["distinguish_result"], "PASS")


if __name__ == "__main__":
    unittest.main()
