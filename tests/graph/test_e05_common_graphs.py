from __future__ import annotations

import json
import unittest

from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision_from_content


E05_NODE_ID = "2ea2f963-44fc-4130-9632-af048b76d0b1"
E05_INPUT_FIELDS = {"isWriteOff", "isReceiptInvoiceDuplicate"}
HISTORY = [
    {
        "chequeNo": "INV-E05-001",
        "miInstanceCode": "REC-OLD-001",
        "estimatedTotalAmount": 100,
    }
]


def _isolated_decision(node: dict) -> object:
    request = {
        "type": "inputNode",
        "content": {"schema": ""},
        "id": "request",
        "name": "request",
        "position": {"x": 0, "y": 0},
    }
    response = {
        "type": "outputNode",
        "content": {"schema": ""},
        "id": "response",
        "name": "response",
        "position": {"x": 500, "y": 0},
    }
    return load_decision_from_content(
        {
            "contentType": "application/vnd.gorules.decision",
            "nodes": [request, node, response],
            "edges": [
                {"id": "edge-request-node", "sourceId": "request", "targetId": node["id"], "type": "edge"},
                {"id": "edge-node-response", "sourceId": node["id"], "targetId": "response", "type": "edge"},
            ],
        }
    )


class CommonE05GraphTests(unittest.TestCase):
    def test_all_non_travel_profiles_expose_the_shared_two_inputs(self) -> None:
        for profile in ("telecom", "personal_transport", "entertainment"):
            with self.subTest(profile=profile):
                graph = json.loads(OFFICIAL_GRAPH_PATHS[profile].read_text(encoding="utf-8"))
                node = next(item for item in graph["nodes"] if item.get("id") == E05_NODE_ID)
                self.assertEqual(
                    {item["field"] for item in node["content"]["inputs"]},
                    E05_INPUT_FIELDS,
                )
                preprocess = next(
                    item
                    for item in graph["nodes"]
                    if item.get("type") == "expressionNode"
                    and any(
                        expression.get("key") == "isWriteOff"
                        for expression in item.get("content", {}).get("expressions", [])
                    )
                )
                expression_keys = {
                    expression["key"]
                    for expression in preprocess["content"]["expressions"]
                }
                self.assertIn("isReceiptInvoiceDuplicate", expression_keys)

    def test_shared_profiles_reject_history_receipt_and_combined_duplicates(self) -> None:
        scenarios = (
            ("pass", True, False, [], "PASS", ""),
            (
                "history",
                False,
                False,
                HISTORY,
                "REJECT",
                "票据 发票号 INV-E05-001 已在以下报销单中使用过：REC-OLD-001；已报销金额为 100 元，不能再次用于本次报销。",
            ),
            (
                "receipt",
                True,
                True,
                [],
                "REJECT",
                "票据 发票号 INV-E05-001 在本核销单内重复出现，不能再次用于本次报销。",
            ),
            (
                "both",
                False,
                True,
                HISTORY,
                "REJECT",
                "票据 发票号 INV-E05-001 在本核销单内重复出现，且已在以下报销单中使用过：REC-OLD-001；已报销金额为 100 元，不能再次用于本次报销。",
            ),
        )
        for profile in ("telecom", "personal_transport", "entertainment"):
            graph = json.loads(OFFICIAL_GRAPH_PATHS[profile].read_text(encoding="utf-8"))
            node = next(item for item in graph["nodes"] if item.get("id") == E05_NODE_ID)
            decision = _isolated_decision(node)
            output_path = node["content"]["outputPath"]
            for scenario, is_writeoff, is_receipt_duplicate, history, result, message in scenarios:
                with self.subTest(profile=profile, scenario=scenario):
                    evaluated = evaluate_prepared_input(
                        decision,
                        {
                            "isWriteOff": is_writeoff,
                            "isReceiptInvoiceDuplicate": is_receipt_duplicate,
                            "invoiceNo": "INV-E05-001",
                            "serviceData": {"invoiceUsageHistory": history},
                        },
                        trace=False,
                    )
                    e05 = evaluated["decisionOutput"][output_path]
                    self.assertEqual(e05["distinguish_result"], result)
                    self.assertEqual(e05["message"], message)
                    self.assertNotRegex(e05["message"], r"\{[^{}]+\}")

    def test_travel_adapter_uses_the_same_four_duplicate_outcomes(self) -> None:
        graph = json.loads(OFFICIAL_GRAPH_PATHS["travel"].read_text(encoding="utf-8"))
        node = next(item for item in graph["nodes"] if item.get("id") == "travel_e05_duplicate_check")
        decision = _isolated_decision(node)
        scenarios = (
            ("pass", [], "PASS", ""),
            (
                "reject",
                HISTORY,
                "REJECT",
                "票据 发票号 INV-E05-001 已在以下报销单中使用过：REC-OLD-001；已报销金额为 100 元，不能再次用于本次报销。",
            ),
            (
                "reject_receipt",
                [],
                "REJECT",
                "票据 发票号 INV-E05-001 在本核销单内重复出现，不能再次用于本次报销。",
            ),
            (
                "reject_both",
                HISTORY,
                "REJECT",
                "票据 发票号 INV-E05-001 在本核销单内重复出现，且已在以下报销单中使用过：REC-OLD-001；已报销金额为 100 元，不能再次用于本次报销。",
            ),
        )
        for state, history, result, message in scenarios:
            with self.subTest(state=state):
                evaluated = evaluate_prepared_input(
                    decision,
                    {
                        "travel_e05_duplicate_state": state,
                        "invoiceNo": "INV-E05-001",
                        "serviceData": {"invoiceUsageHistory": history},
                    },
                    trace=False,
                )
                e05 = evaluated["decisionOutput"][node["content"]["outputPath"]]
                self.assertEqual(e05["distinguish_result"], result)
                self.assertEqual(e05["message"], message)
                self.assertNotRegex(e05["message"], r"\{[^{}]+\}")
