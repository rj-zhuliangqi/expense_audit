from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision
from expense_audit_orchestrator.profiles.personal_transport.data import (
    build_taxi_invoice_serial_enricher,
    is_taxi_invoice,
    personal_transport_invoice_type_enricher,
    normalize_invoice_serial_prefix,
)


ROOT = Path(__file__).resolve().parent
GRAPH_PATH = ROOT / "graph-latest-personal-transport-0722.json"
MESSAGE_FIELD_ID = "509fd9ba-3996-4e4a-9021-df6513ed6807"


class PersonalTransportMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.decision = load_decision(GRAPH_PATH)

    def test_non_empty_message_templates_have_no_unresolved_placeholders(self) -> None:
        dynamic_count = 0
        for node in self.graph["nodes"]:
            if node["type"] != "decisionTableNode":
                continue
            content = node["content"]
            if isinstance(content, str):
                content = json.loads(content)
            output = next(item for item in content["outputs"] if item["field"] == "message")
            for rule in content["rules"]:
                value = rule.get(output["id"])
                if not value:
                    continue
                self.assertNotRegex(value, r"\{[^{}]+\}", node["name"])
                if node["name"] not in {"发票充值卡检查", "发票内容项目检查"} or "LLM服务调用失败" not in value:
                    if value != '""':
                        self.assertIn("+", value, node["name"])
                        dynamic_count += 1
        self.assertEqual(dynamic_count, 16)

    def _base_input(self) -> dict:
        return {
            "receipt": {"code": "REC-MESSAGE-001"},
            "context": {},
            "invoiceNo": "123",
            "invoiceType": "增值税专用发票",
            "buyerName": "错误公司",
            "buyerTaxNo": "BAD-TAX",
            "salerName": "风险销方",
            "invoiceAmount": 100,
            "totalAmount": 100,
            "invoiceDate": "2025-01-01",
            "passengerName": "李四",
            "goodsName": "充值卡、运输服务",
            "items": [{"goodsName": "充值卡"}],
            "verifyResult": [{"key": "sys-001"}],
            "previousInvoiceNumbers": ["122"],
            "serviceData": {
                "expenseInvoiceTypes": [{"manufacturerBillCode": "电子普票"}],
                "companyList": [{"ccode": "C1", "companyName": "正确公司", "companyTax": "GOOD-TAX"}],
                "auditInfo": {
                    "instanceComCode": "C1",
                    "applyAmount": 150,
                    "submitTime": "2026-01-01",
                    "verifiUserName": "张三",
                },
                "companyBlacklist": [{"value": "风险销方"}],
                "invoiceUsageHistory": [{"chequeNo": "123", "miInstanceCode": "REC-OLD-001"}],
            },
        }

    def _apply_invoice_type_enricher(self, prepared: dict) -> None:
        prepared["serviceData"]["personalTransportInvoiceType"] = personal_transport_invoice_type_enricher(
            "REC-MESSAGE-001", "invoice.pdf", prepared, prepared["serviceData"]
        )

    @staticmethod
    def _rule(result: dict, code: str) -> dict:
        for value in result["decisionOutput"].values():
            if isinstance(value, dict) and value.get("reason_code") == code:
                return value
        raise AssertionError(f"rule {code!r} not found")

    def test_runtime_fills_invoice_specific_values(self) -> None:
        prepared = self._base_input()
        prepared["serviceData"]["taxiInvoiceSerial"] = {
            "invoiceNo": "123",
            "currentPrefix": None,
            "historyNumbers": ["122"],
            "historyHit": True,
            "relationDescription": "历史发票号 122",
            "batchHit": False,
            "isTaxiInvoice": True,
            "lookupFailed": False,
        }
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        expected = {
            "E35": "票据 发票号 123 的票据类型为“增值税专用发票”，不属于交通费允许票种范围。交通费仅支持数电普票、电子普票、过路过桥费发票、火车票、客运车票、出租车票、财政收据、数电铁路等票据。",
            "E01": "票据 发票号 123 的购货方信息为“错误公司”，与核销单所属公司“正确公司”不一致。",
            "E02": "票据 发票号 123 的购货方纳税人识别号为“BAD-TAX”，与所属公司正确纳税人识别号“GOOD-TAX”不一致。",
            "E31": "本次交通费报销金额为 150 元，当前有效发票金额为 100 元，待补充 50 元。可用发票金额不足，暂不能提交。",
            "E09": "票据 发票号 123 的销货方“风险销方”命中公司或税务高风险/黑名单企业。",
            "E05": "票据 发票号 123 已在核销单 REC-OLD-001 中使用，不能重复报销。",
            "E33": "票据 发票号 123 的开票日期为 2025-01-01，与本次报销提交年度 2026 不一致。",
            "sys-001": "票据 发票号 123 的发票真伪查验未通过，暂不符合真实、合法、合规票据要求。",
            "E34": "本次报销中存在出租车发票连票，发票号 123 与历史发票号 122 存在连票关系，存在异常报销风险。",
        }
        for code, message in expected.items():
            actual = self._rule(result, code)["message"]
            self.assertEqual(actual, message, code)
            self.assertNotRegex(actual, r"\{[^{}]+\}", code)

    def test_e31_uses_tax_inclusive_total_amount_and_final_amount(self) -> None:
        prepared = self._base_input()
        prepared.update(
            {
                "invoiceNo": "124",
                "invoiceAmount": 90,
                "totalAmount": 105,
                "serviceData": {
                    **prepared["serviceData"],
                    "auditInfo": {
                        **prepared["serviceData"]["auditInfo"],
                        "applyAmount": 100,
                    },
                },
            }
        )

        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        e31 = self._rule(result, "E31")
        self.assertEqual(e31["distinguish_result"], "PASS")
        self.assertEqual(result["decisionOutput"]["invoice_finalAmount"], 105)

        prepared["serviceData"]["auditInfo"]["applyAmount"] = 110
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        e31 = self._rule(result, "E31")
        self.assertEqual(e31["distinguish_result"], "REJECT")
        self.assertEqual(
            e31["message"],
            "本次交通费报销金额为 110 元，当前有效发票金额为 105 元，待补充 5 元。可用发票金额不足，暂不能提交。",
        )

    def test_empty_invoice_content_passes_e17_and_e36_without_calling_llm(self) -> None:
        prepared = self._base_input()
        prepared.update({
            "invoiceNo": "789",
            "invoiceType": "火车票",
            "goodsName": None,
            "contents": None,
            "invoiceContents": None,
            "description": None,
            "items": None,
            "goodsItems": [],
        })

        result = evaluate_prepared_input(self.decision, prepared, trace=True)
        self.assertEqual(self._rule(result, "E17")["distinguish_result"], "PASS")
        self.assertEqual(self._rule(result, "E36")["distinguish_result"], "PASS")

        llm_nodes = [
            value for value in (result["trace"] or {}).values()
            if isinstance(value, dict) and value.get("name") == "调用llm"
        ]
        self.assertEqual(len(llm_nodes), 2)
        self.assertTrue(all(node["output"].get("skipped") is True for node in llm_nodes))

    def test_explicit_recharge_rule_rejects_without_llm(self) -> None:
        prepared = self._base_input()
        prepared.update({
            "invoiceNo": "788",
            "goodsName": "某某储值账户服务",
            "items": [{"goodsName": "某某储值账户服务", "detailAmount": "100"}],
        })

        result = evaluate_prepared_input(self.decision, prepared, trace=True)
        self.assertEqual(self._rule(result, "E17")["distinguish_result"], "REJECT")
        self.assertIn("储值账户", self._rule(result, "E17")["message"])

        e17_llm = next(
            value for value in (result["trace"] or {}).values()
            if isinstance(value, dict)
            and value.get("name") == "调用llm"
            and value.get("output", {}).get("llm_result", {}).get("passed") is False
        )
        self.assertTrue(e17_llm["output"].get("skipped"))

    def test_prompts_keep_model_generalization_and_empty_content_guard(self) -> None:
        sources = {
            node["name"]: node["content"]["source"]
            for node in self.graph["nodes"]
            if node.get("type") == "functionNode"
            and node.get("name") in {"充值卡检查prompt", "发票合规prompt", "调用llm"}
        }
        self.assertIn("同义词", sources["充值卡检查prompt"])
        self.assertIn("无法确定时返回 true", sources["充值卡检查prompt"])
        self.assertIn("严禁因为没有发票内容而拒绝", sources["发票合规prompt"])
        self.assertIn("skipLlm", sources["充值卡检查prompt"])
        self.assertIn("skipLlm", sources["发票合规prompt"])
        self.assertIn("直接返回，不调用远端 LLM", sources["调用llm"])

    def test_invoice_type_72_matches_electronic_ordinary_invoice_mapping(self) -> None:
        prepared = self._base_input()
        prepared.update(
            {
                "invoiceNo": "459",
                "invoiceType": "72",
                "goodsName": None,
                "items": [],
                "serviceData": {
                    **prepared["serviceData"],
                    "expenseInvoiceTypes": [
                        {
                            "manufacturerBillCode": "1",
                            "invoiceType": "1-003",
                            "manufacturerBillName": "电子普通发票",
                        }
                    ],
                },
            }
        )
        self._apply_invoice_type_enricher(prepared)

        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        self.assertEqual(self._rule(result, "E35")["distinguish_result"], "PASS")

    def test_invoice_type_26_passes_when_enricher_is_namespaced_like_production(self) -> None:
        prepared = self._base_input()
        prepared.update(
            {
                "invoiceNo": "461",
                "invoiceType": "26",
                "goodsName": "*交通运输服务*客运服务费",
                "items": [],
                "serviceData": {
                    **prepared["serviceData"],
                    "expenseInvoiceTypes": [
                        {
                            "manufacturerBillCode": "26",
                            "invoiceType": "RJ-001",
                            "manufacturerBillName": "数电普票",
                        }
                    ],
                },
            }
        )
        self._apply_invoice_type_enricher(prepared)

        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        self.assertEqual(self._rule(result, "E35")["distinguish_result"], "PASS")

    def test_unknown_invoice_type_is_rejected_even_with_toll_content(self) -> None:
        prepared = self._base_input()
        prepared.update(
            {
                "invoiceNo": "460",
                "invoiceType": "999",
                "tollMark": "1",
                "goodsName": "通行费",
                "items": [{"goodsName": "通行费"}],
                "serviceData": {
                    **prepared["serviceData"],
                    "expenseInvoiceTypes": [
                        {
                            "manufacturerBillCode": "17",
                            "invoiceType": "1-009",
                            "manufacturerBillName": "过路桥费发票",
                        }
                    ],
                },
            }
        )
        self._apply_invoice_type_enricher(prepared)

        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        self.assertEqual(self._rule(result, "E35")["distinguish_result"], "REJECT")

    def test_w29_and_e37_require_invoice_type_and_passenger_content_gate(self) -> None:
        cases = [
            (
                "26_same",
                {"invoiceType": "26", "passengerName": "张三", "goodsName": "*交通运输服务*客运服务费", "items": []},
                "PASS",
                "PASS",
            ),
            (
                "26_empty",
                {"invoiceType": "26", "passengerName": "", "goodsName": "*交通运输服务*客运服务费", "items": []},
                "WARNING",
                "PASS",
            ),
            (
                "26_mismatch",
                {"invoiceType": "26", "passengerName": "李四", "goodsName": "*交通运输服务*客运服务费", "items": []},
                "PASS",
                "REJECT",
            ),
            (
                "72_mismatch",
                {"invoiceType": "72", "passengerName": "李四", "goodsName": "*运输服务*运输费", "items": []},
                "PASS",
                "REJECT",
            ),
            (
                "72_non_passenger_content",
                {"invoiceType": "72", "passengerName": "李四", "goodsName": "住宿服务", "items": []},
                "PASS",
                "PASS",
            ),
            (
                "26_cargo_content",
                {"invoiceType": "26", "passengerName": "李四", "goodsName": "*运输服务*货物运输", "items": []},
                "PASS",
                "PASS",
            ),
            (
                "26_item_content",
                {"invoiceType": "26", "passengerName": "李四", "goodsName": "*交通运输服务*乘车费", "items": [{"goodsName": "*交通运输服务*乘车费"}]},
                "PASS",
                "REJECT",
            ),
        ]

        for offset, (label, update, expected_w29, expected_e37) in enumerate(cases, start=600):
            with self.subTest(label=label):
                prepared = self._base_input()
                prepared.update({"invoiceNo": str(offset), **update})
                self._apply_invoice_type_enricher(prepared)
                result = evaluate_prepared_input(self.decision, prepared, trace=False)
                self.assertEqual(self._rule(result, "W29")["distinguish_result"], expected_w29)
                self.assertEqual(self._rule(result, "E37")["distinguish_result"], expected_e37)

    def test_runtime_fills_passenger_values(self) -> None:
        prepared = self._base_input()
        prepared.update({"invoiceNo": "456", "invoiceType": "火车票"})
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        self.assertEqual(
            self._rule(result, "E15")["message"],
            "票据 发票号 456 的旅客姓名为“李四”，与核销人“张三”不一致。火车票、客运汽车票、数电铁路等实名交通票据应为本人票据。",
        )

        prepared.update({"invoiceNo": "458", "invoiceType": "29"})
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        self.assertEqual(self._rule(result, "E15")["distinguish_result"], "REJECT")
        self.assertEqual(
            self._rule(result, "E15")["message"],
            "票据 发票号 458 的旅客姓名为“李四”，与核销人“张三”不一致。火车票、客运汽车票、数电铁路等实名交通票据应为本人票据。",
        )

        prepared.update({"invoiceNo": "457", "invoiceType": "26"})
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        self.assertEqual(
            self._rule(result, "E37")["message"],
            "票据 发票号 457 的出行人“李四”与核销人“张三”不一致。旅客运输发票如填写出行人信息，应为核销人本人。",
        )

    def test_taxi_serial_normalization_and_type_detection(self) -> None:
        self.assertEqual(normalize_invoice_serial_prefix("12345601"), "123456")
        self.assertEqual(normalize_invoice_serial_prefix("0012345601"), "001234")
        self.assertEqual(normalize_invoice_serial_prefix("123456789012"), "123456")
        self.assertIsNone(normalize_invoice_serial_prefix("1234567"))
        self.assertTrue(is_taxi_invoice({"invoiceType": "8"}))
        self.assertTrue(is_taxi_invoice({"invoiceType": "电子普通发票", "goodsName": "出租车服务"}))
        self.assertFalse(is_taxi_invoice({"invoiceType": "72", "goodsName": "交通运输服务"}))

    def test_taxi_serial_enricher_uses_invoice_priority_and_skips_non_taxi_or_short_numbers(self) -> None:
        calls: list[tuple[str, str, str | None]] = []

        def provider(cheque_no: str, instance_code: str, accounting_code: str | None) -> list[str]:
            calls.append((cheque_no, instance_code, accounting_code))
            return []

        enricher = build_taxi_invoice_serial_enricher(provider=provider)
        result = enricher(
            "REC-TAXI-002",
            "invoice.pdf",
            {
                "chequeNo": "  ",
                "invoiceNo": "0012345601",
                "serialNo": "9999999901",
                "invoiceType": "8",
                "accountingCode": " ",
            },
            {"auditInfo": {"instanceCode": "REC-TAXI-002", "accountingCode": "ACCT-02"}},
        )
        self.assertEqual(result["invoiceNo"], "0012345601")
        self.assertEqual(calls, [("0012345601", "REC-TAXI-002", "ACCT-02")])

        non_taxi = enricher(
            "REC-TAXI-002",
            "invoice.pdf",
            {"invoiceNo": "12345601", "invoiceType": "72", "goodsName": "交通运输服务"},
            {"auditInfo": {"instanceCode": "REC-TAXI-002"}},
        )
        short_number = enricher(
            "REC-TAXI-002",
            "invoice.pdf",
            {"invoiceNo": "1234567", "invoiceType": "8"},
            {"auditInfo": {"instanceCode": "REC-TAXI-002"}},
        )
        self.assertFalse(non_taxi["isTaxiInvoice"])
        self.assertEqual(non_taxi["currentPrefix"], "123456")
        self.assertIsNone(short_number["currentPrefix"])
        self.assertEqual(calls, [("0012345601", "REC-TAXI-002", "ACCT-02")])

    def test_taxi_serial_enricher_calls_history_provider_and_degrades_on_failure(self) -> None:
        calls: list[tuple[str, str, str | None]] = []

        def provider(cheque_no: str, instance_code: str, accounting_code: str | None) -> list[str]:
            calls.append((cheque_no, instance_code, accounting_code))
            return ["12345699"]

        enricher = build_taxi_invoice_serial_enricher(provider=provider)
        result = enricher(
            "REC-TAXI-001",
            "invoice.pdf",
            {"invoiceType": "8", "invoiceNo": "12345601", "accountingCode": "111"},
            {"auditInfo": {"instanceCode": "REC-TAXI-001"}},
        )

        self.assertEqual(calls, [("12345601", "REC-TAXI-001", "111")])
        self.assertEqual(result["currentPrefix"], "123456")
        self.assertEqual(result["historyNumbers"], ["12345699"])
        self.assertTrue(result["historyHit"])
        self.assertTrue(result["isTaxiInvoice"])
        self.assertFalse(result["lookupFailed"])

        failed_enricher = build_taxi_invoice_serial_enricher(
            provider=lambda cheque_no, instance_code, accounting_code: (_ for _ in ()).throw(
                TimeoutError("timeout")
            )
        )
        failed = failed_enricher(
            "REC-TAXI-001",
            "invoice.pdf",
            {"invoiceType": "8", "invoiceNo": "12345601"},
            {"auditInfo": {"instanceCode": "REC-TAXI-001"}},
        )
        self.assertEqual(failed["historyNumbers"], [])
        self.assertFalse(failed["historyHit"])
        self.assertTrue(failed["lookupFailed"])

    def test_e34_uses_history_and_batch_flags_and_ignores_non_taxi(self) -> None:
        cases = [
            ({"isTaxiInvoice": True, "historyHit": True, "batchHit": False}, "REJECT"),
            ({"isTaxiInvoice": True, "historyHit": False, "batchHit": True}, "REJECT"),
            ({"isTaxiInvoice": True, "historyHit": False, "batchHit": False}, "PASS"),
            ({"isTaxiInvoice": False, "historyHit": True, "batchHit": True}, "PASS"),
        ]
        for offset, (serial_data, expected) in enumerate(cases, start=900):
            with self.subTest(serial_data=serial_data):
                prepared = self._base_input()
                prepared.update({"invoiceNo": str(offset), "invoiceType": "8"})
                prepared["serviceData"]["taxiInvoiceSerial"] = {
                    "invoiceNo": str(offset),
                    "currentPrefix": None,
                    "historyNumbers": [],
                    "lookupFailed": False,
                    **serial_data,
                }
                result = evaluate_prepared_input(self.decision, prepared, trace=False)
                self.assertEqual(self._rule(result, "E34")["distinguish_result"], expected)

    def test_e34_reject_message_contains_related_invoice_numbers_without_template_placeholder(self) -> None:
        prepared = self._base_input()
        prepared.update({"invoiceNo": "12345601", "invoiceType": "8"})
        prepared["serviceData"]["taxiInvoiceSerial"] = {
            "invoiceNo": "12345601",
            "currentPrefix": "123456",
            "historyNumbers": ["12345699"],
            "historyHit": True,
            "relationDescription": "历史发票号 12345699",
            "batchHit": False,
            "isTaxiInvoice": True,
            "lookupFailed": False,
        }

        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        rule = self._rule(result, "E34")
        self.assertEqual(rule["distinguish_result"], "REJECT")
        self.assertIn("发票号 12345601", rule["message"])
        self.assertNotRegex(rule["message"], r"\{[^{}]+\}")



if __name__ == "__main__":
    unittest.main()
