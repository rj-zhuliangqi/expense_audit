from __future__ import annotations

import json
import unittest

import zen

from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS
from graph_runtime.core import load_decision

GRAPH_PATH = OFFICIAL_GRAPH_PATHS["telecom"]
E17_PROMPT_NODE_ID = "33a6a837-0964-4c10-b451-e277138820aa"
E17_LLM_NODE_ID = "109af3d5-6dec-4cd0-a09e-de43b026c491"


class TelecomE17RechargePromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.decision = load_decision(GRAPH_PATH)
        cls.prompt_source = next(
            node["content"]["source"]
            for node in cls.graph["nodes"]
            if node.get("id") == E17_PROMPT_NODE_ID
        )
        cls.llm_source = next(
            node["content"]["source"]
            for node in cls.graph["nodes"]
            if node.get("id") == E17_LLM_NODE_ID
        )
        cls.postprocess_node = next(
            node
            for node in cls.graph["nodes"]
            if node.get("name") == "发票内容检查后处理"
        )
        cls.recharge_expression = next(
            expression["value"]
            for expression in cls.postprocess_node["content"]["expressions"]
            if expression.get("key") == "rechargeCardCheck"
        )
        cls.recharge_node = next(
            node
            for node in cls.graph["nodes"]
            if node.get("name") == "发票充值卡检查"
        )

    def test_graph_json_and_zen_decision_compile(self) -> None:
        self.assertIsNotNone(self.decision)
        self.assertEqual(
            self.graph["contentType"],
            "application/vnd.gorules.decision",
        )

    def test_prompt_is_item_level_and_does_not_overlap_e34(self) -> None:
        required_fragments = (
            "只负责识别",
            "充值、预付、储值或卡类/号码载体",
            "必须逐项看 auditItems",
            "不要判断“信息系统服务费”“终端费”",
            "由 E34 发票内容金额检查处理",
            "税收分类前缀",
            "只有至少一个明细名称本身明确命中",
            "不确定或语义模糊时，默认 passed=true",
            "hitItems",
            "JSON.stringify(promptItems",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.prompt_source)

        # 旧 prompt 会把整张发票聚合内容交给模型，并让“宽带费或实物消费”
        # 进入 E17 判断，导致和 E34 的职责重叠。
        self.assertNotIn("发票内容是：${goodsName}", self.prompt_source)
        self.assertNotIn("宽带费或实物消费", self.prompt_source)

    def test_llm_result_guard_requires_explicit_recharge_hit(self) -> None:
        required_fragments = (
            "llmResult.passed === false",
            "hitText",
            "originalItemsText",
            "explicitRecharge",
            "E17结果保护",
            "passed: true",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.llm_source)

    def test_result_gate_blocks_reject_without_hit_items(self) -> None:
        passed = {
            "recharge_llm_status": "success",
            "recharge_llm_result": {"passed": True, "hitItems": ""},
        }
        self.assertEqual(
            zen.evaluate_expression(self.recharge_expression, passed),
            "true",
        )

        # LLM 无理由返回 false 时，结果门禁不能直接触发 E17 reject。
        unsupported_reject = {
            "recharge_llm_status": "success",
            "recharge_llm_result": {"passed": False},
        }
        self.assertEqual(
            zen.evaluate_expression(self.recharge_expression, unsupported_reject),
            "true",
        )

        explicit_reject = {
            "recharge_llm_status": "success",
            "recharge_llm_result": {
                "passed": False,
                "hitItems": "SIM卡费",
            },
        }
        self.assertEqual(
            zen.evaluate_expression(self.recharge_expression, explicit_reject),
            "false",
        )

        error = {
            "recharge_llm_status": "error",
            "recharge_llm_result": None,
        }
        self.assertEqual(
            zen.evaluate_expression(self.recharge_expression, error),
            "error",
        )

    def test_reject_message_uses_invoice_number_and_hit_items(self) -> None:
        message_id = "509fd9ba-3996-4e4a-9021-df6513ed6807"
        result_id = "f35ede49-0eae-4dda-b39e-11a11383697a"
        input_id = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"
        reject_rule = next(
            rule
            for rule in self.recharge_node["content"]["rules"]
            if rule.get(result_id) == '"REJECT"'
            and rule.get(input_id) == '"false"'
        )
        message_expression = reject_rule[message_id]

        self.assertIn("invoiceNo", message_expression)
        self.assertIn("recharge_llm_result.hitItems", message_expression)
        self.assertIn("公司制度不允许使用充值卡、预付卡、预存款类发票报销通讯费", message_expression)
        self.assertNotIn("{发票号}", message_expression)
        self.assertNotIn("{命中项目}", message_expression)


if __name__ == "__main__":
    unittest.main()
