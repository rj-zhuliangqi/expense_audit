from __future__ import annotations

import json
import unittest
from pathlib import Path

from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision
from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS


_AMOUNT_NODE_ID = "dcc29290-4d5e-4eb9-8e77-eee31820529a"
_E31_CODE = "E31"


class EorE31GraphTests(unittest.TestCase):
    def _base_input(self) -> dict:
        return {
            "receipt": {"code": "REC-EOR-E31-001"},
            "context": {},
            "invoiceNo": "INV-EOR-001",
            "invoiceType": "火车票",
            "buyerName": "正确公司",
            "buyerTaxNo": "GOOD-TAX",
            "salerName": "正常销方",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2026-01-01",
            "verifyResult": [],
            "items": [],
            "serviceData": {
                "expenseInvoiceTypes": [{"manufacturerBillCode": "火车票"}],
                "companyList": [
                    {"ccode": "C1", "companyName": "正确公司", "companyTax": "GOOD-TAX"}
                ],
                "companyBlacklist": [],
                "invoiceUsageHistory": [],
                "auditInfo": {
                    "instanceComCode": "C1",
                    "applyAmount": 150,
                    "submitTime": "2026-01-01",
                    "isEor": "0",
                },
                "entertainment_data": {
                    "giftDetailLookupStatus": "success",
                    "hasGiftItem": False,
                    "giftReceptionCount": 0,
                },
                "entertainmentInvoiceSerial": {
                    "isTaxiInvoice": False,
                    "historyHit": False,
                    "batchHit": False,
                    "lookupFailed": False,
                },
            },
        }

    def _e31_result(self, graph_path: Path, *, is_eor: str, total_amount: int) -> dict:
        prepared = self._base_input()
        prepared["serviceData"]["auditInfo"]["isEor"] = is_eor
        prepared["totalAmount"] = total_amount
        prepared["invoiceAmount"] = total_amount
        result = evaluate_prepared_input(
            load_decision(graph_path),
            prepared,
            trace=False,
        )
        for value in result["decisionOutput"].values():
            if isinstance(value, dict) and value.get("reason_code") == _E31_CODE:
                return value
        self.fail(f"E31 result not found in {graph_path}")

    def test_official_graphs_apply_eor_status_matrix(self) -> None:
        for profile, graph_path in OFFICIAL_GRAPH_PATHS.items():
            if profile not in {"telecom", "personal_transport", "entertainment"}:
                continue
            with self.subTest(profile=profile):
                self.assertEqual(
                    self._e31_result(graph_path, is_eor="0", total_amount=100)["distinguish_result"],
                    "REJECT",
                )
                self.assertEqual(
                    self._e31_result(graph_path, is_eor="1", total_amount=100)["distinguish_result"],
                    "WARNING",
                )
                e31 = self._e31_result(graph_path, is_eor="1", total_amount=200)
                self.assertEqual(e31["distinguish_result"], "PASS")
                self.assertEqual(e31["reason_code"], _E31_CODE)

    def test_amount_node_has_eor_input_and_four_explicit_rules(self) -> None:
        for profile in ("telecom", "personal_transport", "entertainment"):
            graph = json.loads(OFFICIAL_GRAPH_PATHS[profile].read_text(encoding="utf-8"))
            amount_node = next(node for node in graph["nodes"] if node.get("id") == _AMOUNT_NODE_ID)
            self.assertEqual(
                {item["field"] for item in amount_node["content"]["inputs"]},
                {"isAmountEnough", "isEor"},
            )
            self.assertEqual(len(amount_node["content"]["rules"]), 4)
