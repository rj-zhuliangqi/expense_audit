from __future__ import annotations

import json
import unittest
from typing import Any

from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision


PROFILES = ("entertainment", "personal_transport")
RED_INVOICE_INPUT_ID = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"
REASON_CODE_OUTPUT_ID = "48a29115-f542-44d3-8c02-3ff71e19ee38"


class RedInvoiceGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graphs = {
            profile: json.loads(
                OFFICIAL_GRAPH_PATHS[profile].read_text(encoding="utf-8")
            )
            for profile in PROFILES
        }
        cls.decisions = {
            profile: load_decision(OFFICIAL_GRAPH_PATHS[profile])
            for profile in PROFILES
        }

    @staticmethod
    def _prepared(verify_key: str) -> dict[str, Any]:
        return {
            "receipt": {"code": "REC-RED-INVOICE-001"},
            "context": {},
            "invoiceNo": "INV-RED-INVOICE-001",
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

    def _red_invoice_rule(self, profile: str) -> dict[str, Any]:
        for node in self.graphs[profile]["nodes"]:
            if node["type"] != "decisionTableNode":
                continue
            for rule in node["content"]["rules"]:
                if rule.get(REASON_CODE_OUTPUT_ID) == '"sys-004"':
                    return rule
        raise AssertionError(f"{profile}: red-invoice rule not found")

    def test_red_invoice_rules_match_red_invoice_key_and_output_sys004(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile):
                rule = self._red_invoice_rule(profile)
                self.assertEqual(rule[RED_INVOICE_INPUT_ID], '"redInvoice"')
                self.assertEqual(rule[REASON_CODE_OUTPUT_ID], '"sys-004"')

    def test_red_invoice_key_is_audited_as_rejected(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile):
                result = evaluate_prepared_input(
                    self.decisions[profile],
                    self._prepared("redInvoice"),
                    trace=False,
                )

                invoice_check = result["decisionOutput"]["invoice_check_result"]
                self.assertEqual(invoice_check["reason_code"], "sys-004")
                self.assertEqual(invoice_check["distinguish_result"], "REJECT")
                self.assertIn("冲红", invoice_check["audit_content"])
                self.assertIn("红冲", invoice_check["message"])

    def test_sys004_is_not_used_as_the_red_invoice_input_key(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile):
                result = evaluate_prepared_input(
                    self.decisions[profile],
                    self._prepared("sys-004"),
                    trace=False,
                )

                self.assertIsNone(result["decisionOutput"].get("invoice_check_result"))


if __name__ == "__main__":
    unittest.main()
