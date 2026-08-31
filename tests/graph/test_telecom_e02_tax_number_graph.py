from __future__ import annotations

import json
import unittest
from pathlib import Path

import zen

from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision
GRAPH_PATH = OFFICIAL_GRAPH_PATHS["telecom"]
INPUT_FIELD_ID = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"
OUTPUT_RESULT_ID = "f35ede49-0eae-4dda-b39e-11a11383697a"
REASON_CODE_ID = "48a29115-f542-44d3-8c02-3ff71e19ee38"


class TelecomE02TaxNumberGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.preprocess_node = next(
            node
            for node in cls.graph["nodes"]
            if node.get("id") == "c67dcb33-2750-4a43-8af7-8346612c04a9"
        )
        cls.tax_node = next(
            node
            for node in cls.graph["nodes"]
            if node.get("id") == "d13f7062-96e4-4d74-a552-dfcc60d98ff4"
        )
        cls.is_tax_exists_expression = next(
            expression["value"]
            for expression in cls.preprocess_node["content"]["expressions"]
            if expression.get("key") == "isTaxExists"
        )
        cls.is_personal_buyer_expression = next(
            expression["value"]
            for expression in cls.preprocess_node["content"]["expressions"]
            if expression.get("key") == "isPersonalBuyer"
        )
        cls.decision = load_decision(GRAPH_PATH)

    def test_e02_uses_tax_number_check_with_personal_title_bypass(self) -> None:
        self.assertEqual(
            self.tax_node["content"]["inputs"][0]["field"],
            "tax_check",
        )
        self.assertEqual(
            next(
                expression["value"]
                for expression in self.preprocess_node["content"]["expressions"]
                if expression.get("key") == "tax_check"
            ),
            "not($.e02Applicable ?? false) or $.isPersonalBuyer or $.isTaxExists",
        )
        self.assertEqual(
            self.is_personal_buyer_expression,
            '(trim((buyerName ?? orgName ?? "")) != "" and (('
            'endsWith(trim((buyerName ?? orgName ?? "")), "公司") or '
            'endsWith(trim((buyerName ?? orgName ?? "")), "企业") or '
            'endsWith(trim((buyerName ?? orgName ?? "")), "集团") or '
            'endsWith(trim((buyerName ?? orgName ?? "")), "事务所") or '
            'endsWith(trim((buyerName ?? orgName ?? "")), "商行") or '
            'endsWith(trim((buyerName ?? orgName ?? "")), "合作社")) == false))',
        )

    def test_matching_company_tax_number_passes_e02(self) -> None:
        prepared = {
            "instanceComCode": "111",
            "buyerTaxNo": "913500007549617646",
            "header_personal_check": True,
            "serviceData": {
                "companyList": [
                    {
                        "ccode": "111",
                        "companyTax": "913500007549617646",
                    }
                ]
            },
        }
        self.assertTrue(zen.evaluate_expression(self.is_tax_exists_expression, prepared))
        self.assertEqual(self._e02_results()["true"], '"PASS"')

    def test_mismatching_company_tax_number_rejects_e02(self) -> None:
        prepared = {
            "instanceComCode": "111",
            "buyerTaxNo": "9135000075496176",
            "header_personal_check": True,
            "serviceData": {
                "companyList": [
                    {
                        "ccode": "111",
                        "companyTax": "913500007549617646",
                    }
                ]
            },
        }
        self.assertFalse(zen.evaluate_expression(self.is_tax_exists_expression, prepared))
        self.assertEqual(self._e02_results()["false"], '"REJECT"')

    def test_personal_invoice_title_skips_e02_tax_number_check(self) -> None:
        prepared = {
            "receipt": {"code": "REC-TELECOM-E02-PERSONAL-001"},
            "context": {},
            "invoiceType": 26,
            "buyerName": "柳歆炜（个人）",
            "buyerTaxNo": "",
            "salerName": "中国移动通信集团福建有限公司福州分公司",
            "invoiceNo": "INV-TELECOM-E02-PERSONAL-001",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "items": [],
            "verifyResult": [],
            "instanceComCode": "111",
            "serviceData": {
                "expenseInvoiceTypes": [{"manufacturerBillCode": "26"}],
                "companyBlacklist": [],
                "invoiceUsageHistory": [],
                "telecom_list": [],
                "companyList": [
                    {
                        "ccode": "111",
                        "companyTax": "913500007549617646",
                    }
                ],
                "auditInfo": {
                    "instanceComCode": "111",
                    "applyAmount": 100,
                    "submitTime": "2026-01-01",
                },
            },
        }

        self.assertTrue(zen.evaluate_expression(self.is_personal_buyer_expression, prepared))
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        e02 = next(
            value
            for value in result["decisionOutput"].values()
            if isinstance(value, dict) and value.get("reason_code") == "E02"
        )
        self.assertEqual(e02["distinguish_result"], "PASS")

    def test_bare_personal_name_also_skips_e02_tax_number_check(self) -> None:
        prepared = {
            "receipt": {"code": "REC-TELECOM-E02-PERSONAL-002"},
            "context": {},
            "invoiceType": 26,
            "buyerName": "刘雪涛",
            "buyerTaxNo": "",
            "salerName": "中国移动通信集团福建有限公司福州分公司",
            "invoiceNo": "INV-TELECOM-E02-PERSONAL-002",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "items": [],
            "verifyResult": [],
            "instanceComCode": "111",
            "serviceData": {
                "expenseInvoiceTypes": [{"manufacturerBillCode": "26"}],
                "companyBlacklist": [],
                "invoiceUsageHistory": [],
                "telecom_list": [],
                "companyList": [
                    {
                        "ccode": "111",
                        "companyTax": "913500007549617646",
                    }
                ],
                "auditInfo": {
                    "instanceComCode": "111",
                    "applyAmount": 100,
                    "submitTime": "2026-01-01",
                },
            },
        }

        self.assertTrue(zen.evaluate_expression(self.is_personal_buyer_expression, prepared))
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        e02 = next(
            value
            for value in result["decisionOutput"].values()
            if isinstance(value, dict) and value.get("reason_code") == "E02"
        )
        self.assertEqual(e02["distinguish_result"], "PASS")

    def test_missing_buyer_title_does_not_skip_e02_tax_number_check(self) -> None:
        self.assertFalse(
            zen.evaluate_expression(
                self.is_personal_buyer_expression,
                {"buyerName": "   "},
            )
        )

    def test_enterprise_suffix_is_not_treated_as_personal_title(self) -> None:
        self.assertFalse(
            zen.evaluate_expression(
                self.is_personal_buyer_expression,
                {"buyerName": "某某个人独资企业"},
            )
        )

    def test_company_invoice_title_still_requires_matching_tax_number(self) -> None:
        prepared = {
            "receipt": {"code": "REC-TELECOM-E02-COMPANY-001"},
            "context": {},
            "invoiceType": 26,
            "buyerName": "错误公司",
            "buyerTaxNo": "BAD-TAX",
            "salerName": "中国移动通信集团福建有限公司福州分公司",
            "invoiceNo": "INV-TELECOM-E02-COMPANY-001",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "items": [],
            "verifyResult": [],
            "instanceComCode": "111",
            "serviceData": {
                "expenseInvoiceTypes": [{"manufacturerBillCode": "26"}],
                "companyBlacklist": [],
                "invoiceUsageHistory": [],
                "telecom_list": [],
                "companyList": [
                    {
                        "ccode": "111",
                        "companyTax": "913500007549617646",
                    }
                ],
                "auditInfo": {
                    "instanceComCode": "111",
                    "applyAmount": 100,
                    "submitTime": "2026-01-01",
                },
            },
        }

        self.assertFalse(zen.evaluate_expression(self.is_personal_buyer_expression, prepared))
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        e02 = next(
            value
            for value in result["decisionOutput"].values()
            if isinstance(value, dict) and value.get("reason_code") == "E02"
        )
        self.assertEqual(e02["distinguish_result"], "REJECT")

    def test_company_name_match_cannot_mask_tax_number_mismatch(self) -> None:
        prepared = {
            "instanceComCode": "111",
            "orgName": "福建锐捷网络有限公司",
            "buyerName": "福建锐捷网络有限公司",
            "buyerTaxNo": "9135000075496176",
            "header_personal_check": True,
            "serviceData": {
                "companyList": [
                    {
                        "ccode": "111",
                        "companyName": "福建锐捷网络有限公司",
                        "companyTax": "913500007549617646",
                    }
                ]
            },
        }
        self.assertFalse(zen.evaluate_expression(self.is_tax_exists_expression, prepared))

    def test_target_invoice_tax_number_is_rejected_by_e02_input(self) -> None:
        prepared = {
            "invoiceNo": "26517000000590997109",
            "instanceComCode": "111",
            "buyerTaxNo": "9135000075496176",
            "serviceData": {
                "companyList": [
                    {
                        "ccode": "111",
                        "companyTax": "913500007549617646",
                    }
                ]
            },
        }
        e02_input = zen.evaluate_expression(self.is_tax_exists_expression, prepared)
        self.assertFalse(e02_input)
        self.assertEqual(self._e02_results()[str(e02_input).lower()], '"REJECT"')

    def test_graph_e02_contains_only_pass_and_reject_rules(self) -> None:
        self.assertEqual(self.tax_node["content"]["outputPath"], "tax_result")
        rules = {
            rule[INPUT_FIELD_ID]: rule[OUTPUT_RESULT_ID]
            for rule in self.tax_node["content"]["rules"]
            if rule[REASON_CODE_ID] == '"E02"'
        }
        self.assertEqual(rules, {"true": '"PASS"', "false": '"REJECT"'})

    def _e02_results(self) -> dict[str, str]:
        return {
            rule[INPUT_FIELD_ID]: rule[OUTPUT_RESULT_ID]
            for rule in self.tax_node["content"]["rules"]
            if rule[REASON_CODE_ID] == '"E02"'
        }


if __name__ == "__main__":
    unittest.main()
