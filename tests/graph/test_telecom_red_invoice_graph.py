from __future__ import annotations

import json
import unittest
from typing import Any

from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision


GRAPH_PATH = OFFICIAL_GRAPH_PATHS["telecom"]
RED_INVOICE_INPUT_ID = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"
REASON_CODE_OUTPUT_ID = "48a29115-f542-44d3-8c02-3ff71e19ee38"


class TelecomRedInvoiceGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.decision = load_decision(GRAPH_PATH)

    @staticmethod
    def _prepared(verify_key: str) -> dict[str, Any]:
        return {
            "receipt": {"code": "REC-TELECOM-RED-INVOICE-001"},
            "context": {},
            "invoiceNo": "INV-TELECOM-RED-INVOICE-001",
            "invoiceType": 1,
            "buyerName": "正确公司",
            "buyerTaxNo": "GOOD-TAX",
            "salerName": "中国移动通信集团福建有限公司福州分公司",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "items": [],
            "verifyResult": [{"key": verify_key}],
            "serviceData": {
                "expenseInvoiceTypes": [{"manufacturerBillCode": "1"}],
                "companyList": [
                    {
                        "ccode": "C1",
                        "companyName": "正确公司",
                        "companyTax": "GOOD-TAX",
                    }
                ],
                "auditInfo": {
                    "instanceComCode": "C1",
                    "applyAmount": 100,
                    "submitTime": "2026-01-01",
                    "verifiUserName": "张三",
                },
                "companyBlacklist": [],
                "invoiceUsageHistory": [],
                "telecom_list": [],
            },
        }

    def _red_invoice_rule(self) -> dict[str, Any]:
        for node in self.graph["nodes"]:
            if node["type"] != "decisionTableNode":
                continue
            for rule in node["content"]["rules"]:
                if rule.get(REASON_CODE_OUTPUT_ID) == '"sys-004"':
                    return rule
        raise AssertionError("red-invoice rule not found")

    def test_red_invoice_rule_matches_red_invoice_key_and_outputs_sys004(self) -> None:
        rule = self._red_invoice_rule()
        self.assertEqual(rule[RED_INVOICE_INPUT_ID], '"redInvoice"')
        self.assertEqual(rule[REASON_CODE_OUTPUT_ID], '"sys-004"')

    def test_red_invoice_key_is_audited_as_rejected(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared("redInvoice"),
            trace=False,
        )

        invoice_check = result["decisionOutput"]["invoice_check_result"]
        self.assertEqual(invoice_check["reason_code"], "sys-004")
        self.assertEqual(invoice_check["distinguish_result"], "REJECT")
        self.assertIn("冲红", invoice_check["audit_content"])
        self.assertIn("已红冲", invoice_check["message"])

    def test_sys004_is_not_used_as_the_red_invoice_input_key(self) -> None:
        result = evaluate_prepared_input(
            self.decision,
            self._prepared("sys-004"),
            trace=False,
        )

        self.assertIsNone(result["decisionOutput"].get("invoice_check_result"))


if __name__ == "__main__":
    unittest.main()
