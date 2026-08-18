from __future__ import annotations

import json
import unittest
from pathlib import Path

import apps.builders.travel_graph as build_travel_graph
from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision


from expense_audit_orchestrator.paths import PROJECT_ROOT as ROOT
GRAPH_PATH = ROOT / "graph-latest-travel-0807.json"


class TravelGraphBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Keep the checked-in artifact and the generator synchronized for local
        # and CI runs, without changing any data-preparation code.
        build_travel_graph.main()
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))

    def test_graph_shape_and_standard_outputs(self) -> None:
        self.assertEqual(self.graph["contentType"], "application/vnd.gorules.decision")
        self.assertEqual(len(self.graph["nodes"]), 38)
        self.assertEqual(len(self.graph["edges"]), 69)

        nodes = {node["id"]: node for node in self.graph["nodes"]}
        self.assertIn("travel_audit_preprocess", nodes)
        self.assertIn("travel_w39_travel_scene_check", nodes)
        self.assertIn("travel_content_classification_llm", nodes)

        decision_nodes = [node for node in self.graph["nodes"] if node["type"] == "decisionTableNode"]
        self.assertEqual(len(decision_nodes), 32)
        expected_fields = {
            "instance_code",
            "reason_code",
            "distinguish_result",
            "audit_content",
            "audit_type",
            "distinguish_content",
            "invoice_file_id",
            "invoice_info_id",
            "message",
            "policiesIndex",
            "employeeSuggestionTips",
            "create_time",
        }
        for node in decision_nodes:
            self.assertEqual(
                {output["field"] for output in node["content"]["outputs"]},
                expected_fields,
                node["name"],
            )

    def test_all_csv_rows_have_a_rule_node(self) -> None:
        names = {node["name"] for node in self.graph["nodes"] if node["type"] == "decisionTableNode"}
        rows = build_travel_graph._load_csv_rows()
        for definition in build_travel_graph.RULE_DEFINITIONS:
            self.assertIn(definition["name"], names)
        self.assertEqual(len(rows), len(build_travel_graph.RULE_DEFINITIONS))

    def test_missing_travel_data_requires_manual_review(self) -> None:
        decision = load_decision(GRAPH_PATH)
        result = evaluate_prepared_input(
            decision,
            {
                "serviceData": {},
                "context": {},
                "invoiceNo": "INV-MISSING-DATA",
                "invoiceAmount": 100,
            },
            trace=False,
        )
        self.assertEqual(result["checkStatus"], "warning")
        travel_results = [
            value
            for value in result["decisionOutput"].values()
            if isinstance(value, dict) and str(value.get("reason_code", "")).startswith(("E", "W", "sys", "TRAVEL"))
        ]
        self.assertEqual(len(travel_results), 32)
        self.assertTrue(all(value["distinguish_result"] == "WARNING" for value in travel_results))
        self.assertTrue(any(
            "人工复核" in value.get("message", "")
            or "人工复核" in value.get("distinguish_content", "")
            for value in travel_results
        ))

    def _evaluate(self, travel_audit: dict, **overrides: object) -> dict:
        decision = load_decision(GRAPH_PATH)
        prepared_input = {
            "serviceData": {"travelAudit": travel_audit},
            "context": {},
            "invoiceNo": "INV-TRAVEL-001",
            "invoiceAmount": 100,
            "goodsName": "住宿",
            "verifyResult": [],
        }
        prepared_input.update(overrides)
        return evaluate_prepared_input(decision, prepared_input, trace=False)

    @staticmethod
    def _rule(result: dict, code: str, audit_content: str | None = None) -> dict:
        for value in result["decisionOutput"].values():
            if (
                isinstance(value, dict)
                and value.get("reason_code") == code
                and (audit_content is None or value.get("audit_content") == audit_content)
            ):
                return value
        qualifier = f" / {audit_content}" if audit_content else ""
        raise AssertionError(f"rule {code!r}{qualifier} not found")

    def test_city_transport_amount_hits_e38(self) -> None:
        result = self._evaluate(
            {
                "cityTransportApplyAmount": 200,
                "cityTransportStandardAmount": 100,
                "cityTransportInvoiceAmount": 300,
            }
        )
        rule = self._rule(result, "E38")
        self.assertEqual(rule["distinguish_result"], "REJECT")
        self.assertEqual(result["checkStatus"], "reject")

    def test_role_and_warning_rules(self) -> None:
        result = self._evaluate(
            {
                "employeeRole": "销售",
                "cityTransportApplyAmount": 1,
                "ruleStates": {"w37_baggage_airline": "warning"},
            }
        )
        self.assertEqual(self._rule(result, "E23")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(result, "W37")["distinguish_result"], "WARNING")
        self.assertEqual(result["checkStatus"], "reject")

    def test_content_scene_warning_and_tax_warning(self) -> None:
        result = self._evaluate(
            {
                "ruleStates": {"w39_travel_scene": "warning"},
                "taxInfo": {"invoiceDeductibleTax": 10, "formInputTax": 20},
            }
        )
        self.assertEqual(self._rule(result, "W39")["distinguish_result"], "WARNING")
        self.assertEqual(self._rule(result, "TRAVEL-TAX-001")["distinguish_result"], "WARNING")
        self.assertEqual(result["checkStatus"], "warning")

    def test_rule_state_placeholders_cover_complex_travel_checks(self) -> None:
        result = self._evaluate(
            {
                "ruleStates": {
                    "e30_station_vehicle": "reject",
                    "e25_meal_meeting_subsidy": "reject",
                    "e20_train_date": "reject",
                    "e29_train_passenger": "reject",
                    "e32_train_seat": "reject",
                    "w35_baggage_date": "warning",
                    "w38_baggage_weight": "warning",
                }
            }
        )
        self.assertEqual(self._rule(result, "E30")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(result, "E25")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(result, "E20", "检查自购/月结火车票的票据行程与核销单中差旅行程日期是否一致")["distinguish_result"], "REJECT")
        self.assertEqual(
            result["decisionOutput"]["travel_e29_train_passenger_result"]["distinguish_result"],
            "REJECT",
        )
        self.assertEqual(self._rule(result, "E32", "检查是否为二等座及以下")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(result, "W35")["distinguish_result"], "WARNING")
        self.assertEqual(self._rule(result, "W38")["distinguish_result"], "WARNING")

    def test_common_invoice_rules_remain_compatible(self) -> None:
        result = self._evaluate(
            {"ruleStates": {"sys003_void": "reject", "sys004_red_flush": "reject"}},
            goodsName="充值卡",
            verifyResult=[{"key": "invoice_invalid"}],
            salerName="风险销方",
        )
        self.assertEqual(self._rule(result, "E17")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(result, "sys-001")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(result, "sys-003")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(result, "sys-004")["distinguish_result"], "REJECT")

        blacklist_result = self._evaluate(
            {},
            salerName="风险销方",
            serviceData={
                "travelAudit": {},
                "companyBlacklist": [{"value": "风险销方"}],
            },
        )
        self.assertEqual(self._rule(blacklist_result, "E09")["distinguish_result"], "REJECT")

        duplicate_result = self._evaluate(
            {},
            serviceData={
                "travelAudit": {},
                "invoiceUsageHistory": [{"chequeNo": "INV-TRAVEL-001"}],
            },
        )
        self.assertEqual(self._rule(duplicate_result, "E05")["distinguish_result"], "REJECT")

        # Common invoice checks are not blocked by the absence of the travel
        # interface object; only travel-specific rules are fail-open gated.
        common_without_travel = self._evaluate(
            {},
            goodsName="充值卡",
            verifyResult=[{"key": "invoice_invalid"}],
            salerName="风险销方",
            serviceData={
                "companyBlacklist": [{"value": "风险销方"}],
                "invoiceUsageHistory": [{"chequeNo": "INV-TRAVEL-001"}],
            },
        )
        self.assertEqual(self._rule(common_without_travel, "E17")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(common_without_travel, "sys-001")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(common_without_travel, "E09")["distinguish_result"], "REJECT")
        self.assertEqual(self._rule(common_without_travel, "E05")["distinguish_result"], "REJECT")

    def test_every_rule_has_an_input_to_output_path(self) -> None:
        nodes = {node["id"]: node for node in self.graph["nodes"]}
        outgoing: dict[str, list[str]] = {}
        for edge in self.graph["edges"]:
            outgoing.setdefault(edge["sourceId"], []).append(edge["targetId"])
        input_id = next(node["id"] for node in self.graph["nodes"] if node["type"] == "inputNode")
        output_id = next(node["id"] for node in self.graph["nodes"] if node["type"] == "outputNode")

        def reachable(start: str, target: str) -> bool:
            seen = set()
            stack = [start]
            while stack:
                current = stack.pop()
                if current == target:
                    return True
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(outgoing.get(current, []))
            return False

        for node in nodes.values():
            if node["type"] == "decisionTableNode":
                self.assertTrue(reachable(input_id, node["id"]), node["name"])
                self.assertTrue(reachable(node["id"], output_id), node["name"])

    def test_monthly_train_is_checked_for_each_invoice(self) -> None:
        result = self._evaluate({"selfBoughtMonthlyTrain": True})
        self.assertEqual(self._rule(result, "TRAVEL-TRAIN-001")["distinguish_result"], "REJECT")

        # Row 32 is explicitly a per-ticket rule in the CSV.  A non-primary
        # invoice must not be suppressed by document-level dedup metadata.
        still_checked = self._evaluate({
            "selfBoughtMonthlyTrain": True,
            "primaryInvoice": False,
            "raisedRuleCodes": ["TRAVEL-TRAIN-001"],
        })
        self.assertEqual(self._rule(still_checked, "TRAVEL-TRAIN-001")["distinguish_result"], "REJECT")

    def test_multi_invoice_tax_total_is_evaluated_by_the_graph(self) -> None:
        passed = self._evaluate({
            "primaryInvoice": True,
            "taxInfo": {"invoiceDeductibleTaxTotal": 3, "formInputTax": 3},
            "ruleStates": {"travel_tax_amount": "pass"},
        })
        self.assertEqual(self._rule(passed, "TRAVEL-TAX-001")["distinguish_result"], "PASS")

        warning = self._evaluate({
            "primaryInvoice": True,
            "taxInfo": {"invoiceDeductibleTaxTotal": 3, "formInputTax": 4},
            "ruleStates": {"travel_tax_amount": "warning"},
        })
        self.assertEqual(self._rule(warning, "TRAVEL-TAX-001")["distinguish_result"], "WARNING")

        deduped = self._evaluate({
            "primaryInvoice": False,
            "raisedRuleCodes": ["TRAVEL-TAX-001"],
            "raisedRuleKeys": ["travel_tax_amount"],
            "taxInfo": {"invoiceDeductibleTaxTotal": 3, "formInputTax": 4},
            "ruleStates": {"travel_tax_amount": "warning"},
        })
        self.assertEqual(self._rule(deduped, "TRAVEL-TAX-001")["distinguish_result"], "PASS")


if __name__ == "__main__":
    unittest.main()
