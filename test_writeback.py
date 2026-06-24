import unittest
import json

from expense_audit_orchestrator.writeback import assemble_result_audit_info


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
                        },
                    },
                    "decisionStatus": "reject",
                    "executionStatus": "SUCCEEDED",
                }
            ],
            "summary": {"overallStatus": "SUCCESS"},
        }

        payload = assemble_result_audit_info(prepared_receipt, processed_receipt)

        self.assertEqual(len(payload["auditLogs"]), 2)
        self.assertEqual(payload["auditLogs"][0]["reasonCode"], "E31")
        self.assertEqual(payload["auditLogs"][0]["distinguishResult"], "reject")
        self.assertEqual(payload["auditLogs"][1]["reasonCode"], "E01")
        self.assertEqual(payload["auditLogs"][1]["distinguishResult"], "pass")
        self.assertEqual(payload["auditInvoiceInfos"][0]["reasonCode"], "E31")

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


if __name__ == "__main__":
    unittest.main()