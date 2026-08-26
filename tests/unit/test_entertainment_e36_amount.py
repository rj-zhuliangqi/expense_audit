import unittest

from expense_audit_orchestrator.application import (
    _extract_invoice_final_amount,
    _invoice_has_unresolved_e36_amount,
)
from expense_audit_orchestrator.receipt_summary import (
    extract_valid_invoice_final_amount,
    invoice_contributes_valid_amount,
)
from expense_audit_orchestrator.writeback import assemble_result_audit_info


def _invoice_result(e36_amount=5000):
    e36_result = {
        "reason_code": "E36",
        "distinguish_result": "PASS",
    }
    if e36_amount is not None:
        e36_result["invoice_finalAmount"] = e36_amount

    return {
        "invoiceKey": "FID-E36-001",
        "decisionOutput": {
            "amount_result": {
                "reason_code": "E31",
                "distinguish_result": "REJECT",
            },
            # 业务招待费图的金额位于这个路径，而不是 decisionOutput 顶层。
            "content_compliance_result": e36_result,
        },
        "executionStatus": "SUCCEEDED",
        "decisionStatus": "reject",
    }


def _receipt(processed_invoice, *, is_amount_sufficient=True):
    prepared = {
        "receiptCode": "REC-ENT-E36-001",
        "serviceData": {
            "auditInfo": {
                "instanceCode": "REC-ENT-E36-001",
                "applyAmount": 5000,
                "isEor": "0",
            }
        },
        "invoicePreparations": [
            {
                "invoiceKey": "FID-E36-001",
                "preparedInput": {
                    "invoiceNo": "INV-E36-001",
                    "totalAmount": 5000,
                    "serviceData": {
                        "currentInvoiceInfo": {"aiiid": "AIIID-E36-001"},
                        "currentAuditInvoiceFile": {"afiid": "AFID-E36-001"},
                    },
                },
            }
        ],
    }
    processed = {
        "receiptCode": "REC-ENT-E36-001",
        "serviceData": prepared["serviceData"],
        "invoiceResults": [processed_invoice],
        "applyAmount": 5000,
        "validInvoiceTotal": 5000,
        "isAmountSufficient": is_amount_sufficient,
    }
    return prepared, processed


class EntertainmentE36AmountTests(unittest.TestCase):
    def test_content_compliance_amount_is_used_for_e31(self) -> None:
        invoice_result = _invoice_result()

        self.assertEqual(extract_valid_invoice_final_amount(invoice_result), 5000)
        self.assertEqual(_extract_invoice_final_amount(invoice_result), 5000.0)
        self.assertFalse(_invoice_has_unresolved_e36_amount(invoice_result))
        self.assertTrue(invoice_contributes_valid_amount(invoice_result))

        prepared, processed = _receipt(invoice_result)
        payload = assemble_result_audit_info(
            prepared,
            processed,
            expense_profile="entertainment",
        )
        e31_log = next(row for row in payload["auditLogs"] if row["reasonCode"] == "E31")
        self.assertEqual(e31_log["distinguishResult"], "pass")
        self.assertEqual(
            payload["aiAuditSummary"],
            "本次报销申请总金额5,000.00元|提交发票总金额5,000.00元|"
            "发票有效可报销金额5,000.00元|发票待补充金额0.00元",
        )

    def test_missing_e36_amount_remains_unresolved(self) -> None:
        invoice_result = _invoice_result(e36_amount=None)

        self.assertIsNone(extract_valid_invoice_final_amount(invoice_result))
        self.assertTrue(_invoice_has_unresolved_e36_amount(invoice_result))
        self.assertFalse(invoice_contributes_valid_amount(invoice_result))


if __name__ == "__main__":
    unittest.main()
