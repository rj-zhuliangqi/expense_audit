from __future__ import annotations

import copy
import unittest
from pathlib import Path

from expense_audit_orchestrator.application import ReceiptAuditService
from expense_audit_orchestrator.profiles.travel.writeback import (
    travel_audit_travels_builder,
    travel_compliance_rule,
    travel_form_invoice_tax_views_builder,
)
from expense_audit_orchestrator.writeback import assemble_result_audit_info


class _CapturingRuntime:
    def __init__(self) -> None:
        self.inputs: list[dict] = []

    def evaluate(self, *, prepared_input, graph_path=None, graph_content=None):
        del graph_path, graph_content
        self.inputs.append(copy.deepcopy(prepared_input))
        tax_info = prepared_input["serviceData"]["travelAudit"]["taxInfo"]
        states = prepared_input["serviceData"]["travelAudit"]["ruleStates"]
        state = states.get("r37", states.get("travel_tax_amount"))
        return {
            "checkStatus": "warning" if state == "warning" else "passed",
            "decisionOutput": {
                "travel_travel_r37_检查发票可抵扣税额和表单税额是否相等_result": {
                    "reason_code": "E39",
                    "distinguish_result": "WARNING" if state == "warning" else "PASS",
                    "invoice_tax_total": tax_info.get("invoiceDeductibleTaxTotal"),
                }
            },
        }


