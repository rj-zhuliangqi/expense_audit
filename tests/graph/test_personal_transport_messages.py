from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from graph_runtime.application import evaluate_prepared_input
from graph_runtime.core import load_decision
from expense_audit_orchestrator import audit_client
from expense_audit_orchestrator.writeback import assemble_result_audit_info
from expense_audit_orchestrator.profiles.personal_transport.data import (
    build_taxi_invoice_serial_enricher,
    is_taxi_invoice,
    personal_transport_invoice_type_enricher,
    normalize_invoice_serial_prefix,
)


from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
GRAPH_PATH = OFFICIAL_GRAPH_PATHS["personal_transport"]
MESSAGE_FIELD_ID = "509fd9ba-3996-4e4a-9021-df6513ed6807"


class PersonalTransportMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.decision = load_decision(GRAPH_PATH)

    def test_e34_taxi_node_uses_e34_alias_and_explicit_history_gate(self) -> None:
        graph_text = json.dumps(self.graph, ensure_ascii=False)
        taxi_nodes = [
            node
            for node in self.graph["nodes"]
            if node.get("name") == "出租车连号检查"
        ]
        self.assertEqual(len(taxi_nodes), 1)
        self.assertEqual(taxi_nodes[0]["id"], "travel_e34_taxi_consecutive_check")
        self.assertNotIn("w19", graph_text.lower())

        expression_node = next(
            node
            for node in self.graph["nodes"]
            if node.get("type") == "expressionNode"
            and any(
                expression.get("key") == "isTaxiConsecutive"
                for expression in node.get("content", {}).get("expressions", [])
            )
        )
        expressions = {
            expression["key"]: expression["value"]
            for expression in expression_node["content"]["expressions"]
        }
        self.assertIn("isTaxiHistoricalConsecutive", expressions)
        self.assertIn("historyHit", expressions["isTaxiHistoricalConsecutive"])
        self.assertIn("isTaxiHistoricalConsecutive", expressions["isTaxiConsecutive"])
        self.assertIn("isTaxiBatchConsecutive", expressions["isTaxiConsecutive"])

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
                is_llm_failure = (
                    node["name"] in {"发票充值卡检查", "发票内容项目检查"}
                    and rule.get(content["inputs"][0]["id"]) == '"error"'
                )
                if not is_llm_failure and value != '""':
                    self.assertIn("+", value, node["name"])
                    dynamic_count += 1
        self.assertEqual(dynamic_count, 17)

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
            "E01": "",
            "E02": "",
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

    def test_e01_header_mismatch_is_rejected_for_applicable_invoice_type(self) -> None:
        prepared = self._base_input()
        prepared["invoiceType"] = "1"

        result = evaluate_prepared_input(self.decision, prepared, trace=False)

        self.assertEqual(self._rule(result, "E01")["distinguish_result"], "REJECT")

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
            "instance_code": "REC-788",
            "invoice_file_id": "FILE-788",
            "invoice_info_id": "INFO-788",
        })

        result = evaluate_prepared_input(self.decision, prepared, trace=True)
        e17 = self._rule(result, "E17")
        self.assertEqual(e17["distinguish_result"], "REJECT")
        self.assertEqual(
            e17["message"],
            "票据 发票号 788 的明细中包含“某某储值账户服务”，属于预付、充值、预存类内容，不符合交通费报销要求。",
        )
        self.assertEqual(e17["instance_code"], "REC-788")
        self.assertEqual(e17["invoice_file_id"], "FILE-788")
        self.assertEqual(e17["invoice_info_id"], "INFO-788")

        e17_llm = next(
            value for value in (result["trace"] or {}).values()
            if isinstance(value, dict)
            and value.get("name") == "调用llm"
            and value.get("output", {}).get("llm_result", {}).get("passed") is False
        )
        self.assertTrue(e17_llm["output"].get("skipped"))
        self.assertEqual(e17_llm["output"]["invoiceNo"], "788")
        self.assertEqual(e17_llm["output"]["goodsName"], "某某储值账户服务")

    def test_e36_llm_failure_keeps_invoice_context_for_writeback(self) -> None:
        prepared = self._base_input()
        prepared.update({
            "invoiceNo": "E36-FAIL",
            "goodsName": "金徽章",
            "items": [{"goodsName": "金徽章"}],
            "instance_code": "REC-E36-FAIL",
            "invoice_file_id": "FILE-E36-FAIL",
            "invoice_info_id": "INFO-E36-FAIL",
        })

        # 测试环境不配置 LLM 网关，E36 必须以 REJECT 暴露模型异常；
        # 决策表仍必须保留原始发票主键，避免回写成 null。
        result = evaluate_prepared_input(self.decision, prepared, trace=True)
        e36 = self._rule(result, "E36")
        self.assertEqual(e36["distinguish_result"], "REJECT")
        self.assertEqual(e36["instance_code"], "REC-E36-FAIL")
        self.assertEqual(e36["invoice_file_id"], "FILE-E36-FAIL")
        self.assertEqual(e36["invoice_info_id"], "INFO-E36-FAIL")
        self.assertEqual(
            e36["message"],
            "模型服务暂时异常，当前发票内容项目检查未完成，请联系管理员处理。",
        )
        self.assertNotIn("llmGatewayUrl not injected", e36["message"])

        e36_llm = (result["trace"] or {})["514e15db-3657-4fa3-9228-88b750ea08f8"]
        self.assertEqual(e36_llm["output"]["invoiceNo"], "E36-FAIL")
        self.assertEqual(e36_llm["output"]["goodsName"], "金徽章")

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

        # E36 的交通费允许清单必须覆盖最新的经营租赁/共享出行场景，
        # 避免模型将共享单车发票误判为普通非交通租赁服务。
        for fragment in (
            "代驾", "停车", "电费", "供电", "充电", "客运", "车位管理费",
            "通行费", "代订车", "信息系统增值服务", "车辆停放", "运输服务",
            "停车占道费", "*经营租赁*租赁服务", "共享单车", "共享电单车", "扫码骑行"
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, sources["发票合规prompt"])
        self.assertIn("skipLlm", sources["充值卡检查prompt"])
        self.assertIn("skipLlm", sources["发票合规prompt"])
        self.assertIn("直接返回，不调用远端 LLM", sources["调用llm"])

        e36_node = next(
            node for node in self.graph["nodes"] if node.get("id") == "travel_e36_content_project_check"
        )
        e36_reject = next(
            rule for rule in e36_node["content"]["rules"]
            if (
                rule.get("f35ede49-0eae-4dda-b39e-11a11383697a") == '"REJECT"'
                and rule.get("dea9a1bc-66ae-47b3-885f-9e9a1bb07571") == "false"
            )
        )
        e36_message = e36_reject[MESSAGE_FIELD_ID]
        for fragment in ("*经营租赁*租赁服务", "共享单车", "共享电单车", "停车占道费"):
            with self.subTest(message_fragment=fragment):
                self.assertIn(fragment, e36_message)

        # LLM 只负责返回 passed；E17/E36 决策表还需要原始发票号、内容和主键。
        for node in self.graph["nodes"]:
            if node.get("type") != "functionNode":
                continue
            if node.get("name") in {"充值卡检查prompt", "发票合规prompt", "调用llm"}:
                source = node["content"]["source"]
                self.assertIn("invoiceContext", source, node["name"])
                self.assertIn("invoiceNo: input?.invoiceNo", source, node["name"])
                self.assertIn("invoice_file_id: input?.invoice_file_id", source, node["name"])

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

    def test_taxi_serial_enricher_uses_taxi_history_interface_by_default(self) -> None:
        with patch.object(
            audit_client,
            "fetch_taxi_invoice_serial_numbers",
            return_value=["12345699"],
        ) as taxi_provider, patch.object(
            audit_client,
            "fetch_invoice_serial_numbers",
            side_effect=AssertionError("personal transport must not use generic serial interface"),
        ):
            enricher = build_taxi_invoice_serial_enricher(
                service_url="https://service.example"
            )
            result = enricher(
                "REC-TAXI-DEFAULT",
                "invoice.pdf",
                {"invoiceType": "8", "invoiceNo": "12345601", "accountingCode": "111"},
                {"auditInfo": {"instanceCode": "REC-TAXI-DEFAULT"}},
            )

        taxi_provider.assert_called_once_with(
            "12345601",
            "REC-TAXI-DEFAULT",
            "111",
            service_url="https://service.example",
        )
        self.assertEqual(result["historyNumbers"], ["12345699"])
        self.assertTrue(result["historyHit"])

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

    def _w36_rule(self, prepared: dict) -> dict:
        result = evaluate_prepared_input(self.decision, prepared, trace=False)
        return self._rule(result, "W36")

    def test_w36_supported_invoice_types_use_configured_amount_field(self) -> None:
        cases = [
            ("15", "14", None, "taxAmount", "11"),
            ("1", "12", None, "taxAmount", "12"),
            ("26", "12", None, "taxAmount", "13"),
            ("29", "24", None, "totalAmount", "14"),
            ("28", "25", "1", "totalAmount", "15"),
            ("4", None, None, "taxAmount", "16"),
            ("2", None, None, "taxAmount", "17"),
            ("27", None, None, "taxAmount", "18"),
        ]
        for invoice_type, special_mark, international_flag, compare_field, amount in cases:
            with self.subTest(invoice_type=invoice_type):
                prepared = self._base_input()
                prepared.update(
                    {
                        "invoiceType": invoice_type,
                        "specialTypeMark": special_mark,
                        "effectiveTaxAmount": f"{amount}.00",
                        "taxAmount": "999",
                        "totalAmount": "999",
                    }
                )
                if international_flag is not None:
                    prepared["internationalFlag"] = international_flag
                prepared[compare_field] = amount
                rule = self._w36_rule(prepared)
                self.assertEqual(rule["distinguish_result"], "PASS")
                self.assertEqual(rule["reason_code"], "W36")
                self.assertEqual(rule["audit_content"], "检查发票可抵扣税额与核销单发票进项税额是否一致")
                self.assertEqual(rule["audit_type"], "general-rules")
                self.assertEqual(rule["message"], "")
                self.assertEqual(rule["problem_category"], "")
                self.assertEqual(rule["optimization_action_category"], "")
                self.assertEqual(rule["employeeSuggestionTips"], "")

    def test_w36_code_conditions_use_numeric_semantics_for_string_and_number_values(self) -> None:
        prepared = self._base_input()
        prepared.update(
            {
                "invoiceType": 1,
                "specialTypeMark": 12,
                "taxAmount": "1.00",
                "effectiveTaxAmount": "1",
            }
        )
        self.assertEqual(self._w36_rule(prepared)["distinguish_result"], "PASS")

        prepared.update(
            {
                "invoiceType": "28",
                "specialTypeMark": 25,
                "internationalFlag": 1,
                "totalAmount": "2.00",
                "effectiveTaxAmount": 2,
            }
        )
        self.assertEqual(self._w36_rule(prepared)["distinguish_result"], "PASS")

    def test_w36_supported_invoice_types_reject_amount_mismatch(self) -> None:
        cases = [
            ("15", "14", None, "taxAmount"),
            ("1", "12", None, "taxAmount"),
            ("26", "12", None, "taxAmount"),
            ("29", "24", None, "totalAmount"),
            ("28", "25", "1", "totalAmount"),
            ("4", None, None, "taxAmount"),
            ("2", None, None, "taxAmount"),
            ("27", None, None, "taxAmount"),
        ]
        for invoice_type, special_mark, international_flag, compare_field in cases:
            with self.subTest(invoice_type=invoice_type):
                prepared = self._base_input()
                prepared.update(
                    {
                        "invoiceType": invoice_type,
                        "specialTypeMark": special_mark,
                        "taxAmount": "10",
                        "totalAmount": "10",
                        "effectiveTaxAmount": "11.00",
                    }
                )
                if international_flag is not None:
                    prepared["internationalFlag"] = international_flag
                prepared[compare_field] = "10"
                rule = self._w36_rule(prepared)
                self.assertEqual(rule["distinguish_result"], "REJECT")
                self.assertEqual(
                    rule["message"],
                    "本次发票可抵扣税额合计为 10 元，与核销单发票进项税额 11.00 元不一致。",
                )
                self.assertEqual(rule["problem_category"], "税额不一致")
                self.assertEqual(rule["optimization_action_category"], "【核对税额】")
                self.assertEqual(
                    rule["employeeSuggestionTips"],
                    "请核对发票税额和表单税额是否录入正确；无法确认的，系统将标记高风险并转财务审核。",
                )
                self.assertEqual(rule["policiesIndex"], "无")

    def test_w36_special_type_mismatch_uses_zero(self) -> None:
        cases = [
            ("15", "99", None, "taxAmount"),
            ("1", "99", None, "taxAmount"),
            ("26", "99", None, "taxAmount"),
            ("29", "99", None, "totalAmount"),
            ("28", "25", "0", "totalAmount"),
        ]
        for invoice_type, special_mark, international_flag, compare_field in cases:
            with self.subTest(invoice_type=invoice_type, international_flag=international_flag):
                prepared = self._base_input()
                prepared.update(
                    {
                        "invoiceType": invoice_type,
                        "specialTypeMark": special_mark,
                        "taxAmount": "not-a-number",
                        "totalAmount": "100",
                        "effectiveTaxAmount": "0.00",
                    }
                )
                if international_flag is not None:
                    prepared["internationalFlag"] = international_flag
                if compare_field == "taxAmount":
                    prepared[compare_field] = "not-a-number"
                self.assertEqual(self._w36_rule(prepared)["distinguish_result"], "PASS")

                prepared["effectiveTaxAmount"] = "0.01"
                rule = self._w36_rule(prepared)
                self.assertEqual(rule["distinguish_result"], "REJECT")
                self.assertIn("合计为 0 元", rule["message"])

    def test_w36_air_ticket_requires_special_mark_and_international_flag(self) -> None:
        for special_mark, international_flag in (("25", "1"), ("25", "0"), ("24", "1"), ("25", None)):
            with self.subTest(special_mark=special_mark, international_flag=international_flag):
                prepared = self._base_input()
                prepared.update(
                    {
                        "invoiceType": "28",
                        "specialTypeMark": special_mark,
                        "totalAmount": "20",
                        "taxAmount": "999",
                        "effectiveTaxAmount": "20",
                    }
                )
                if international_flag is not None:
                    prepared["internationalFlag"] = international_flag
                rule = self._w36_rule(prepared)
                expected = "PASS" if (special_mark, international_flag) == ("25", "1") else "REJECT"
                self.assertEqual(rule["distinguish_result"], expected)

    def test_w36_non_applicable_invoice_passes_even_with_invalid_amounts(self) -> None:
        prepared = self._base_input()
        prepared.update(
            {
                "invoiceType": "3",
                "specialTypeMark": "not-a-number",
                "taxAmount": "not-a-number",
                "totalAmount": "100",
                "effectiveTaxAmount": "not-a-number",
            }
        )
        rule = self._w36_rule(prepared)
        self.assertEqual(rule["distinguish_result"], "PASS")
        self.assertEqual(rule["message"], "")

    def test_w36_ignores_amount_format_but_rejects_missing_empty_and_non_numeric_values(self) -> None:
        formatted = self._base_input()
        formatted.update(
            {
                "invoiceType": "1",
                "specialTypeMark": "12",
                "taxAmount": "1.00",
                "effectiveTaxAmount": 1,
            }
        )
        self.assertEqual(self._w36_rule(formatted)["distinguish_result"], "PASS")

        invalid_cases = [
            {"invoiceType": "1", "specialTypeMark": "12", "effectiveTaxAmount": "1"},
            {"invoiceType": "1", "specialTypeMark": "12", "taxAmount": "", "effectiveTaxAmount": "1"},
            {"invoiceType": "1", "specialTypeMark": "12", "taxAmount": "abc", "effectiveTaxAmount": "1"},
            {"invoiceType": "1", "specialTypeMark": "12", "taxAmount": "1"},
            {"invoiceType": "1", "specialTypeMark": "12", "taxAmount": "1", "effectiveTaxAmount": ""},
            {"invoiceType": "1", "specialTypeMark": "12", "taxAmount": "1", "effectiveTaxAmount": "abc"},
        ]
        for changes in invalid_cases:
            with self.subTest(changes=changes):
                prepared = self._base_input()
                prepared.update(changes)
                self.assertEqual(self._w36_rule(prepared)["distinguish_result"], "REJECT")

    def test_w36_is_written_back_once_per_invoice(self) -> None:
        prepared_inputs = []
        invoice_results = []
        for invoice_key, invoice_type, tax_amount, effective_tax_amount in (
            ("F-W36-001", "1", "1", "1"),
            ("F-W36-002", "1", "2", "1"),
        ):
            prepared = self._base_input()
            prepared.update(
                {
                    "invoiceType": invoice_type,
                    "specialTypeMark": "12",
                    "taxAmount": tax_amount,
                    "effectiveTaxAmount": effective_tax_amount,
                    "invoice_file_id": invoice_key,
                    "invoice_info_id": f"I-{invoice_key}",
                    "instance_code": "REC-W36-MULTI",
                }
            )
            result = evaluate_prepared_input(self.decision, prepared, trace=False)
            prepared_inputs.append({"invoiceKey": invoice_key, "preparedInput": prepared})
            invoice_results.append(
                {
                    "invoiceKey": invoice_key,
                    "preparedInput": prepared,
                    "decisionOutput": result["decisionOutput"],
                    "decisionStatus": result["checkStatus"],
                }
            )

        payload = assemble_result_audit_info(
            {
                "receiptCode": "REC-W36-MULTI",
                "serviceData": {"auditInfo": {"instanceCode": "REC-W36-MULTI"}},
                "invoicePreparations": prepared_inputs,
            },
            {"receiptCode": "REC-W36-MULTI", "invoiceResults": invoice_results},
            expense_profile="personal_transport",
        )
        w36_logs = [log for log in payload["auditLogs"] if log["reasonCode"] == "W36"]
        self.assertEqual(len(w36_logs), 2)
        self.assertEqual(
            {(log["invoiceFileId"], log["distinguishResult"]) for log in w36_logs},
            {("F-W36-001", "pass"), ("F-W36-002", "reject")},
        )
        self.assertEqual(
            {log["problemTags"] for log in w36_logs if log["distinguishResult"] == "reject"},
            {"税额不一致"},
        )
        self.assertEqual(
            {log["suggestionTags"] for log in w36_logs if log["distinguishResult"] == "reject"},
            {"【核对税额】"},
        )

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
