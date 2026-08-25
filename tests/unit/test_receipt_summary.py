import unittest

from expense_audit_orchestrator.receipt_summary import (
    build_ai_audit_advice,
    build_ai_audit_summary,
    build_ai_audit_summary_finance,
)
from expense_audit_orchestrator.writeback import assemble_result_audit_info


class ReceiptSummaryTests(unittest.TestCase):
    def test_advice_uses_blocking_invoice_numbers_and_exact_shortage(self) -> None:
        prepared_receipt = {
            "serviceData": {"auditInfo": {"applyAmount": "1,180.00"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": {"invoiceNo": "24000122", "totalAmount": "100.00"},
                },
                {
                    "invoiceKey": "FID-002",
                    "preparedInput": {"invoiceNo": "240000118", "totalAmount": "100.00"},
                },
                {
                    "invoiceKey": "FID-003",
                    "preparedInput": {"invoiceNo": "24000115", "totalAmount": "100.00"},
                },
                {
                    "invoiceKey": "FID-004",
                    "preparedInput": {"invoiceNo": "24000116", "totalAmount": "3,560.00"},
                },
            ],
        }
        processed_receipt = {
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "reject",
                    "decisionOutput": {
                        "header_result": {
                            "reason_code": "E01",
                            "distinguish_result": "REJECT",
                        }
                    },
                },
                {
                    "invoiceKey": "FID-002",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "failed",
                    "decisionOutput": {
                        "header_result": {
                            "reason_code": "E02",
                            "distinguish_result": "FAILED",
                        }
                    },
                },
                {
                    "invoiceKey": "FID-003",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "reject",
                    "decisionOutput": {
                        "header_result": {
                            "reason_code": "E05",
                            "distinguish_result": "REJECT",
                        }
                    },
                },
                {
                    "invoiceKey": "FID-004",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "passed",
                    "decisionOutput": {"invoice_finalAmount": "1,143.19"},
                },
            ],
        }

        self.assertEqual(
            build_ai_audit_advice(prepared_receipt, processed_receipt),
            "本次报销捕捉3张问题发票,需要删除/重开发票24000122、240000118、24000115,"
            "待补充发票金额36.81元,期待下一次满分；"
            "存在REJECT稽核项，请根据稽核明细处理；"
            "存在FAILED稽核项，当前结果无法确认，请稍后重试或联系管理员处理",
        )

    def test_advice_keeps_warning_and_e34_valid_without_listing_them(self) -> None:
        prepared_receipt = {
            "serviceData": {"auditInfo": {"applyAmount": "200.00"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "WARNING",
                    "preparedInput": {"invoiceNo": "WARN-001", "totalAmount": "100.00"},
                },
                {
                    "invoiceKey": "E34",
                    "preparedInput": {"invoiceNo": "E34-001", "totalAmount": "100.00"},
                },
            ],
        }
        processed_receipt = {
            "invoiceResults": [
                {
                    "invoiceKey": "WARNING",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "warning",
                    "decisionOutput": {"invoice_finalAmount": "100.00"},
                },
                {
                    "invoiceKey": "E34",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "reject",
                    "decisionOutput": {
                        "invoice_content_valid_result": {
                            "reason_code": "E34",
                            "distinguish_result": "REJECT",
                            "invoice_finalAmount": "63.19",
                        }
                    },
                },
            ],
        }

        self.assertEqual(
            build_ai_audit_advice(prepared_receipt, processed_receipt),
            "本次审核存在REJECT稽核项，请根据稽核明细处理；"
            "存在WARNING稽核项，请根据稽核明细进行人工复核；"
            "待补充发票金额36.81元",
        )

    def test_advice_does_not_mask_reject_or_warning_in_legacy_audit_logs(self) -> None:
        prepared_receipt = {
            "serviceData": {"auditInfo": {"applyAmount": "200.00"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "totalAmount": "1778.00",
                    },
                }
            ],
        }
        # 历史回写/重放数据没有 invoiceResults，只有已经展开的 auditLogs。
        processed_receipt = {
            "auditLogs": [
                {"reasonCode": "E31", "distinguishResult": "reject"},
                {"reasonCode": "W33", "distinguishResult": "warning"},
            ],
            "aiAuditAdvice": "本次发票全部通过！",
        }

        advice = build_ai_audit_advice(prepared_receipt, processed_receipt)
        self.assertIsNotNone(advice)
        self.assertNotIn("本次发票全部通过", advice or "")
        self.assertIn("REJECT", advice or "")
        self.assertIn("WARNING", advice or "")

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)
        self.assertNotIn("本次发票全部通过", payload["aiAuditAdvice"])
        self.assertIn("REJECT", payload["aiAuditAdvice"])
        self.assertIn("WARNING", payload["aiAuditAdvice"])

    def test_advice_uses_invoice_number_fallback_priority(self) -> None:
        prepared_receipt = {
            "serviceData": {"auditInfo": {"applyAmount": "100.00"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"invoiceNo": "FILE-INVOICE-001"},
                    "preparedInput": {"chequeNo": "CHEQUE-001", "totalAmount": "10.00"},
                },
            ],
        }
        processed_receipt = {
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "reject",
                    "preparedInput": {"serialNo": "SERIAL-001", "totalAmount": "10.00"},
                    "decisionOutput": {
                        "header_result": {
                            "reason_code": "E01",
                            "distinguish_result": "REJECT",
                        }
                    },
                }
            ],
        }

        advice = build_ai_audit_advice(prepared_receipt, processed_receipt)
        self.assertIn("需要删除/重开发票SERIAL-001", advice or "")

    def test_advice_returns_none_without_application_amount(self) -> None:
        self.assertIsNone(
            build_ai_audit_advice(
                {"invoicePreparations": []},
                {"invoiceResults": []},
            )
        )

    def test_summary_uses_exact_decimal_formula_and_keeps_e34_deduction(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-SUMMARY-001",
            "serviceData": {"auditInfo": {"applyAmount": "1,180.00"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": {"totalAmount": "1,000.00"},
                },
                {
                    "invoiceKey": "FID-002",
                    "preparedInput": {"totalAmount": "2,860.00"},
                },
            ],
        }
        processed_receipt = {
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "passed",
                    "preparedInput": {"totalAmount": "1,000.00"},
                    "decisionOutput": {"invoice_finalAmount": "1,000.00"},
                },
                {
                    "invoiceKey": "FID-002",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "reject",
                    "preparedInput": {"totalAmount": "2,860.00"},
                    "decisionOutput": {
                        "invoice_content_valid_result": {
                            "reason_code": "E34",
                            "distinguish_result": "REJECT",
                            "invoice_finalAmount": "143.19",
                        }
                    },
                },
            ],
        }

        self.assertEqual(
            build_ai_audit_summary(prepared_receipt, processed_receipt),
            "本次报销申请总金额1,180.00元|提交发票总金额3,860.00元|"
            "发票有效可报销金额1,143.19元|发票待补充金额36.81元",
        )

    def test_rejected_invoice_is_included_in_submitted_total_but_not_valid_total(self) -> None:
        prepared_receipt = {
            "serviceData": {"auditInfo": {"applyAmount": 100}},
            "invoicePreparations": [
                {"invoiceKey": "VALID", "preparedInput": {"totalAmount": 150}},
                {"invoiceKey": "REJECTED", "preparedInput": {"totalAmount": 200}},
            ],
        }
        processed_receipt = {
            "invoiceResults": [
                {
                    "invoiceKey": "VALID",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "warning",
                    "decisionOutput": {"invoice_finalAmount": 80},
                },
                {
                    "invoiceKey": "REJECTED",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "reject",
                    "decisionOutput": {
                        "header_result": {
                            "reason_code": "E01",
                            "distinguish_result": "REJECT",
                        },
                        "invoice_finalAmount": 200,
                    },
                },
            ],
        }

        self.assertEqual(
            build_ai_audit_summary(prepared_receipt, processed_receipt),
            "本次报销申请总金额100.00元|提交发票总金额350.00元|"
            "发票有效可报销金额80.00元|发票待补充金额20.00元",
        )

    def test_submitted_total_only_uses_total_amount_without_alias_fallback(self) -> None:
        prepared_receipt = {
            "serviceData": {"auditInfo": {"applyAmount": "100.00"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": {"amount": "88.00", "invoiceAmount": "99.00"},
                },
            ],
        }
        processed_receipt = {
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "passed",
                    "decisionOutput": {"invoice_finalAmount": "88.00"},
                }
            ],
        }

        self.assertEqual(
            build_ai_audit_summary(prepared_receipt, processed_receipt),
            "本次报销申请总金额100.00元|提交发票总金额0.00元|"
            "发票有效可报销金额88.00元|发票待补充金额12.00元",
        )


    def test_finance_summary_classifies_audit_points_by_code_and_pass_status(self) -> None:
        catalog = {
            "W29": {"riskLevel": "medium_low"},
            "W36": {"riskLevel": "high"},
        }
        audit_logs = [
            {"reasonCode": "E01", "distinguishResult": "reject"},
            {"reasonCode": "W29", "distinguishResult": "warning"},
            {"reasonCode": "W36", "distinguishResult": "reject"},
            {"reasonCode": "E02", "distinguishResult": "pass"},
            {"reasonCode": "E05", "distinguishResult": "PASSED"},
        ]

        summary = build_ai_audit_summary_finance(
            {},
            {},
            audit_logs=audit_logs,
            audit_risk_catalog=catalog,
            expense_profile="personal_transport",
        )

        self.assertEqual(
            summary,
            "本单高风险 1 项、中低风险 1 项，阻断 1 项，已通过 2 项稽核项。",
        )

    def test_finance_summary_supports_telecom_and_entertainment_profiles(self) -> None:
        audit_logs = [
            {"reasonCode": "W28", "distinguishResult": "WARNING"},
            {"reasonCode": "W32", "distinguishResult": "REJECT"},
            {"reasonCode": "W40", "distinguishResult": "REJECT"},
            {"reasonCode": "E01", "distinguishResult": "PASS"},
        ]
        telecom_summary = build_ai_audit_summary_finance(
            {},
            {},
            audit_logs=audit_logs,
            audit_risk_catalog={
                "W28": {"riskLevel": "medium_low"},
                "W32": {"riskLevel": "high"},
                "W40": {"riskLevel": "medium_low"},
            },
            expense_profile="telecom",
        )
        self.assertEqual(
            telecom_summary,
            "本单高风险 1 项、中低风险 2 项，阻断 0 项，已通过 1 项稽核项。",
        )

        entertainment_summary = build_ai_audit_summary_finance(
            {},
            {},
            audit_logs=[
                {"reasonCode": "W33", "distinguishResult": "WARNING"},
                {"reasonCode": "W34", "distinguishResult": "WARNING"},
                {"reasonCode": "W31", "distinguishResult": "PASS"},
                {"reasonCode": "E36", "distinguishResult": "REJECT"},
            ],
            audit_risk_catalog={
                "W33": {"riskLevel": "medium_low"},
                "W34": {"riskLevel": "high"},
                "W31": {"riskLevel": "high"},
            },
            expense_profile="entertainment",
        )
        self.assertEqual(
            entertainment_summary,
            "本单高风险 1 项、中低风险 1 项，阻断 1 项，已通过 1 项稽核项。",
        )

    def test_finance_summary_is_not_added_for_travel_profile(self) -> None:
        summary = build_ai_audit_summary_finance(
            {},
            {},
            audit_logs=[{"reasonCode": "E01", "distinguishResult": "REJECT"}],
            audit_risk_catalog={"E01": {"riskLevel": "blocking"}},
            expense_profile="travel",
        )

        self.assertIsNone(summary)

    def test_finance_summary_forces_all_e_codes_to_blocking_and_unconfigured_w_to_high(self) -> None:
        summary = build_ai_audit_summary_finance(
            {},
            {},
            audit_logs=[
                {"reasonCode": "E01", "distinguishResult": "reject"},
                {"reasonCode": "W99", "distinguishResult": "warning"},
            ],
            audit_risk_catalog={"E01": {"riskLevel": "high"}},
            expense_profile="personal_transport",
        )

        self.assertEqual(
            summary,
            "本单高风险 1 项、中低风险 0 项，阻断 1 项，已通过 0 项稽核项。",
        )

    def test_finance_summary_counts_receipt_level_e31_only_on_last_invoice(self) -> None:
        prepared_receipt = {
            "invoicePreparations": [
                {"invoiceKey": "F-1", "preparedInput": {}},
                {"invoiceKey": "F-2", "preparedInput": {}},
            ]
        }
        processed_receipt = {
            "invoiceResults": [
                {
                    "invoiceKey": "F-1",
                    "decisionOutput": {
                        "amount_result": {
                            "reason_code": "E31",
                            "distinguish_result": "REJECT",
                        }
                    },
                },
                {
                    "invoiceKey": "F-2",
                    "decisionOutput": {
                        "amount_result": {
                            "reason_code": "E31",
                            "distinguish_result": "PASS",
                        }
                    },
                },
            ]
        }

        summary = build_ai_audit_summary_finance(
            prepared_receipt,
            processed_receipt,
            audit_risk_catalog={"E31": {"riskLevel": "blocking"}},
            expense_profile="personal_transport",
        )

        self.assertEqual(
            summary,
            "本单高风险 0 项、中低风险 0 项，阻断 0 项，已通过 1 项稽核项。",
        )

    def test_writeback_payload_contains_finance_summary(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-FINANCE-SUMMARY-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-FINANCE-SUMMARY-001"},
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": {},
                },
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-FINANCE-SUMMARY-001",
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "executionStatus": "SUCCEEDED",
                    "decisionOutput": {
                        "header_result": {
                            "reason_code": "W29",
                            "distinguish_result": "WARNING",
                        },
                        "tax_result": {
                            "reason_code": "W36",
                            "distinguish_result": "PASS",
                        },
                        "date_result": {
                            "reason_code": "E33",
                            "distinguish_result": "REJECT",
                        },
                    },
                },
            ],
        }

        payload = assemble_result_audit_info(
            prepared_receipt,
            processed_receipt,
            expense_profile="personal_transport",
            audit_risk_catalog={
                "W29": {"riskLevel": "medium_low"},
                "W36": {"riskLevel": "high"},
            },
        )

        self.assertEqual(
            payload["aiAuditSummaryFinance"],
            "本单高风险 0 项、中低风险 1 项，阻断 1 项，已通过 1 项稽核项。",
        )

    def test_writeback_payload_contains_summary_when_receipt_result_did_not_precompute_it(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-SUMMARY-WRITEBACK-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-SUMMARY-WRITEBACK-001", "applyAmount": "100.00"},
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {"totalAmount": "120.00"},
                },
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-SUMMARY-WRITEBACK-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "passed",
                    "preparedInput": {"totalAmount": "120.00"},
                    "decisionOutput": {"invoice_finalAmount": "95.25"},
                }
            ],
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        self.assertEqual(
            payload["aiAuditSummary"],
            "本次报销申请总金额100.00元|提交发票总金额120.00元|"
            "发票有效可报销金额95.25元|发票待补充金额4.75元",
        )

    def test_external_gift_detail_failure_cannot_be_summarized_as_pass(self) -> None:
        prepared_receipt = {
            "serviceData": {
                "auditInfo": {"applyAmount": "100.00"},
                "entertainment_data": {
                    "giftDetailLookupStatus": "error",
                    "giftDetailLookupError": "业务费用明细服务不可用",
                },
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-LOOKUP-ERROR",
                    "preparedInput": {"invoiceNo": "INV-LOOKUP-ERROR", "totalAmount": "100.00"},
                }
            ],
        }
        processed_receipt = {
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-LOOKUP-ERROR",
                    "executionStatus": "SUCCEEDED",
                    "decisionStatus": "passed",
                    "decisionOutput": {"invoice_finalAmount": "100.00"},
                }
            ],
        }

        advice = build_ai_audit_advice(prepared_receipt, processed_receipt)

        self.assertIsNotNone(advice)
        self.assertIn("业务费用明细接口异常", advice)
        self.assertIn("WARNING", advice)
        self.assertNotIn("本次发票全部通过", advice)

    def test_prebuilt_model_failure_rules_are_visible_in_final_advice(self) -> None:
        prepared_receipt = {
            "serviceData": {"auditInfo": {"applyAmount": "100.00"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-MODEL-ERROR",
                    "preparedInput": {"invoiceNo": "INV-MODEL-ERROR", "totalAmount": "100.00"},
                }
            ],
        }

        for reason_code, expected_status in (
            ("E17", "REJECT"),
            ("W40", "REJECT"),
            ("W32", "REJECT"),
            ("E34", "REJECT"),
            ("E36", "REJECT"),
            ("W31", "WARNING"),
        ):
            with self.subTest(reason_code=reason_code):
                processed_receipt = {
                    "invoiceResults": [],
                    "auditLogs": [
                        {
                            "reasonCode": reason_code,
                            "distinguishResult": expected_status,
                            "message": "模型服务暂时异常，请稍后重试或联系管理员处理。",
                        }
                    ],
                }
                advice = build_ai_audit_advice(prepared_receipt, processed_receipt)

                self.assertIn("模型服务异常", advice)
                self.assertIn(expected_status, advice)
                self.assertNotIn("本次发票全部通过", advice)


if __name__ == "__main__":
    unittest.main()
