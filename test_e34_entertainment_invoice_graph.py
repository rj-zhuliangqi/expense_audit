from __future__ import annotations

import json
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
        is_taxi: bool,
        history_hit: bool = False,
        batch_hit: bool = False,
    ) -> dict:
        invoice_no = "12345601"
        relation_description = ""
        if history_hit and batch_hit:
            relation_description = "本次核销单其他出租车发票号 12345602 及历史发票号 12345699"
        elif history_hit:
            relation_description = "历史发票号 12345699"
        elif batch_hit:
            relation_description = "本次核销单其他出租车发票号 12345602"

        return {
            "receipt": {"code": "REC-ENT-E34-001"},
            "context": {},
            "invoiceNo": invoice_no,
            "invoiceType": "8" if is_taxi else "餐饮服务",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "passengerName": "张三",
            "goodsName": "出租车服务" if is_taxi else "餐饮服务",
            "items": [],
            "verifyResult": [],
            "serviceData": {
                "expenseInvoiceTypes": [],
                "companyList": [],
                "companyBlacklist": [],
                "invoiceUsageHistory": [],
                "auditInfo": {
                    "instanceCode": "INSTANCE-E34-001",
                    "instanceComCode": "",
                    "applyAmount": 100,
                    "submitTime": "2026-01-01",
                    "verifiUserName": "张三",
                },
                "entertainment_data": {
                    "hasGiftItem": False,
                    "giftReceptionCount": 0,
                },
                "e15InvoiceType": {
                    "isApplicable": False,
                },
                "entertainmentInvoiceSerial": {
                    "invoiceNo": invoice_no,
                    "currentPrefix": "123456",
                    "historyNumbers": ["12345699"] if history_hit else [],
                    "historyHit": history_hit,
                    "batchHit": batch_hit,
                    "isTaxiInvoice": is_taxi,
                    "lookupFailed": False,
                    "relationDescription": relation_description,
                },
            },
        }

    @staticmethod
    def _rule(result: dict, reason_code: str) -> dict:
        for value in result["decisionOutput"].values():
            if isinstance(value, dict) and value.get("reason_code") == reason_code:
                return value
        raise AssertionError(f"{reason_code} result not found")

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
                rule = self._rule(result, "E34")
                self.assertEqual(rule["distinguish_result"], "REJECT")
                self.assertIn("发票号 12345601", rule["message"])
                self.assertNotIn("{发票号}", rule["message"])

    def test_message_shows_batch_and_history_peers(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(is_taxi=True, history_hit=True, batch_hit=True),
            trace=False,
        )
        rule = self._rule(result, "E34")

        self.assertEqual(rule["distinguish_result"], "REJECT")
        self.assertIn("本次核销单其他出租车发票号 12345602", rule["message"])
        self.assertIn("历史发票号 12345699", rule["message"])

    def test_taxi_without_hit_passes(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(is_taxi=True),
            trace=False,
        )
        self.assertEqual(self._rule(result, "E34")["distinguish_result"], "PASS")

    def test_non_taxi_passes_even_if_flags_are_true(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(is_taxi=False, history_hit=True, batch_hit=True),
            trace=False,
        )
        self.assertEqual(self._rule(result, "E34")["distinguish_result"], "PASS")

    def test_existing_w34_rule_is_preserved(self) -> None:
        graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        w34_nodes = [
            node
            for node in graph["nodes"]
            if node.get("id") == "ent-invoice-number-check"
        ]
        self.assertEqual(len(w34_nodes), 1)
        w34_rules = w34_nodes[0]["content"]["rules"]
        self.assertEqual(
            {rule["48a29115-f542-44d3-8c02-3ff71e19ee38"] for rule in w34_rules},
            {'"W34"'},
        )
        self.assertEqual(
            {rule["f35ede49-0eae-4dda-b39e-11a11383697a"] for rule in w34_rules},
            {'"PASS"', '"WARNING"'},
        )


if __name__ == "__main__":
    unittest.main()
