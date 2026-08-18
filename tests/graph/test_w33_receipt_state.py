import unittest

from expense_audit_orchestrator.application import (
    _resolve_gift_count_context,
    _update_gift_count_state,
)
from expense_audit_orchestrator.writeback import assemble_result_audit_info


class W33ReceiptStateTests(unittest.TestCase):
    def test_state_accumulates_goods_count_by_invoice(self) -> None:
        prepared_receipt = {
            "serviceData": {
                "entertainment_data": {
                    "hasGiftItem": True,
                    "giftReceptionCount": 10,
                }
            }
        }
        self.assertEqual(_resolve_gift_count_context(prepared_receipt), (True, 10.0))

        first = {
            "preparedInput": {"items": [{"num": "4"}]},
        }
        second = {
            "preparedInput": {"items": [{"num": "3"}]},
        }

        _update_gift_count_state(
            first,
            cumulative_goods_count=4,
            gift_reception_count=10,
            is_last_invoice=False,
        )
        _update_gift_count_state(
            second,
            cumulative_goods_count=7,
            gift_reception_count=10,
            is_last_invoice=True,
        )

        self.assertEqual(first["preparedInput"]["totalGoodsCount"], 4)
        self.assertEqual(first["preparedInput"]["giftRemainingReceptionCount"], 6)
        self.assertFalse(first["preparedInput"]["isLastInvoice"])
        self.assertEqual(second["preparedInput"]["totalGoodsCount"], 7)
        self.assertEqual(second["preparedInput"]["giftRemainingReceptionCount"], 3)
        self.assertTrue(second["preparedInput"]["isLastInvoice"])

    def test_writeback_keeps_w33_only_on_last_invoice(self) -> None:
        preparations = [self._preparation("FID-1", "INV-1"), self._preparation("FID-2", "INV-2")]
        processed = {
            "receiptCode": "REC-W33-001",
            "serviceData": {},
            "isGiftCountReasonable": False,
            "giftReceptionCount": 10,
            "totalGoodsCount": 7,
            "invoiceResults": [
                self._result(preparations[0]),
                self._result(preparations[1]),
            ],
        }

        payload = assemble_result_audit_info(
            {
                "receiptCode": "REC-W33-001",
                "serviceData": {
                    "auditInfo": {"instanceCode": "REC-W33-001"},
                    "auditInvoiceFiles": [],
                },
                "invoicePreparations": preparations,
            },
            processed,
            audit_rule_catalog={
                "W33": {"auditContent": "检查礼品数量与接待人数的合理性"},
            },
        )

        w33_logs = [item for item in payload["auditLogs"] if item["reasonCode"] == "W33"]
        self.assertEqual(len(w33_logs), 1)
        self.assertEqual(w33_logs[0]["invoiceInfoId"], "AII-FID-2")
        self.assertEqual(w33_logs[0]["distinguishResult"], "warning")
        self.assertIn("【7】少于", w33_logs[0]["message"])

        self.assertIsNone(payload["auditInvoiceInfos"][0]["reasonCode"])
        self.assertEqual(payload["auditInvoiceInfos"][1]["reasonCode"], "W33")

    @staticmethod
    def _preparation(fid: str, invoice_no: str) -> dict:
        return {
            "invoiceKey": fid,
            "invoiceFile": {
                "fid": fid,
                "auditInvoiceFile": {"fid": fid, "afiid": f"AF-{fid}"},
            },
            "preparedInput": {
                "invoiceNo": invoice_no,
                "serviceData": {
                    "currentInvoiceInfo": {"aiiid": f"AII-{fid}"},
                    "currentAuditInvoiceFile": {"fid": fid, "afiid": f"AF-{fid}"},
                },
            },
        }

    @staticmethod
    def _result(preparation: dict) -> dict:
        return {
            "invoiceKey": preparation["invoiceKey"],
            "preparedInput": preparation["preparedInput"],
            "executionStatus": "SUCCEEDED",
            "decisionOutput": {
                "gift_count_result": {
                    "reason_code": "W33",
                    "distinguish_result": "WARNING",
                    "audit_content": "礼品数量合理性",
                    "message": "原始结果",
                }
            },
        }


if __name__ == "__main__":
    unittest.main()
