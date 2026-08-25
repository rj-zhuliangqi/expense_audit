from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision_from_content


GRAPH_PATH = OFFICIAL_GRAPH_PATHS["telecom"]
REQUEST_NODE_ID = "9948bfb0-d9fb-416d-b9a2-b22a875094f0"
RESPONSE_NODE_ID = "e109e75a-d107-4fd0-a8b3-e3dae7fad15b"
HEADER_PREPROCESS_NODE_ID = "df8506e4-11fd-4e0d-8198-17a25fcb4f50"
E01_NODE_ID = "d3046965-dbaf-41cd-ba93-0d957fe67ec8"
W28_NODE_ID = "e2b630c8-096d-4fea-ad4c-986473d8a880"

INPUT_FIELD_ID = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"
REASON_CODE_ID = "48a29115-f542-44d3-8c02-3ff71e19ee38"
RESULT_ID = "f35ede49-0eae-4dda-b39e-11a11383697a"


class TelecomE01W28HeaderGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.graph = graph
        cls.preprocess_node = next(
            node for node in graph["nodes"] if node["id"] == HEADER_PREPROCESS_NODE_ID
        )
        cls.e01_node = next(node for node in graph["nodes"] if node["id"] == E01_NODE_ID)
        cls.w28_node = next(node for node in graph["nodes"] if node["id"] == W28_NODE_ID)

        # Execute only the header branch. The official graph also contains LLM
        # function nodes, which are unrelated to this deterministic regression.
        node_ids = {
            REQUEST_NODE_ID,
            RESPONSE_NODE_ID,
            HEADER_PREPROCESS_NODE_ID,
            E01_NODE_ID,
            W28_NODE_ID,
        }
        header_graph = {
            "contentType": graph["contentType"],
            "nodes": [node for node in graph["nodes"] if node["id"] in node_ids],
            "edges": [
                edge
                for edge in graph["edges"]
                if edge["sourceId"] in node_ids and edge["targetId"] in node_ids
            ],
        }
        cls.decision = load_decision_from_content(header_graph)

    def test_e01_and_w28_have_disjoint_responsibilities(self) -> None:
        expressions = {
            expression["key"]: expression["value"]
            for expression in self.preprocess_node["content"]["expressions"]
        }
        self.assertEqual(expressions["header_check"], "$.is_company ? $.is_in_company_list:$.is_same_peple")
        self.assertEqual(
            expressions["header_personal_check"],
            "$.is_company or ($.is_same_peple == false) or $.is_in_telecom_list",
        )

        e01_rules = self._rules_for(self.e01_node, "E01")
        w28_rules = self._rules_for(self.w28_node, "W28")
        self.assertEqual(e01_rules, {"true": '"PASS"', "false": '"REJECT"'})
        self.assertEqual(w28_rules, {"true": '"PASS"', "false": '"WARNING"'})

        w28_pass_rule = next(
            rule
            for rule in self.w28_node["content"]["rules"]
            if rule[REASON_CODE_ID] == '"W28"' and rule[RESULT_ID] == '"PASS"'
        )
        self.assertEqual(w28_pass_rule[INPUT_FIELD_ID], "true")

    def test_personal_employee_in_allowed_operator_city_passes_both(self) -> None:
        results = self._header_results(
            buyer_name="蔡盈琳（个人）",
            saler_name="中国移动通信集团广东有限公司广州分公司",
            telecom_list=[["移动", "广东"]],
        )

        self.assertEqual(results["E01"], "PASS")
        self.assertEqual(results["W28"], "PASS")
        self._assert_at_most_one_non_pass(results)

    def test_personal_employee_outside_operator_city_is_only_w28_warning(self) -> None:
        results = self._header_results(
            buyer_name="蔡盈琳（个人）",
            saler_name="中国移动通信集团福建有限公司福州分公司",
            telecom_list=[["移动", "广东"]],
        )

        self.assertEqual(results["E01"], "PASS")
        self.assertEqual(results["W28"], "WARNING")
        self._assert_at_most_one_non_pass(results)

    def test_personal_non_employee_is_only_e01_reject(self) -> None:
        results = self._header_results(
            buyer_name="王荣波（个人）",
            saler_name="中国移动通信集团福建有限公司福州分公司",
            telecom_list=[["移动", "广东"]],
        )

        self.assertEqual(results["E01"], "REJECT")
        self.assertEqual(results["W28"], "PASS")
        self._assert_at_most_one_non_pass(results)

    def test_matching_company_header_passes_e01_and_skips_w28_check(self) -> None:
        results = self._header_results(
            buyer_name="锐捷网络股份有限公司",
            saler_name="中国移动通信集团福建有限公司福州分公司",
            telecom_list=[["移动", "广东"]],
        )

        self.assertEqual(results["E01"], "PASS")
        self.assertEqual(results["W28"], "PASS")
        self._assert_at_most_one_non_pass(results)

    def test_mismatching_company_header_is_only_e01_reject(self) -> None:
        results = self._header_results(
            buyer_name="北京星网锐捷网络技术有限公司",
            saler_name="中国移动通信集团福建有限公司福州分公司",
            telecom_list=[["移动", "广东"]],
        )

        self.assertEqual(results["E01"], "REJECT")
        self.assertEqual(results["W28"], "PASS")
        self._assert_at_most_one_non_pass(results)

    def _header_results(
        self,
        *,
        buyer_name: str,
        saler_name: str,
        telecom_list: list[list[str]],
    ) -> dict[str, str]:
        prepared_input = {
            "receipt": {"code": "TELECOM-E01-W28-TEST"},
            "buyerName": buyer_name,
            "salerName": saler_name,
            "instanceComCode": "111",
            "invoice_file_id": "invoice-file-test",
            "invoice_info_id": "invoice-info-test",
            "context": {},
            "serviceData": {
                "auditInfo": {
                    "instanceComCode": "111",
                    "verifiUserName": "蔡盈琳",
                },
                "companyList": [
                    {
                        "ccode": "111",
                        "companyName": "锐捷网络股份有限公司",
                    },
                ],
                "telecom_list": telecom_list,
            },
        }
        result = evaluate_prepared_input(self.decision, prepared_input, trace=False)
        return {
            value["reason_code"]: value["distinguish_result"]
            for value in result["decisionOutput"].values()
            if isinstance(value, dict) and value.get("reason_code") in {"E01", "W28"}
        }

    @staticmethod
    def _rules_for(node: dict[str, Any], reason_code: str) -> dict[str, str]:
        return {
            rule[INPUT_FIELD_ID]: rule[RESULT_ID]
            for rule in node["content"]["rules"]
            if rule[REASON_CODE_ID] == f'"{reason_code}"'
        }

    def _assert_at_most_one_non_pass(self, results: dict[str, str]) -> None:
        non_pass = [status for status in results.values() if status != "PASS"]
        self.assertLessEqual(len(non_pass), 1, results)


if __name__ == "__main__":
    unittest.main()
