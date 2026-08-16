from __future__ import annotations

import unittest
from pathlib import Path

from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision

ROOT = Path(__file__).resolve().parent
GRAPH_PATH = ROOT / "graph-latest-entertainment-0722.json"


class E34EntertainmentInvoiceGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.decision = load_decision(GRAPH_PATH)

    @staticmethod
    def _prepared(
        *,
        is_entertainment: bool = True,
        history_hit: bool = False,
        batch_hit: bool = False,
        relation_description: str = "",
    ) -> dict:
        invoice_no = "12345601"
        return {
            "receipt": {"code": "REC-ENT-E34-001"},
            "context": {},
            "invoiceNo": invoice_no,
            "invoiceType": "26",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "passengerName": "张三",
            "goodsName": "餐饮服务",
            "items": [],
            "verifyResult": [],
            "serviceData": {
                "expenseInvoiceTypes": [],
                "companyList": [],
                "auditInfo": {
                    "instanceComCode": "",
                    "applyAmount": 100,
                    "submitTime": "2026-01-01",
                    "verifiUserName": "张三",
                },
                "companyBlacklist": [],
                "invoiceUsageHistory": [],
                "entertainmentInvoiceSerial": {
                    "invoiceNo": invoice_no,
                    "currentPrefix": "123456",
                    "historyNumbers": ["12345699"] if history_hit else [],
                    "historyHit": history_hit,
                    "batchHit": batch_hit,
                    "isEntertainmentInvoice": is_entertainment,
                    "lookupFailed": False,
                    "relationDescription": relation_description,
                },
            },
        }

    @staticmethod
    def _e34(result: dict) -> dict:
        for value in result["decisionOutput"].values():
            if isinstance(value, dict) and value.get("reason_code") == "E34":
                return value
        raise AssertionError("E34 result not found")

    def test_history_or_batch_hit_rejects_and_lists_related_invoice(self) -> None:
        cases = [
            (True, False, "历史发票号 12345699"),
            (False, True, "本次核销单其他发票号 12345602"),
        ]
        for history_hit, batch_hit, relation_description in cases:
            with self.subTest(history_hit=history_hit, batch_hit=batch_hit):
                result = evaluate_prepared_input(
                    self.decision,
                    self._prepared(
                        history_hit=history_hit,
                        batch_hit=batch_hit,
                        relation_description=relation_description,
                    ),
                    trace=False,
                )
                rule = self._e34(result)
                self.assertEqual(rule["distinguish_result"], "REJECT")
                self.assertIn("发票号 12345601", rule["message"])
                self.assertIn(relation_description, rule["message"])
                self.assertNotIn("{发票号}", rule["message"])

    def test_message_lists_history_and_batch_relationship(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(
                history_hit=True,
                batch_hit=True,
                relation_description="本次核销单其他发票号 12345602 及历史发票号 12345699",
            ),
            trace=False,
        )
        rule = self._e34(result)
        self.assertEqual(rule["distinguish_result"], "REJECT")
        self.assertIn("12345601", rule["message"])
        self.assertIn("12345602", rule["message"])
        self.assertIn("12345699", rule["message"])

    def test_without_hit_passes(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(),
            trace=False,
        )
        self.assertEqual(self._e34(result)["distinguish_result"], "PASS")

    def test_non_entertainment_invoice_passes_even_if_flags_are_true(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(
                is_entertainment=False,
                history_hit=True,
                batch_hit=True,
                relation_description="历史发票号 12345699",
            ),
            trace=False,
        )
        self.assertEqual(self._e34(result)["distinguish_result"], "PASS")


if __name__ == "__main__":
    unittest.main()
