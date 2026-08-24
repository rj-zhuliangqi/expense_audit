import unittest
import json

from expense_audit_orchestrator.writeback import (
    assemble_result_audit_info,
    _build_e31_message,
    _format_amount,
)
from expense_audit_orchestrator.profiles.telecom.writeback import telecom_compliance_rule


class WritebackAssemblerTests(unittest.TestCase):
    def test_assemble_result_audit_info_maps_current_receipt_sources(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-WRITEBACK-001",
            "serviceData": {
                "auditInfo": {
                    "instanceCode": "REC-WRITEBACK-001",
                    "verifiUserId": "u-001",
                    "verifiUserCompanyName": "锐捷网络股份有限公司",
                },
                "auditInvoiceFiles": [
                    {
                        "afiid": "AFID-001",
                        "fid": "FID-001",
                        "fileName": "origin.pdf",
                        "aiid": "AIID-001",
                        "createTime": "2026-06-16 10:00:00",
                    }
                ],
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "fid": "FID-001",
                        "auditInvoiceFile": {
                            "afiid": "AFID-001",
                            "fid": "FID-001",
                            "fileName": "origin.pdf",
                            "aiid": "AIID-001",
                            "createTime": "2026-06-16 10:00:00",
                        },
                    },
                    "preparedInput": {
                        "invoiceType": "26",
                        "invoiceNo": "INV-001",
                        "goodsName": "*电信服务*通信服务费",
                        "invoiceDate": "2026-06-16",
                        "buyerTaxNo": "913500007549617646",
                        "buyerName": "锐捷网络股份有限公司",
                        "salerName": "中国联合网络通信有限公司北京市分公司",
                        "salerTaxNo": "911100008016572721",
                        "salerAddressPhone": "北京市西城区复兴门南大街6号 10010",
                        "salerAccount": "工行 123456",
                        "totalAmount": "476.1",
                        "totalTaxAmount": "0",
                        "remark": "业务号码:15652828661",
                        "items": [
                            {
                                "goodsName": "*电信服务*通信服务费",
                                "detailAmount": "476.1",
                                "taxAmount": "0",
                                "taxRate": "0",
                                "unit": "",
                                "num": "1",
                                "unitPrice": "476.1",
                                "specModel": "",
                            }
                        ],
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "currentInvoiceInfo": {
                                "aiiid": "AIIID-001",
                                "miInstanceCode": "REC-WRITEBACK-001",
                                "miApplyUserId": "u-apply-001",
                                "miApplyUserName": "王丽",
                                "createTime": "2026-06-16 10:00:00",
                            },
                            "currentAuditInvoiceFile": {
                                "afiid": "AFID-001",
                                "fid": "FID-001",
                                "fileName": "origin.pdf",
                                "aiid": "AIID-001",
                                "createTime": "2026-06-16 10:00:00",
                            },
                            "ocrEnvelope": {
                                "upload": {
                                    "fileDownUrl": "https://kingdee.example/file/FID-001.pdf"
                                },
                                "recognition": {
                                    "rawPayload": {
                                        "status": True,
                                        "data": {
                                            "invoiceNo": "INV-001"
                                        },
                                    }
                                },
                                "status": {
                                    "code": "200",
                                    "message": "success",
                                    "finishedAt": "2026-06-16T12:00:00+00:00",
                                },
                            },
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-WRITEBACK-001",
            "serviceData": prepared_receipt["serviceData"],
            "receiptContext": {"receiptCode": "REC-WRITEBACK-001"},
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "checkStatus": "warning",
                        "message": "金额需人工复核",
                    },
                    "decisionStatus": "warning",
                    "executionStatus": "SUCCEEDED",
                    "errorMessage": None,
                    "startedAt": "2026-06-16T12:00:00+00:00",
                    "finishedAt": "2026-06-16T12:00:01+00:00",
                }
            ],
            "summary": {
                "overallStatus": "SUCCESS",
            },
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        self.assertEqual(payload["instanceCode"], "REC-WRITEBACK-001")
        self.assertEqual(payload["auditInvoiceFiles"][0]["fid"], "FID-001")
        self.assertEqual(
            payload["auditRelationFiles"][0]["manufacturerFileDownloadUrl"],
            "https://kingdee.example/file/FID-001.pdf",
        )
        self.assertEqual(payload["auditTruthCheckLogs"][0]["status"], "200")
        self.assertEqual(
            json.loads(payload["auditTruthCheckLogs"][0]["json"])["data"]["invoiceNo"],
            "INV-001",
        )
        self.assertTrue(payload["auditTruthCheckLogs"][0]["atclid"])
        self.assertEqual(payload["auditTruthCheckResultBills"], [])
        self.assertEqual(payload["auditTruthCheckResultItems"], [])
        self.assertEqual(payload["auditTruthCheckResultItemCols"], [])
        self.assertEqual(payload["auditInvoiceInfos"][0]["aiiid"], "AIIID-001")
        self.assertEqual(payload["auditInvoiceInfos"][0]["aiid"], "AIID-001")
        self.assertEqual(payload["auditInvoiceInfoContents"][0]["content"], "*电信服务*通信服务费")
        self.assertEqual(payload["auditLogs"][0]["distinguishResult"], "warning")
        self.assertEqual(payload["auditLogs"][0]["specificProblemDes"], payload["auditLogs"][0]["message"])

    def test_assemble_result_audit_info_expands_runtime_rule_results_into_audit_logs(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-RULE-001",
            "serviceData": {
                "auditInfo": {
                    "instanceCode": "REC-RULE-001",
                }
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "afiid": "AFID-001",
                            "fid": "FID-001",
                            "aiid": "AIID-001",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "currentInvoiceInfo": {
                                "aiiid": "AIIID-001",
                            },
                            "currentAuditInvoiceFile": {
                                "afiid": "AFID-001",
                                "fid": "FID-001",
                                "aiid": "AIID-001",
                            },
                            "ocrEnvelope": {
                                "recognition": {"rawPayload": {"data": {"invoiceNo": "INV-001"}}},
                                "status": {"code": "200", "message": "success", "finishedAt": "2026-06-16T12:00:00+00:00"},
                                "upload": {"fileDownUrl": "https://files.example/FID-001.pdf"},
                            },
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-RULE-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "amount_result": {
                            "audit_content": "检查使用发票合计金额是否充足",
                            "audit_type": "general-rules",
                            "distinguish_content": "金额不足",
                            "distinguish_result": "REJECT",
                            "instance_code": "REC-RULE-001",
                            "invoice_file_id": "AFID-001",
                            "invoice_info_id": "AIIID-001",
                            "message": "金额不够",
                            "reason_code": "E31",
                            "policiesIndex": "",
                            "employeeSuggestionTips": "E31建议",
                        },
                        "header_result": {
                            "audit_content": "检查使用的发票购买方抬头与公司信息是否一致",
                            "audit_type": "general-rules",
                            "distinguish_content": "抬头一致",
                            "distinguish_result": "PASS",
                            "instance_code": "REC-RULE-001",
                            "invoice_file_id": "AFID-001",
                            "invoice_info_id": "AIIID-001",
                            "message": "通过",
                            "reason_code": "E01",
                            "policiesIndex": "",
                            "employeeSuggestionTips": "",
                        },
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
            "applyAmount": 100.0,
            "validInvoiceTotal": 40.0,
            "isAmountSufficient": False,
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        self.assertEqual(len(payload["auditLogs"]), 2)
        self.assertEqual(payload["auditLogs"][0]["reasonCode"], "E31")
        self.assertEqual(payload["auditLogs"][0]["distinguishResult"], "reject")
        # E31 message is overridden with receipt-level real totals
        self.assertEqual(
            payload["auditLogs"][0]["message"],
            "本次报销金额为 100 元，当前可用发票金额为 40 元， 待补充60 元。可用发票金额不足，暂不能提交。",
        )
        self.assertEqual(payload["auditLogs"][0]["policiesIndex"], "")
        self.assertEqual(payload["auditLogs"][0]["specificProblemDes"], payload["auditLogs"][0]["message"])
        self.assertEqual(
            payload["auditLogs"][0]["employeeSuggestionTips"],
            "请补充上传金额足够的有效发票。若下方已有发票被标记为异常，请先按提示修正这些发票；"
            "修正通过后，系统会重新计算可用发票金额。",
        )
        self.assertEqual(payload["auditLogs"][0]["problemTags"], "金额不足")
        self.assertEqual(payload["auditLogs"][0]["suggestionTags"], "【补充发票】【调减金额】")
        self.assertEqual(payload["auditLogs"][1]["reasonCode"], "E01")
        self.assertEqual(payload["auditLogs"][1]["distinguishResult"], "pass")
        self.assertEqual(payload["auditLogs"][1]["specificProblemDes"], payload["auditLogs"][1]["message"])
        self.assertEqual(payload["auditLogs"][1]["policiesIndex"], "")
        self.assertEqual(payload["auditLogs"][1]["employeeSuggestionTips"], "")
        self.assertEqual(payload["auditInvoiceInfos"][0]["reasonCode"], "E31")

    def test_reason_code_falls_back_to_decision_output_when_primary_rule_result_lacks_reason_code(self) -> None:
        """回归测试：primary_rule_result 存在但 reason_code 为空时，
        reasonCode 应 fallback 到 decision_output 顶层的 reasonCode，
        而非落为 None（避免回写 payload reasonCode=null 导致服务端 SQL 异常）。
        """
        prepared_receipt = {
            "receiptCode": "REC-REASONCODE-FALLBACK-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-REASONCODE-FALLBACK-001"},
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "serviceData": {
                            "currentInvoiceInfo": {"aiiid": "AIIID-001"},
                            "currentAuditInvoiceFile": {"fid": "FID-001", "aiid": "AIID-001"},
                        },
                    },
                },
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-REASONCODE-FALLBACK-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        # 顶层 reasonCode（执行图整体结论）
                        "reasonCode": "E34",
                        "checkStatus": "failed",
                        # 规则结果：distinguish_result=REJECT 但没有 reason_code 字段
                        "phone_result": {
                            "audit_content": "电话校验",
                            "distinguish_result": "REJECT",
                            "message": "电话不匹配",
                        },
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
            "isAmountSufficient": True,
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        # primary_rule_result 会选中 phone_result（REJECT），但其 reason_code 为空，
        # 应 fallback 到 decision_output["reasonCode"] = "E34"，而非 None
        self.assertEqual(payload["auditInvoiceInfos"][0]["reasonCode"], "E34")

    def test_assemble_result_audit_info_uses_real_aifid_field_for_invoice_file_id_fallback(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-AIFID-001",
            "serviceData": {
                "auditInfo": {
                    "instanceCode": "REC-AIFID-001",
                }
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "aifid": "AIFID-001",
                            "fid": "FID-001",
                            "aiid": "AIID-001",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "currentInvoiceInfo": {},
                            "currentAuditInvoiceFile": {
                                "aifid": "AIFID-001",
                                "fid": "FID-001",
                                "aiid": "AIID-001",
                            },
                            "ocrEnvelope": {
                                "recognition": {"rawPayload": {"data": {"invoiceNo": "INV-001"}}},
                                "status": {"code": "200", "message": "success", "finishedAt": "2026-06-16T12:00:00+00:00"},
                                "upload": {"fileDownUrl": "https://files.example/FID-001.pdf"},
                            },
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-AIFID-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "checkStatus": "warning",
                        "message": "金额需人工复核",
                    },
                    "decisionStatus": "warning",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        self.assertEqual(payload["auditLogs"][0]["invoiceFileId"], "AIFID-001")

    def test_assemble_result_audit_info_uses_generated_invoice_info_id_from_prepared_input(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-INVOICE-INFO-ID-001",
            "serviceData": {
                "auditInfo": {
                    "instanceCode": "REC-INVOICE-INFO-ID-001",
                }
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "aifid": "AIFID-001",
                            "fid": "FID-001",
                            "aiid": "AIID-001",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "instance_code": "REC-INVOICE-INFO-ID-001",
                        "invoice_file_id": "AIFID-001",
                        "invoice_info_id": "AIIID-GENERATED-001",
                        "serviceData": {
                            "invoiceUsageHistory": [
                                {
                                    "aiiid": "AIIID-HISTORY-001",
                                    "miInstanceCode": "REC-HISTORY-001",
                                }
                            ],
                            "currentInvoiceInfo": {
                                "aiiid": "AIIID-GENERATED-001",
                            },
                            "currentAuditInvoiceFile": {
                                "aifid": "AIFID-001",
                                "fid": "FID-001",
                                "aiid": "AIID-001",
                            },
                            "ocrEnvelope": {
                                "recognition": {"rawPayload": {"data": {"invoiceNo": "INV-001"}}},
                                "status": {"code": "200", "message": "success", "finishedAt": "2026-06-16T12:00:00+00:00"},
                                "upload": {"fileDownUrl": "https://files.example/FID-001.pdf"},
                            },
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-INVOICE-INFO-ID-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "amount_result": {
                            "audit_content": "检查使用发票合计金额是否充足",
                            "audit_type": "general-rules",
                            "distinguish_content": "test",
                            "distinguish_result": "REJECT",
                            "instance_code": "REC-INVOICE-INFO-ID-001",
                            "invoice_file_id": "AIFID-001",
                            "invoice_info_id": "AIIID-GENERATED-001",
                            "message": "test",
                            "reason_code": "E31",
                        }
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        self.assertEqual(payload["auditLogs"][0]["instanceCode"], "REC-INVOICE-INFO-ID-001")
        self.assertEqual(payload["auditLogs"][0]["invoiceFileId"], "AIFID-001")
        self.assertEqual(payload["auditLogs"][0]["invoiceInfoId"], "AIIID-GENERATED-001")
        self.assertEqual(payload["auditInvoiceInfos"][0]["aiiid"], "AIIID-GENERATED-001")

    def test_assemble_result_audit_info_builds_truthcheck_detail_tables_from_top_level_raw_payload_keys(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-TRUTHCHECK-001",
            "serviceData": {
                "auditInfo": {
                    "instanceCode": "REC-TRUTHCHECK-001",
                    "verifiUserId": "u-001",
                    "verifiUserName": "测试用户",
                }
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "aifid": "AIFID-001",
                            "fid": "FID-001",
                            "fileName": "origin.pdf",
                            "aiid": "AIID-001",
                            "createTime": "2026-06-17 10:00:00",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "goodsName": "*电信服务*通信服务费、*电信服务*违约金",
                        "items": [
                            {
                                "goodsName": "*电信服务*通信服务费",
                                "detailAmount": "476.1",
                                "taxAmount": "0",
                                "taxRate": "0",
                                "unit": "",
                                "num": "1",
                                "unitPrice": "476.1",
                                "specModel": "",
                            },
                            {
                                "goodsName": "*电信服务*违约金",
                                "detailAmount": "1.2",
                            }
                        ],
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "truthCheckFieldMappings": {
                                "bill": [
                                    {
                                        "fieldName": "invoiceNo",
                                        "fieldLable": "发票号码",
                                        "belongTable": "bill",
                                        "status": True,
                                    },
                                    {
                                        "fieldName": "invoiceNo",
                                        "fieldLable": "发票号码-重复",
                                        "belongTable": "bill",
                                        "status": True,
                                    },
                                    {
                                        "fieldName": "missingBill",
                                        "fieldLable": "不存在字段",
                                        "belongTable": "bill",
                                        "status": True,
                                    },
                                ],
                                "item": [
                                    {
                                        "fieldName": "totalAmount",
                                        "fieldLable": "价税合计",
                                        "belongTable": "item",
                                        "status": True,
                                    },
                                    {
                                        "fieldName": "totalAmount",
                                        "fieldLable": "价税合计-重复",
                                        "belongTable": "item",
                                        "status": True,
                                    },
                                    {
                                        "fieldName": "detailAmount",
                                        "fieldLable": "明细金额",
                                        "belongTable": "item",
                                        "status": True,
                                    },
                                ],
                            },
                            "currentInvoiceInfo": {
                                "aiiid": "AIIID-001",
                                "atcrid": "ATCRID-001",
                                "miInstanceCode": "REC-TRUTHCHECK-001",
                                "miApplyUserId": "u-001",
                                "miApplyUserName": "测试用户",
                                "createTime": "2026-06-17 10:00:00",
                            },
                            "currentAuditInvoiceFile": {
                                "aifid": "AIFID-001",
                                "fid": "FID-001",
                                "fileName": "origin.pdf",
                                "aiid": "AIID-001",
                                "createTime": "2026-06-17 10:00:00",
                            },
                            "ocrEnvelope": {
                                "upload": {
                                    "fileDownUrl": "https://files.example/FID-001.pdf"
                                },
                                "recognition": {
                                    "rawPayload": {
                                        "status": True,
                                        "data": [
                                            {
                                                "invoiceNo": "INV-001",
                                                "totalAmount": 476.1,
                                                "items": [
                                                    {
                                                        "detailAmount": "888.8",
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                },
                                "status": {
                                    "code": "200",
                                    "message": "success",
                                    "finishedAt": "2026-06-17T12:00:00+00:00",
                                },
                            },
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-TRUTHCHECK-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "checkStatus": "pass",
                        "message": "success",
                    },
                    "decisionStatus": "pass",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        self.assertEqual(payload["auditInvoiceInfos"][0]["atcrid"], "ATCRID-001")
        self.assertEqual(payload["auditInvoiceInfoContents"][0]["atcrId"], "ATCRID-001")
        self.assertEqual(len(payload["auditTruthCheckResultBills"]), 2)
        self.assertEqual(
            [item["name"] for item in payload["auditTruthCheckResultBills"]],
            ["发票号码", "发票号码-重复"],
        )
        self.assertEqual(
            [item["code"] for item in payload["auditTruthCheckResultBills"]],
            ["invoiceNo", "invoiceNo"],
        )
        self.assertEqual(
            [item["value"] for item in payload["auditTruthCheckResultBills"]],
            ["INV-001", "INV-001"],
        )
        self.assertEqual(
            len({item["atcrbid"] for item in payload["auditTruthCheckResultBills"]}),
            2,
        )
        self.assertEqual(len(payload["auditTruthCheckResultItems"]), 2)
        self.assertEqual(
            [item["name"] for item in payload["auditTruthCheckResultItems"]],
            ["totalAmount", "totalAmount"],
        )
        self.assertEqual(
            [item["label"] for item in payload["auditTruthCheckResultItems"]],
            ["价税合计", "价税合计-重复"],
        )
        self.assertEqual(
            [item["code"] for item in payload["auditTruthCheckResultItems"]],
            [None, None],
        )
        self.assertEqual(
            [item["value"] for item in payload["auditTruthCheckResultItems"]],
            ["476.1", "476.1"],
        )
        self.assertEqual(
            len({item["atcriid"] for item in payload["auditTruthCheckResultItems"]}),
            2,
        )
        self.assertEqual(len(payload["auditTruthCheckResultItemCols"]), 2)
        self.assertEqual(
            [item["name"] for item in payload["auditTruthCheckResultItemCols"]],
            ["totalAmount", "totalAmount"],
        )
        self.assertEqual(
            [item["label"] for item in payload["auditTruthCheckResultItemCols"]],
            ["价税合计", "价税合计-重复"],
        )
        self.assertTrue(all("code" not in item for item in payload["auditTruthCheckResultItemCols"]))
        self.assertEqual(
            len({item["atcricid"] for item in payload["auditTruthCheckResultItemCols"]}),
            2,
        )

    def test_assemble_result_audit_info_skips_disabled_string_status_field_mappings(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-TRUTHCHECK-STATUS-001",
            "serviceData": {
                "auditInfo": {
                    "instanceCode": "REC-TRUTHCHECK-STATUS-001",
                }
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "aifid": "AIFID-001",
                            "fid": "FID-001",
                            "fileName": "origin.pdf",
                            "aiid": "AIID-001",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "truthCheckFieldMappings": {
                                "bill": [
                                    {
                                        "fieldName": "invoiceNo",
                                        "fieldLable": "发票号码-禁用",
                                        "belongTable": "bill",
                                        "status": "false",
                                    },
                                    {
                                        "fieldName": "invoiceNo",
                                        "fieldLable": "发票号码-启用",
                                        "belongTable": "bill",
                                        "status": "true",
                                    },
                                ],
                                "item": [],
                            },
                            "currentInvoiceInfo": {
                                "aiiid": "AIIID-001",
                                "atcrid": "ATCRID-001",
                            },
                            "currentAuditInvoiceFile": {
                                "aifid": "AIFID-001",
                                "fid": "FID-001",
                                "fileName": "origin.pdf",
                                "aiid": "AIID-001",
                            },
                            "ocrEnvelope": {
                                "recognition": {
                                    "rawPayload": {
                                        "data": {
                                            "invoiceNo": "INV-001"
                                        }
                                    }
                                },
                                "status": {
                                    "code": "200",
                                    "message": "success",
                                    "finishedAt": "2026-06-17T12:00:00+00:00",
                                },
                            },
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-TRUTHCHECK-STATUS-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {"checkStatus": "pass"},
                    "decisionStatus": "pass",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        self.assertEqual(len(payload["auditTruthCheckResultBills"]), 1)
        self.assertEqual(payload["auditTruthCheckResultBills"][0]["name"], "发票号码-启用")

    def test_assemble_result_audit_info_with_multiple_invoices_strips_e31_on_non_last_and_overrides_last_pass(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-MULTI-001",
            "serviceData": {
                "auditInfo": {
                    "instanceCode": "REC-MULTI-001",
                }
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "afiid": "AFID-001",
                            "fid": "FID-001",
                            "aiid": "AIID-001",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "currentInvoiceInfo": {
                                "aiiid": "AIIID-001",
                            },
                            "currentAuditInvoiceFile": {
                                "afiid": "AFID-001",
                                "fid": "FID-001",
                                "aiid": "AIID-001",
                            },
                            "ocrEnvelope": {
                                "status": {"code": "200", "finishedAt": "2026-06-16T12:00:00+00:00"},
                            },
                        },
                    },
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "afiid": "AFID-002",
                            "fid": "FID-002",
                            "aiid": "AIID-002",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-002",
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "currentInvoiceInfo": {
                                "aiiid": "AIIID-002",
                            },
                            "currentAuditInvoiceFile": {
                                "afiid": "AFID-002",
                                "fid": "FID-002",
                                "aiid": "AIID-002",
                            },
                            "ocrEnvelope": {
                                "status": {"code": "200", "finishedAt": "2026-06-16T12:00:00+00:00"},
                            },
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-MULTI-001",
            "serviceData": prepared_receipt["serviceData"],
            "isAmountSufficient": True,
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "amount_result": {
                            "reason_code": "E31",
                            "distinguish_result": "REJECT",
                            "audit_content": "检查使用发票合计金额是否充足",
                            "message": "金额不够",
                        },
                        "header_result": {
                            "reason_code": "E01",
                            "distinguish_result": "PASS",
                            "audit_content": "检查使用的发票购买方抬头与公司信息是否一致",
                            "message": "抬头一致",
                        }
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                },
                {
                    "invoiceKey": "FID-002",
                    "preparedInput": prepared_receipt["invoicePreparations"][1]["preparedInput"],
                    "decisionOutput": {
                        "amount_result": {
                            "reason_code": "E31",
                            "distinguish_result": "REJECT",
                            "audit_content": "检查使用发票合计金额是否充足",
                            "message": "金额不够",
                        },
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        # 1. auditLogs validation
        # FID-001's E31 is stripped, only E01 remains
        # FID-002's E31 is overridden to PASS
        self.assertEqual(len(payload["auditLogs"]), 2)

        # Log 0: FID-001's E01 (E31 was stripped)
        self.assertEqual(payload["auditLogs"][0]["invoiceFileId"], "AFID-001")
        self.assertEqual(payload["auditLogs"][0]["reasonCode"], "E01")
        self.assertEqual(payload["auditLogs"][0]["distinguishResult"], "pass")

        # Log 1: FID-002's overridden E31 (it was REJECT in processed_receipt but isAmountSufficient is True)
        self.assertEqual(payload["auditLogs"][1]["invoiceFileId"], "AFID-002")
        self.assertEqual(payload["auditLogs"][1]["reasonCode"], "E31")
        self.assertEqual(payload["auditLogs"][1]["distinguishResult"], "pass")
        self.assertEqual(payload["auditLogs"][1]["message"], "发票合计金额充足")
        # PASS override clears policiesIndex/employeeSuggestionTips
        self.assertEqual(payload["auditLogs"][1]["policiesIndex"], "")
        self.assertEqual(payload["auditLogs"][1]["employeeSuggestionTips"], "")

        # 2. auditInvoiceInfos validation
        self.assertEqual(len(payload["auditInvoiceInfos"]), 2)
        # For FID-001, since E31 was stripped, the primary rule result should be E01 (pass)
        self.assertEqual(payload["auditInvoiceInfos"][0]["fid"], "FID-001")
        self.assertEqual(payload["auditInvoiceInfos"][0]["reasonCode"], "E01")
        # For FID-002, E31 is overridden to PASS, so its reasonCode is E31
        self.assertEqual(payload["auditInvoiceInfos"][1]["fid"], "FID-002")
        self.assertEqual(payload["auditInvoiceInfos"][1]["reasonCode"], "E31")

    def test_assemble_result_audit_info_with_multiple_invoices_strips_e31_on_non_last_and_overrides_last_reject(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-MULTI-002",
            "serviceData": {
                "auditInfo": {
                    "instanceCode": "REC-MULTI-002",
                }
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "afiid": "AFID-001",
                            "fid": "FID-001",
                            "aiid": "AIID-001",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-001",
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "currentInvoiceInfo": {
                                "aiiid": "AIIID-001",
                            },
                            "currentAuditInvoiceFile": {
                                "afiid": "AFID-001",
                                "fid": "FID-001",
                                "aiid": "AIID-001",
                            },
                            "ocrEnvelope": {
                                "status": {"code": "200", "finishedAt": "2026-06-16T12:00:00+00:00"},
                            },
                        },
                    },
                },
                {
                    "invoiceKey": "FID-002",
                    "invoiceFile": {
                        "auditInvoiceFile": {
                            "afiid": "AFID-002",
                            "fid": "FID-002",
                            "aiid": "AIID-002",
                        }
                    },
                    "preparedInput": {
                        "invoiceNo": "INV-002",
                        "serviceData": {
                            "invoiceUsageHistory": [],
                            "currentInvoiceInfo": {
                                "aiiid": "AIIID-002",
                            },
                            "currentAuditInvoiceFile": {
                                "afiid": "AFID-002",
                                "fid": "FID-002",
                                "aiid": "AIID-002",
                            },
                            "ocrEnvelope": {
                                "status": {"code": "200", "finishedAt": "2026-06-16T12:00:00+00:00"},
                            },
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-MULTI-002",
            "serviceData": prepared_receipt["serviceData"],
            "isAmountSufficient": False,
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "amount_result": {
                            "reason_code": "E31",
                            "distinguish_result": "PASS",
                            "audit_content": "检查使用发票合计金额是否充足",
                            "message": "发票合计金额充足",
                        },
                    },
                    "decisionStatus": "pass",
                    "executionStatus": "SUCCEEDED",
                },
                {
                    "invoiceKey": "FID-002",
                    "preparedInput": prepared_receipt["invoicePreparations"][1]["preparedInput"],
                    "decisionOutput": {
                        "amount_result": {
                            "reason_code": "E31",
                            "distinguish_result": "PASS",
                            "audit_content": "检查使用发票合计金额是否充足",
                            "message": "发票合计金额充足",
                        },
                    },
                    "decisionStatus": "pass",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        # 1. auditLogs validation
        # FID-001's E31 is stripped, list becomes length 1
        # FID-002's E31 is overridden to REJECT
        self.assertEqual(len(payload["auditLogs"]), 1)

        # Log 0: FID-002's overridden E31 (it was PASS in processed_receipt but isAmountSufficient is False)
        self.assertEqual(payload["auditLogs"][0]["invoiceFileId"], "AFID-002")
        self.assertEqual(payload["auditLogs"][0]["reasonCode"], "E31")
        self.assertEqual(payload["auditLogs"][0]["distinguishResult"], "reject")
        # No applyAmount/validInvoiceTotal in processed_receipt -> new-template fallback message
        self.assertEqual(payload["auditLogs"][0]["message"], "可用发票金额不足，暂不能提交。")
        # REJECT override carries the latest CSV suggestion and categories
        self.assertEqual(payload["auditLogs"][0]["policiesIndex"], "")
        self.assertEqual(
            payload["auditLogs"][0]["employeeSuggestionTips"],
            "请补充上传金额足够的有效发票。若下方已有发票被标记为异常，请先按提示修正这些发票；"
            "修正通过后，系统会重新计算可用发票金额。",
        )
        self.assertEqual(payload["auditLogs"][0]["problemTags"], "金额不足")
        self.assertEqual(payload["auditLogs"][0]["suggestionTags"], "【补充发票】【调减金额】")

        # 2. auditInvoiceInfos validation
        self.assertEqual(len(payload["auditInvoiceInfos"]), 2)
        # For FID-001, since E31 was stripped, reasonCode is None
        self.assertEqual(payload["auditInvoiceInfos"][0]["fid"], "FID-001")
        self.assertIsNone(payload["auditInvoiceInfos"][0]["reasonCode"])
        # For FID-002, E31 is overridden to REJECT, so its reasonCode is E31
        self.assertEqual(payload["auditInvoiceInfos"][1]["fid"], "FID-002")
        self.assertEqual(payload["auditInvoiceInfos"][1]["reasonCode"], "E31")

    def test_e31_message_builder_fills_real_totals(self) -> None:
        # 有真实金额时按 CSV 模板填充
        self.assertEqual(
            _build_e31_message(100.0, 40.0),
            "本次报销金额为 100 元，当前可用发票金额为 40 元， 待补充60 元。可用发票金额不足，暂不能提交。",
        )
        # shortage 不会出现负数
        self.assertIn("待补充0 元", _build_e31_message(10.0, 187.15))
        # 金额缺失时仍返回新版的不可提交提示
        self.assertEqual(_build_e31_message(None, None), "可用发票金额不足，暂不能提交。")
        self.assertEqual(_build_e31_message(100.0, None), "可用发票金额不足，暂不能提交。")
        # 金额格式化去掉无意义尾零
        self.assertEqual(_format_amount(10.0), "10")
        self.assertEqual(_format_amount(10.5), "10.5")
        self.assertEqual(_format_amount(382.2), "382.2")

    def test_personal_transport_e31_uses_receipt_totals_and_maps_tags(self) -> None:
        """交通费 E31 应使用整单金额，并把图字段映射到 auditLogs 标签。"""
        prepared_receipt = {
            "receiptCode": "REC-TRANSPORT-E31-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-TRANSPORT-E31-001"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "serviceData": {
                            "currentInvoiceInfo": {"aiiid": "AIIID-001"},
                            "currentAuditInvoiceFile": {"afiid": "AFID-001", "fid": "FID-001"},
                        }
                    },
                }
            ],
        }
        graph_message = (
            "本次交通费报销金额为 {报销金额} 元，当前有效发票金额为 "
            "{可用发票金额} 元，待补充 {缺少金额} 元。可用发票金额不足，暂不能提交。"
        )
        traffic_message = (
            "本次交通费报销金额为 100 元，当前有效发票金额为 40 元，待补充 60 元。"
            "可用发票金额不足，暂不能提交。"
        )
        processed_receipt = {
            "receiptCode": "REC-TRANSPORT-E31-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "amount_result": {
                            "reason_code": "E31",
                            "distinguish_result": "REJECT",
                            "audit_content": "检查使用发票合计金额是否充足",
                            "message": graph_message,
                            "policiesIndex": "",
                            "employeeSuggestionTips": "交通费 CSV 建议",
                            "problem_category": "金额不足",
                            "optimization_action_category": "【补充发票】【调减金额】",
                        }
                    },
                    "decisionStatus": "reject",
                }
            ],
            "applyAmount": 100.0,
            "validInvoiceTotal": 40.0,
            "isAmountSufficient": False,
        }

        payload = assemble_result_audit_info(
            prepared_receipt,
            processed_receipt,
            expense_profile="personal_transport",
        )
        log = payload["auditLogs"][0]
        self.assertEqual(log["message"], traffic_message)
        self.assertEqual(log["employeeSuggestionTips"], "交通费 CSV 建议")
        self.assertEqual(log["problemTags"], "金额不足")
        self.assertEqual(log["suggestionTags"], "【补充发票】【调减金额】")

    def test_assemble_e31_message_uses_receipt_real_totals(self) -> None:
        """端到端：processed_receipt 带真实 applyAmount/validInvoiceTotal 时，E31 message
        按 CSV 模板填入真实金额（476 有效 / 500 报销 / 缺 24）。

        回归用例：图里 E34 的 invoice_finalAmount 嵌套在
        decisionOutput['invoice_content_valid_result'] 下；此前 application 层读不到 →
        validInvoiceTotal 算成 0 → message 报「有效发票合计金额 0 元」。
        """
        prepared_receipt = {
            "receiptCode": "REC-E31-REAL-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-E31-REAL-001"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "instance_code": "REC-E31-REAL-001",
                        "serviceData": {
                            "currentInvoiceInfo": {"aiiid": "AIIID-001"},
                            "currentAuditInvoiceFile": {"afiid": "AFID-001", "fid": "FID-001"},
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-E31-REAL-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "amount_result": {
                            "reason_code": "E31",
                            "distinguish_result": "REJECT",
                            "audit_content": "检查使用发票合计金额是否充足",
                            "audit_type": "verification-form",
                            "invoice_file_id": "AFID-001",
                            "invoice_info_id": "AIIID-001",
                            "message": "发票合计金额不足",
                            "policiesIndex": "",
                            "employeeSuggestionTips": "",
                        },
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
            "applyAmount": 500.0,
            "validInvoiceTotal": 476.0,
            "remainingApplyAmount": 24.0,
            "isAmountSufficient": False,
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)
        log = payload["auditLogs"][0]
        self.assertEqual(log["reasonCode"], "E31")
        self.assertEqual(log["distinguishResult"], "reject")
        self.assertEqual(
            log["message"],
            "本次报销金额为 500 元，当前可用发票金额为 476 元， 待补充24 元。可用发票金额不足，暂不能提交。",
        )
        self.assertEqual(log["employeeSuggestionTips"],
                         "请补充上传金额足够的有效发票。若下方已有发票被标记为异常，请先按提示修正这些发票；"
                         "修正通过后，系统会重新计算可用发票金额。")
        self.assertEqual(log["problemTags"], "金额不足")
        self.assertEqual(log["suggestionTags"], "【补充发票】【调减金额】")

    def test_assemble_propagates_graph_regulation_and_suggestion(self) -> None:
        # 图节点产出的 regulation/suggestion 应原样透传到 auditLogs（非 E31 节点）
        prepared_receipt = {
            "receiptCode": "REC-REG-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-REG-001"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "instance_code": "REC-REG-001",
                        "invoiceNo": "INV-001",
                        "serviceData": {
                            "currentInvoiceInfo": {"aiiid": "AIIID-001"},
                            "currentAuditInvoiceFile": {"afiid": "AFID-001", "fid": "FID-001"},
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-REG-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "company_backlist_result": {
                            "reason_code": "E09",
                            "distinguish_result": "REJECT",
                            "audit_content": "检查销货方黑名单",
                            "audit_type": "general-rules",
                            "invoice_file_id": "AFID-001",
                            "invoice_info_id": "AIIID-001",
                            "message": "票据发票销货方在黑名单中",
                            "policiesIndex": "《锐捷网络员工费用管理与报销制度》\n5.2票据使用规范",
                            "employeeSuggestionTips": "【发票作废】联系销货方作废本发票",
                            "problem_category": "虚开发票",
                            "optimization_action_category": "【重新开票】",
                        },
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }
        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)
        log = payload["auditLogs"][0]
        self.assertEqual(log["reasonCode"], "E09")
        self.assertEqual(log["policiesIndex"], "《锐捷网络员工费用管理与报销制度》\n5.2票据使用规范")
        self.assertEqual(log["employeeSuggestionTips"], "【发票作废】联系销货方作废本发票")
        self.assertEqual(log["problemTags"], "虚开发票")
        self.assertEqual(log["suggestionTags"], "【重新开票】")

    def test_assemble_propagates_create_time_from_rule_result(self) -> None:
        """图内各稽核点输出的 create_time（取自 context.executionTime）应原样透传到
        auditLogs 的 createTime；缺失时为 None（回写层不另行生成时间戳）。
        """
        prepared_receipt = {
            "receiptCode": "REC-CT-PROP-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-CT-PROP-001"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "instance_code": "REC-CT-PROP-001",
                        "serviceData": {
                            "currentInvoiceInfo": {"aiiid": "AIIID-001"},
                            "currentAuditInvoiceFile": {"afiid": "AFID-001", "fid": "FID-001"},
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-CT-PROP-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "header_result": {
                            "reason_code": "E01",
                            "distinguish_result": "REJECT",
                            "audit_content": "抬头检查",
                            "audit_type": "general-rules",
                            "invoice_file_id": "AFID-001",
                            "invoice_info_id": "AIIID-001",
                            "message": "抬头不一致",
                            "create_time": "2026-07-08T21:35:00+08:00",
                        }
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }
        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)
        self.assertEqual(payload["auditLogs"][0]["createTime"], "2026-07-08T21:35:00+08:00")

        # 缺失 create_time 时 createTime 为 None（回写层不兜底生成时间戳）
        processed_receipt["invoiceResults"][0]["decisionOutput"]["header_result"].pop("create_time")
        payload_without = assemble_result_audit_info(prepared_receipt, processed_receipt)
        self.assertIsNone(payload_without["auditLogs"][0]["createTime"])

    def test_assemble_includes_overall_suggestion_when_present(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-OVERALL-001",
            "serviceData": {"auditInfo": {"instanceCode": "REC-OVERALL-001"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "instance_code": "REC-OVERALL-001",
                        "invoiceNo": "INV-001",
                        "serviceData": {
                            "currentInvoiceInfo": {"aiiid": "AIIID-001"},
                            "currentAuditInvoiceFile": {"afiid": "AFID-001", "fid": "FID-001"},
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-OVERALL-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "company_backlist_result": {
                            "reason_code": "E09",
                            "distinguish_result": "REJECT",
                            "audit_content": "检查销货方黑名单",
                            "audit_type": "general-rules",
                            "invoice_file_id": "AFID-001",
                            "invoice_info_id": "AIIID-001",
                            "message": "黑名单",
                            "policiesIndex": "",
                            "employeeSuggestionTips": "作废",
                        },
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
            "aiAuditAdvice": "本核销单建议统一联系销货方作废黑名单发票。",
        }
        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)
        self.assertEqual(
            payload["aiAuditAdvice"],
            "本核销单建议统一联系销货方作废黑名单发票。",
        )

    def test_assemble_omits_overall_suggestion_when_absent_or_empty(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-OVERALL-002",
            "serviceData": {"auditInfo": {"instanceCode": "REC-OVERALL-002"}},
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "instance_code": "REC-OVERALL-002",
                        "serviceData": {
                            "currentInvoiceInfo": {"aiiid": "AIIID-001"},
                            "currentAuditInvoiceFile": {"afiid": "AFID-001", "fid": "FID-001"},
                        },
                    },
                }
            ],
        }
        base_processed = {
            "receiptCode": "REC-OVERALL-002",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {
                        "company_backlist_result": {
                            "reason_code": "E09",
                            "distinguish_result": "REJECT",
                            "audit_content": "c",
                            "audit_type": "general-rules",
                            "invoice_file_id": "AFID-001",
                            "invoice_info_id": "AIIID-001",
                            "message": "m",
                        },
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }
        expected_advice = "本次审核存在REJECT稽核项，请根据稽核明细处理。"
        # absent: a deterministic warning is now required for a non-pass result.
        payload = assemble_result_audit_info(prepared_receipt, dict(base_processed))
        self.assertEqual(payload["aiAuditAdvice"], expected_advice)
        # empty string
        payload = assemble_result_audit_info(
            prepared_receipt, {**base_processed, "aiAuditAdvice": ""}
        )
        self.assertEqual(payload["aiAuditAdvice"], expected_advice)
        # whitespace
        payload = assemble_result_audit_info(
            prepared_receipt, {**base_processed, "aiAuditAdvice": "   "}
        )
        self.assertEqual(payload["aiAuditAdvice"], expected_advice)
        # None
        payload = assemble_result_audit_info(
            prepared_receipt, {**base_processed, "aiAuditAdvice": None}
        )
        self.assertEqual(payload["aiAuditAdvice"], expected_advice)

    def test_telecom_compliance_marks_telecom_service_penalty_and_surcharge_noncompliant(self) -> None:
        prepared_receipt = {
            "receiptCode": "REC-COMPLIANCE-001",
            "serviceData": {
                "auditInfo": {"instanceCode": "REC-COMPLIANCE-001"},
            },
            "invoicePreparations": [
                {
                    "invoiceKey": "FID-001",
                    "invoiceFile": {"fid": "FID-001"},
                    "preparedInput": {
                        "goodsName": "*电信服务*违约金、*电信服务*代收费、*电信服务*通信服务费、普通商品",
                        "items": [
                            {"goodsName": "*电信服务*违约金", "detailAmount": "10", "taxAmount": "0", "taxRate": "0"},
                            {"goodsName": "*电信服务*代收费", "detailAmount": "20", "taxAmount": "0", "taxRate": "0"},
                            {"goodsName": "*电信服务*通信服务费", "detailAmount": "30", "taxAmount": "0", "taxRate": "0"},
                            {"goodsName": "普通商品", "detailAmount": "40", "taxAmount": "0", "taxRate": "0"},
                            {"goodsName": "", "detailAmount": "50", "taxAmount": "0", "taxRate": "0"},
                        ],
                        "serviceData": {
                            "currentInvoiceInfo": {"aiiid": "AIIID-001", "atcrid": "ATCRID-001"},
                            "ocrEnvelope": {"status": {"finishedAt": "2026-06-16T12:00:00+00:00"}},
                        },
                    },
                }
            ],
        }
        processed_receipt = {
            "receiptCode": "REC-COMPLIANCE-001",
            "serviceData": prepared_receipt["serviceData"],
            "invoiceResults": [
                {
                    "invoiceKey": "FID-001",
                    "preparedInput": prepared_receipt["invoicePreparations"][0]["preparedInput"],
                    "decisionOutput": {"checkStatus": "pass"},
                    "decisionStatus": "pass",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        payload = assemble_result_audit_info(
            prepared_receipt, processed_receipt, compliance_rule=telecom_compliance_rule
        )
        compliance = [c["compliance"] for c in payload["auditInvoiceInfoContents"]]

        self.assertEqual(compliance, [False, False, True, True, True])


if __name__ == "__main__":
    unittest.main()