from __future__ import annotations

import unittest
from pathlib import Path

from apps.builders.entertainment_graph import build_entertainment_graph
from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision


from expense_audit_orchestrator.paths import PROJECT_ROOT as ROOT
GRAPH_PATHS = (
    ROOT / "graph-latest-0727-1900.json",
    ROOT / "graph-latest-entertainment-0722.json",
)
INPUT_FIELD_ID = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"
OUTPUT_RESULT_ID = "f35ede49-0eae-4dda-b39e-11a11383697a"
REASON_CODE_ID = "48a29115-f542-44d3-8c02-3ff71e19ee38"


def _prepared_input(invoice_type: str) -> dict:
    return {
        "receipt": {"code": "E35-INVOICE-TYPE-TEST"},
        "context": {},
        "invoiceNo": "INV-E35-001",
        "invoiceType": invoice_type,
        "invoice_file_id": "file-e35-001",
        "invoice_info_id": "info-e35-001",
        "invoiceAmount": 100,
        "totalAmount": 100,
        "invoiceDate": "2026-01-01",
        "items": [],
        "verifyResult": [],
        "serviceData": {
            "expenseInvoiceTypes": [
                {"manufacturerBillCode": "26", "manufacturerBillName": "数电普票"}
            ],
            "companyList": [],
            "companyBlacklist": [],
            "invoiceUsageHistory": [],
            "auditInfo": {
                "instanceCode": "INSTANCE-E35-TEST",
                "instanceComCode": "",
                "applyAmount": 100,
                "submitTime": "2026-01-01",
                "verifiUserName": "张三",
            },
            "entertainment_data": {"hasGiftItem": False, "giftReceptionCount": 0},
            "e15InvoiceType": {"isApplicable": False},
            "entertainmentInvoiceSerial": {
                "invoiceNo": "INV-E35-001",
                "historyNumbers": [],
                "historyHit": False,
                "batchHit": False,
                "isTaxiInvoice": False,
                "lookupFailed": False,
                "relationDescription": "",
            },
        },
    }


def _e35(result: dict) -> dict:
    for value in result["decisionOutput"].values():
        if isinstance(value, dict) and value.get("reason_code") == "E35":
            return value
    raise AssertionError("E35 result not found")


class E35InvoiceTypeGraphTests(unittest.TestCase):
    def test_formal_graphs_compare_manufacturer_bill_code_via_boolean_preprocessing(self) -> None:
        for graph_path in GRAPH_PATHS:
            with self.subTest(graph=graph_path.name):
                decision = load_decision(graph_path)

                allowed = _e35(
                    evaluate_prepared_input(
                        decision, _prepared_input("26"), trace=False
                    )
                )
                self.assertEqual(allowed["distinguish_result"], "PASS")

                rejected = _e35(
                    evaluate_prepared_input(
                        decision, _prepared_input("99"), trace=False
                    )
                )
                self.assertEqual(rejected["distinguish_result"], "REJECT")
                self.assertIn("INV-E35-001", rejected["message"])

                # The display name is not the value returned by Kingdee and must
                # not be compared with invoiceType.
                display_name = _e35(
                    evaluate_prepared_input(
                        decision, _prepared_input("数电普票"), trace=False
                    )
                )
                self.assertEqual(display_name["distinguish_result"], "REJECT")

    def test_formal_graphs_wire_e35_to_boolean_expression(self) -> None:
        for graph_path in GRAPH_PATHS:
            with self.subTest(graph=graph_path.name):
                graph = __import__("json").loads(graph_path.read_text(encoding="utf-8"))
                node = next(node for node in graph["nodes"] if node["id"] == "invoice_type_check")
                self.assertEqual(node["content"]["inputs"][0]["field"], "isInvoicType")
                rules = {
                    rule[INPUT_FIELD_ID]: rule[OUTPUT_RESULT_ID]
                    for rule in node["content"]["rules"]
                    if rule[REASON_CODE_ID] == '"E35"'
                }
                self.assertEqual(rules, {"true": '"PASS"', "false": '"REJECT"'})

    def test_generated_entertainment_graph_keeps_the_same_e35_contract(self) -> None:
        graph = build_entertainment_graph()
        node = next(node for node in graph["nodes"] if node["id"] == "invoice_type_check")
        self.assertEqual(node["content"]["inputs"][0]["field"], "isInvoicType")
        rules = {
            rule[INPUT_FIELD_ID]: rule[OUTPUT_RESULT_ID]
            for rule in node["content"]["rules"]
            if rule[REASON_CODE_ID] == '"E35"'
        }
        self.assertEqual(rules, {"true": '"PASS"', "false": '"REJECT"'})
        reject_rule = next(
            rule for rule in node["content"]["rules"] if rule[INPUT_FIELD_ID] == "false"
        )
        self.assertIn("业务招待费报销允许", reject_rule["509fd9ba-3996-4e4a-9021-df6513ed6807"])


if __name__ == "__main__":
    unittest.main()
