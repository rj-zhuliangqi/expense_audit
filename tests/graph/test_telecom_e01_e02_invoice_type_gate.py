from __future__ import annotations

import json
import unittest
from typing import Any

from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision


GRAPH_PATH = OFFICIAL_GRAPH_PATHS["telecom"]
E01_CODES = {1, 2, 3, 4, 5, 7, 11, 12, 13, 15, 19, 21, 23, 25, 26, 27, 28, 29, 72}
E02_CODES = {1, 2, 3, 4, 5, 7, 11, 12, 13, 15, 19, 23, 25, 26, 27, 28, 29, 72}


class TelecomE01E02InvoiceTypeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.decision = load_decision(GRAPH_PATH)

    @staticmethod
    def _rule(result: dict[str, Any], code: str) -> dict[str, Any]:
        for value in result["decisionOutput"].values():
            if isinstance(value, dict) and value.get("reason_code") == code:
                return value
        raise AssertionError(f"rule {code!r} not found")

    @staticmethod
    def _prepared(invoice_type: Any) -> dict[str, Any]:
        return {
            "receipt": {"code": "REC-TELECOM-E01-E02-GATE-001"},
            "context": {},
            "invoiceNo": "TELECOM-E01-E02-GATE-001",
            "invoiceType": invoice_type,
            "buyerName": "错误公司",
            "buyerTaxNo": "BAD-TAX",
            "salerName": "中国移动通信集团福建有限公司福州分公司",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "items": [],
            "verifyResult": [],
            "serviceData": {
                # This field is deliberately independent from the gate: the
                # gate must use the raw invoiceType, not the allowed-ticket list.
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

    def _evaluate(self, invoice_type: Any) -> dict[str, Any]:
        return evaluate_prepared_input(
            self.decision,
            self._prepared(invoice_type),
            trace=False,
        )

    def test_all_e01_codes_execute_header_check(self) -> None:
        for invoice_type in sorted(E01_CODES):
            with self.subTest(invoice_type=invoice_type):
                result = self._evaluate(invoice_type)
                self.assertEqual(self._rule(result, "E01")["distinguish_result"], "REJECT")

    def test_all_e02_codes_execute_tax_number_check(self) -> None:
        for invoice_type in sorted(E02_CODES):
            with self.subTest(invoice_type=invoice_type):
                result = self._evaluate(invoice_type)
                self.assertEqual(self._rule(result, "E02")["distinguish_result"], "REJECT")

    def test_invoice_type_21_only_runs_e01(self) -> None:
        result = self._evaluate(21)
        self.assertEqual(self._rule(result, "E01")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(result, "E02")["distinguish_result"], "PASS")

    def test_non_applicable_invoice_types_pass_without_comparing_header_or_tax(self) -> None:
        for invoice_type in (8, 9, 16, 17, None, "999"):
            with self.subTest(invoice_type=invoice_type):
                result = self._evaluate(invoice_type)
                self.assertEqual(self._rule(result, "E01")["distinguish_result"], "PASS")
                self.assertEqual(self._rule(result, "E02")["distinguish_result"], "PASS")

    def test_invoice_type_normalization_accepts_number_and_trimmed_numeric_string(self) -> None:
        for invoice_type in (1, "1", " 1 ", 72, "72", " 72 "):
            with self.subTest(invoice_type=invoice_type):
                result = self._evaluate(invoice_type)
                self.assertEqual(self._rule(result, "E01")["distinguish_result"], "REJECT")
                self.assertEqual(self._rule(result, "E02")["distinguish_result"], "REJECT")

    def test_gate_uses_raw_invoice_type_not_expense_invoice_types(self) -> None:
        result = self._evaluate(9)
        self.assertEqual(self._rule(result, "E01")["distinguish_result"], "PASS")
        self.assertEqual(self._rule(result, "E02")["distinguish_result"], "PASS")

    def test_formal_graph_contains_both_gate_expression_families(self) -> None:
        data_preprocess = next(
            node
            for node in self.graph["nodes"]
            if node.get("id") == "c67dcb33-2750-4a43-8af7-8346612c04a9"
        )
        header_preprocess = next(
            node
            for node in self.graph["nodes"]
            if node.get("id") == "df8506e4-11fd-4e0d-8198-17a25fcb4f50"
        )
        data_keys = {expression["key"] for expression in data_preprocess["content"]["expressions"]}
        header_keys = {expression["key"] for expression in header_preprocess["content"]["expressions"]}
        self.assertTrue({"e02InvoiceTypeCode", "e02Applicable", "tax_check"}.issubset(data_keys))
        self.assertTrue({"e01InvoiceTypeCode", "e01Applicable", "header_check"}.issubset(header_keys))


if __name__ == "__main__":
    unittest.main()
