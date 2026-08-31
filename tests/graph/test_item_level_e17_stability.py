from __future__ import annotations

import json
import unittest

import zen

from apps.builders.entertainment_graph import (
    _build_content_compliance_llm_node,
    _build_content_compliance_postprocess_node,
    _build_recharge_card_check_node,
)
from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
from graph_runtime.core import load_decision


class PersonalTransportE17StabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(OFFICIAL_GRAPH_PATHS["personal_transport"].read_text(encoding="utf-8"))
        cls.prompt_source = next(
            node["content"]["source"]
            for node in cls.graph["nodes"]
            if node.get("id") == "33a6a837-0964-4c10-b451-e277138820aa"
        )
        cls.llm_source = next(
            node["content"]["source"]
            for node in cls.graph["nodes"]
            if node.get("id") == "109af3d5-6dec-4cd0-a09e-de43b026c491"
        )
        cls.expression = next(
            expression["value"]
            for node in cls.graph["nodes"]
            if node.get("name") == "发票内容检查后处理"
            for expression in node["content"]["expressions"]
            if expression.get("key") == "rechargeCardCheck"
        )
        cls.recharge_node = next(
            node for node in cls.graph["nodes"] if node.get("name") == "发票充值卡检查"
        )

    def test_json_and_zen_decision_compile(self) -> None:
        self.assertIsNotNone(load_decision(OFFICIAL_GRAPH_PATHS["personal_transport"]))

    def test_prompt_uses_items_without_taking_e36_scope(self) -> None:
        required_fragments = (
            "只负责识别个人交通费发票",
            "E17 不判断机票、保险、维修保养、违约金",
            "由 E36 发票内容项目检查处理",
            "必须逐项看 auditItems",
            "JSON.stringify(promptItems)",
            "hitItems",
            "不确定或语义模糊时，默认 passed=true",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.prompt_source)

        self.assertNotIn("发票内容是：${goodsName}", self.prompt_source)
        self.assertNotIn("- 输入：*现代服务*违约金 ➡️ 返回：{\"passed\": false}", self.prompt_source)

    def test_llm_guard_rejects_unsupported_hit(self) -> None:
        for fragment in (
            "E17结果保护",
            "llmResult.passed === false",
            "explicitRecharge",
            "input.auditItems",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.llm_source)

    def test_result_gate_requires_hit_items(self) -> None:
        success = {"llm_status": "success", "llm_result": {"passed": True}}
        no_hit_reject = {"llm_status": "success", "llm_result": {"passed": False}}
        explicit_reject = {
            "llm_status": "success",
            "llm_result": {"passed": False, "hitItems": "加油卡充值"},
        }
        failure = {"llm_status": "error", "llm_result": None}

        self.assertEqual(zen.evaluate_expression(self.expression, success), "true")
        self.assertEqual(zen.evaluate_expression(self.expression, no_hit_reject), "true")
        self.assertEqual(zen.evaluate_expression(self.expression, explicit_reject), "false")
        self.assertEqual(zen.evaluate_expression(self.expression, failure), "error")

    def test_reject_message_uses_hit_items(self) -> None:
        message_id = "509fd9ba-3996-4e4a-9021-df6513ed6807"
        result_id = "f35ede49-0eae-4dda-b39e-11a11383697a"
        input_id = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"
        reject_rule = next(
            rule
            for rule in self.recharge_node["content"]["rules"]
            if rule.get(result_id) == '"REJECT"'
            and rule.get(input_id) == '"false"'
        )
        message = reject_rule[message_id]
        self.assertIn("llm_result.hitItems", message)
        self.assertIn("goodsName", message)


class EntertainmentE17StabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(OFFICIAL_GRAPH_PATHS["entertainment"].read_text(encoding="utf-8"))
        cls.prompt_source = next(
            node["content"]["source"]
            for node in cls.graph["nodes"]
            if node.get("id") == "ent-content-compliance-prompt"
        )
        cls.llm_source = next(
            node["content"]["source"]
            for node in cls.graph["nodes"]
            if node.get("id") == "ent-content-compliance-llm"
        )
        cls.postprocess = next(
            node for node in cls.graph["nodes"]
            if node.get("id") == "ent-content-compliance-postprocess"
        )
        cls.expressions = {
            expression["key"]: expression["value"]
            for expression in cls.postprocess["content"]["expressions"]
        }
        cls.recharge_node = next(
            node for node in cls.graph["nodes"] if node.get("id") == "ent-recharge-card-check"
        )

    def test_json_and_zen_decision_compile(self) -> None:
        self.assertIsNotNone(load_decision(OFFICIAL_GRAPH_PATHS["entertainment"]))

    def test_keeps_single_llm_call_but_uses_explicit_hits(self) -> None:
        # 业务招待费已把 E36 + E17 合并成一个 LLM 调用；本优化不增加第二次调用。
        self.assertEqual(
            len([node for node in self.graph["nodes"] if node.get("name") == "调用llm"]), 1
        )
        self.assertIn("rechargeHitItems", self.prompt_source)
        self.assertIn("auditItems", self.prompt_source)
        self.assertIn("rechargeHitItems", self.llm_source)
        self.assertIn("rechargeCheckResult", self.expressions)
        self.assertEqual(
            self.recharge_node["content"]["inputs"][0]["field"],
            "rechargeCheckResult",
        )

    def test_e17_gate_preserves_e36_behavior(self) -> None:
        result = {"llm_status": "success", "llm_result": {"passed": False, "violationType": "recharge_card"}}
        self.assertEqual(
            zen.evaluate_expression(self.expressions["contentCheckResult"], result),
            "recharge_card",
        )
        self.assertEqual(
            zen.evaluate_expression(self.expressions["rechargeCheckResult"], result),
            "pass",
        )

        explicit = {
            "llm_status": "success",
            "llm_result": {
                "passed": False,
                "violationType": "recharge_card",
                "rechargeHitItems": "充值卡",
            },
        }
        self.assertEqual(
            zen.evaluate_expression(self.expressions["contentCheckResult"], explicit),
            "recharge_card",
        )
        self.assertEqual(
            zen.evaluate_expression(self.expressions["rechargeCheckResult"], explicit),
            "recharge_card",
        )

        prohibited = {
            "llm_status": "success",
            "llm_result": {
                "passed": False,
                "violationType": "prohibited_item",
                "rechargeHitItems": "",
            },
        }
        self.assertEqual(
            zen.evaluate_expression(self.expressions["contentCheckResult"], prohibited),
            "prohibited_item",
        )
        self.assertEqual(
            zen.evaluate_expression(self.expressions["rechargeCheckResult"], prohibited),
            "pass",
        )

    def test_builder_and_official_graph_share_item_level_guard(self) -> None:
        builder_prompt = _build_content_compliance_llm_node()["content"]["source"]
        builder_postprocess = _build_content_compliance_postprocess_node()["content"]
        builder_recharge = _build_recharge_card_check_node()["content"]

        self.assertIn("rechargeHitItems", builder_prompt)
        self.assertIn("input.auditItems", builder_prompt)
        self.assertIn("rechargeCheckResult", {x["key"] for x in builder_postprocess["expressions"]})
        self.assertEqual(builder_recharge["inputs"][0]["field"], "rechargeCheckResult")
