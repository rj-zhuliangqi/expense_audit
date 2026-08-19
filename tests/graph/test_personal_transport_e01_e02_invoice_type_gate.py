from __future__ import annotations

import json
import unittest
from typing import Any

from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision
from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS


GRAPH_PATH = OFFICIAL_GRAPH_PATHS["personal_transport"]
E01_CODES = {1, 2, 3, 4, 5, 7, 11, 12, 13, 15, 19, 21, 23, 25, 26, 27, 28, 29, 72}
E02_CODES = {1, 2, 3, 4, 5, 7, 11, 12, 13, 15, 19, 23, 25, 26, 27, 28, 29, 72}


class PersonalTransportE01E02InvoiceTypeGateTests(unittest.TestCase):
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
            "receipt": {"code": "REC-E01-E02-GATE-001"},
            "context": {},
            "invoiceNo": "E01-E02-GATE-001",
            "invoiceType": invoice_type,
            "buyerName": "错误公司",
            "buyerTaxNo": "BAD-TAX",
            "salerName": "正常销方",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "passengerName": "张三",
            "goodsName": "交通运输服务",
            "items": [],
            "verifyResult": [],
            "previousInvoiceNumbers": [],
            "serviceData": {
                "expenseInvoiceTypes": [{"manufacturerBillCode": "电子普票"}],
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
            },
        }

    def _evaluate(self, invoice_type: Any) -> dict[str, Any]:
        return evaluate_prepared_input(
            self.decision,
            self._prepared(invoice_type),
            trace=False,
        )

    def test_all_feishu_e01_codes_execute_header_check(self) -> None:
        for invoice_type in sorted(E01_CODES):
            with self.subTest(invoice_type=invoice_type):
                result = self._evaluate(invoice_type)
                self.assertEqual(self._rule(result, "E01")["distinguish_result"], "REJECT")

    def test_all_feishu_e02_codes_execute_tax_number_check(self) -> None:
        for invoice_type in sorted(E02_CODES):
            with self.subTest(invoice_type=invoice_type):
                result = self._evaluate(invoice_type)
                self.assertEqual(self._rule(result, "E02")["distinguish_result"], "REJECT")

    def test_invoice_type_21_only_runs_e01(self) -> None:
        result = self._evaluate(21)
        self.assertEqual(self._rule(result, "E01")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(result, "E02")["distinguish_result"], "PASS")

    def test_non_applicable_invoice_types_pass_without_comparing_name_or_tax_number(self) -> None:
        for invoice_type in (9, 16, None, "999"):
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

    def test_raw_invoice_type_controls_gate_without_personal_transport_fallback(self) -> None:
        prepared = self._prepared(9)
        prepared["serviceData"]["personalTransportInvoiceType"] = {
            "personalTransportInvoiceTypeCode": "1",
            "personalTransportAllowedInvoiceTypeCodes": ["1"],
        }
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        self.assertEqual(self._rule(result, "E01")["distinguish_result"], "PASS")
        self.assertEqual(self._rule(result, "E02")["distinguish_result"], "PASS")

    def test_e01_and_e02_have_only_pass_and_reject_outputs(self) -> None:
        expected = {"E01": {"PASS", "REJECT"}, "E02": {"PASS", "REJECT"}}
        for node in self.graph["nodes"]:
            if node.get("type") != "decisionTableNode":
                continue
            content = node["content"]
            reason_output = next(output for output in content["outputs"] if output["field"] == "reason_code")
            result_output = next(
                output for output in content["outputs"] if output["field"] == "distinguish_result"
            )
            for code, statuses in expected.items():
                values = {
                    rule[result_output["id"]].strip('"')
                    for rule in content["rules"]
                    if rule.get(reason_output["id"]) == f'"{code}"'
                }
                if values:
                    self.assertEqual(values, statuses, node.get("name"))


if __name__ == "__main__":
    unittest.main()
