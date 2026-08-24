from __future__ import annotations

import unittest
from pathlib import Path

from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision

from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
GRAPH_PATH = OFFICIAL_GRAPH_PATHS["personal_transport"]


class E34TaxiInvoiceGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.decision = load_decision(GRAPH_PATH)

    @staticmethod
    def _prepared(
        *,
        is_taxi: bool,
        history_hit: bool = False,
        batch_hit: bool = False,
        lookup_failed: bool = False,
        relation_description: str | None = None,
    ) -> dict:
        invoice_no = "12345601"
        if relation_description is None:
            if history_hit and batch_hit:
                relation_description = "本次核销单其他出租车发票号 12345602 及历史发票号 12345699"
            elif history_hit:
                relation_description = "历史发票号 12345699"
            elif batch_hit:
                relation_description = "本次核销单其他出租车发票号 12345602"
            else:
                relation_description = ""
        return {
            "receipt": {"code": "REC-E34-001"},
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
                    "historyNumbers": ["12345699"] if history_hit else [],
                    "historyHit": history_hit,
                    "relationDescription": relation_description,
                    "batchHit": batch_hit,
                    "isTaxiInvoice": is_taxi,
                    "lookupFailed": lookup_failed,
                },
            },
        }

    @staticmethod
    def _e34(result: dict) -> dict:
        for value in result["decisionOutput"].values():
            if isinstance(value, dict) and value.get("reason_code") == "E34":
                return value
        raise AssertionError("E34 result not found")

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
                rule = self._e34(result)
                self.assertEqual(rule["distinguish_result"], "REJECT")
                self.assertNotIn("{发票号}", rule["message"])
                self.assertIn("发票号 12345601", rule["message"])
                if history_hit:
                    self.assertIn("历史发票号 12345699", rule["message"])
                if batch_hit:
                    self.assertIn("本次核销单其他出租车发票号 12345602", rule["message"])

    def test_reject_message_lists_both_history_and_batch_numbers(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(is_taxi=True, history_hit=True, batch_hit=True),
            trace=False,
        )
        rule = self._e34(result)
        self.assertEqual(rule["distinguish_result"], "REJECT")
        self.assertIn("本次核销单其他出租车发票号 12345602", rule["message"])
        self.assertIn("历史发票号 12345699", rule["message"])

    def test_taxi_without_hit_passes(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(is_taxi=True),
            trace=False,
        )
        self.assertEqual(self._e34(result)["distinguish_result"], "PASS")

    def test_history_lookup_failure_rejects_with_retry_failure_message(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(is_taxi=True, lookup_failed=True),
            trace=False,
        )
        rule = self._e34(result)
        self.assertEqual(rule["distinguish_result"], "REJECT")
        self.assertIn("历史连号接口查询失败", rule["message"])
        self.assertIn("自动重试但仍未成功", rule["message"])

    def test_non_taxi_passes_even_if_flags_are_true(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared(is_taxi=False, history_hit=True, batch_hit=True),
            trace=False,
        )
        self.assertEqual(self._e34(result)["distinguish_result"], "PASS")


if __name__ == "__main__":
    unittest.main()