class TravelCompletionTests(unittest.TestCase):
    def _travel_audit(self, tax: float) -> dict:
        return {
            "instanceCode": "INS-TRAVEL-001",
            "travelSegments": [
                {
                    "journeyId": "J-001",
                    "errandCode": "E-001",
                    "departureTime": "2026-08-01 08:00:00",
                    "arrivalTime": "2026-08-02 20:00:00",
                    "departure": "福州",
                    "destination": "北京",
                    "trafficType": "飞机",
                    "days": 2,
                    "isDomestic": True,
                    "travellerId": "U-001",
                    "travellerName": "张三",
                    "localTrafficApplyAmount": 80,
                    "trafficAmount": 1000,
                    "mileage": 0,
                    "mileageStandard": 0,
                    "hotelAmount": 500,
                    "otherAmount": 0,
                    "subsidyAmount": 200,
                    "airportReturnAmount": 150,
                    "baggageAmount": 0,
                }
            ],
            "taxInfo": {
                "invoiceDeductibleTax": tax,
                "formInputTax": 3,
            },
            "ruleStates": {"travel_tax_amount": "pass"},
            "sourceReady": True,
        }

    def test_travel_writeback_builders_and_compliance(self) -> None:
        service_data = {
            "auditInfo": {"instanceCode": "INS-TRAVEL-001"},
            "travelAudit": self._travel_audit(1),
        }
        travels = travel_audit_travels_builder([], service_data)
        self.assertEqual(len(travels), 1)
        self.assertEqual(travels[0]["miInstanceCode"], "INS-TRAVEL-001")
        self.assertEqual(travels[0]["journeyId"], "J-001")
        self.assertEqual(travels[0]["startPlace"], "福州")
        self.assertEqual(travels[0]["subsidyCost"], 200)

        invoice_pair = (
            {
                "invoiceKey": "F-001",
                "preparedInput": {
                    "invoiceNo": "INV-001",
                    "totalAmount": "1000",
                    "totalTaxAmount": "1",
                    "items": [{"taxRate": "0.01"}],
                    "invoice_info_id": "AII-001",
                    "serviceData": {
                        "currentInvoiceInfo": {"aiiid": "AII-001"},
                        "travelAudit": {
                            "taxInfo": {
                                "invoiceDeductibleTax": 1,
                                "currentInvoiceDeductibleTax": 1,
                            }
                        },
                    },
                },
            },
            {},
        )
        tax_rows = travel_form_invoice_tax_views_builder([invoice_pair], service_data)
        self.assertEqual(tax_rows[0]["invoiceNo"], "INV-001")
        self.assertEqual(tax_rows[0]["invoiceAmount"], 1000)
        self.assertEqual(tax_rows[0]["invoiceDeductibleTax"], 1)
        self.assertEqual(tax_rows[0]["invoiceInfoId"], "AII-001")

        self.assertFalse(travel_compliance_rule("充值卡", {}))
        self.assertFalse(travel_compliance_rule("*服务*保险", {}))
        self.assertTrue(travel_compliance_rule("住宿", {}))
        self.assertTrue(travel_compliance_rule("未知内容", {}))

    def test_writeback_payload_contains_travel_sections(self) -> None:
        prepared = {
            "receiptCode": "INS-TRAVEL-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "INS-TRAVEL-001"},
                "travelAudit": self._travel_audit(1),
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "F-001",
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "totalAmount": "1000",
                        "totalTaxAmount": "1",
                        "items": [{"taxRate": "0.01"}],
                        "invoice_info_id": "AII-001",
                        "serviceData": {
                            "currentInvoiceInfo": {"aiiid": "AII-001"},
                            "travelAudit": {"taxInfo": {"invoiceDeductibleTax": 1}},
                        },
                    },
                }
            ],
        }
        processed = {
            "receiptCode": "INS-TRAVEL-001",
            "serviceData": prepared["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "F-001",
                    "preparedInput": prepared["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {},
                    "decisionStatus": "passed",
                }
            ],
            "isAmountSufficient": True,
        }
        payload = assemble_result_audit_info(
            prepared,
            processed,
            compliance_rule=travel_compliance_rule,
            audit_travels_builder=travel_audit_travels_builder,
            form_invoice_tax_views_builder=travel_form_invoice_tax_views_builder,
            expense_profile="travel",
        )
        self.assertEqual(payload["auditTravels"][0]["journeyId"], "J-001")
        self.assertEqual(payload["formInvoiceTaxViews"][0]["invoiceNo"], "INV-001")

    def test_multi_invoice_tax_total_and_document_rule_dedup_context(self) -> None:
        runtime = _CapturingRuntime()
        service = ReceiptAuditService(
            runtime,
            object(),
            graph_content={"contentType": "application/vnd.gorules.decision", "nodes": [], "edges": []},
            run_id_factory=lambda: "run-1",
        )
        audit_one = self._travel_audit(1)
        # Make the first invoice raise the document-level tax rule; the
        # second invoice must receive the accumulated context and deduplicate
        # the same rule.
        audit_one["taxInfo"]["formInputTax"] = 4
        audit_two = self._travel_audit(2)
        prepared = {
            "receiptCode": "INS-TRAVEL-001",
            "receiptContext": {},
            "serviceData": {"auditInfo": {"instanceCode": "INS-TRAVEL-001"}, "travelAudit": audit_one},
            "invoicePreparations": [
                {
                    "invoiceKey": "F-001",
                    "invoiceFile": {"fid": "F-001", "invoiceNo": "INV-001"},
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "serviceData": {"travelAudit": audit_one},
                        "context": {"serviceData": copy.deepcopy({"travelAudit": audit_one})},
                    },
                },
                {
                    "invoiceKey": "F-002",
                    "invoiceFile": {"fid": "F-002", "invoiceNo": "INV-002"},
                    "preparedInput": {
                        "invoiceNo": "INV-002",
                        "serviceData": {"travelAudit": audit_two},
                        "context": {"serviceData": copy.deepcopy({"travelAudit": audit_two})},
                    },
                },
            ],
        }
        result = service.process_prepared_receipt(prepared)
        self.assertEqual(len(runtime.inputs), 2)
        first = runtime.inputs[0]["serviceData"]["travelAudit"]
        second = runtime.inputs[1]["serviceData"]["travelAudit"]
        self.assertTrue(first["primaryInvoice"])
        self.assertFalse(second["primaryInvoice"])
        self.assertEqual(first["taxInfo"]["invoiceDeductibleTaxTotal"], 3)
        self.assertEqual(second["taxInfo"]["invoiceDeductibleTaxTotal"], 3)
        self.assertEqual(second["raisedRuleCodes"], ["E39"])
        self.assertEqual(
            second["raisedRuleKeys"],
            ["travel_r37_检查发票可抵扣税额和表单税额是否相等"],
        )
        self.assertEqual(result["invoiceCount"], 2)


if __name__ == "__main__":
    unittest.main()
