from __future__ import annotations

import json
import unittest
from pathlib import Path

import zen


from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
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

    def test_e02_uses_tax_number_expression_not_personal_header_check(self) -> None:
        self.assertEqual(
            self.tax_node["content"]["inputs"][0]["field"],
            "tax_check",
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
