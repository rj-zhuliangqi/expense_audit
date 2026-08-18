from __future__ import annotations

import json
import unittest
from pathlib import Path

import zen

from graph_runtime.core import load_decision


from expense_audit_orchestrator.paths import PROJECT_ROOT as ROOT
GRAPH_PATH = ROOT / "graph-latest-0727-1900.json"


class TelecomE34InvoiceContentGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.decision = load_decision(GRAPH_PATH)
        cls.prompt_node = next(
            node for node in cls.graph["nodes"] if node.get("name") == "发票合规prompt"
        )
        cls.prompt_source = cls.prompt_node["content"]["source"]
        cls.amount_node = next(
            node for node in cls.graph["nodes"] if node.get("name") == "发票内容金额检查"
        )
        cls.reject_rule = next(
            rule
            for rule in cls.amount_node["content"]["rules"]
            if rule.get("f35ede49-0eae-4dda-b39e-11a11383697a") == '"REJECT"'
        )
        cls.postprocess_node = next(
            node
            for node in cls.graph["nodes"]
            if node.get("name") == "发票内容检查后处理"
        )
        cls.is_valid_expression = next(
            expression["value"]
            for expression in cls.postprocess_node["content"]["expressions"]
            if expression.get("key") == "isValidInvoiceAmount"
        )

    def test_graph_json_and_zen_decision_compile(self) -> None:
        self.assertIsNotNone(self.decision)
        self.assertEqual(self.graph["contentType"], "application/vnd.gorules.decision")

    def test_prompt_uses_strict_telecom_whitelist(self) -> None:
        required_fragments = (
            "*电信服务*",
            "通信服务费",
            "套餐合约费/套餐固定费",
            "套餐固定费",
            "通话费/语音通话费",
            "短信服务费",
            "流量费",
            "生产生活服务",
            "信息技术服务",
            "现代服务",
            "终端费",
            "机顶盒",
            "光猫设备",
            "技术服务费",
            "信息系统服务费",
            "设备租赁费",
            "宽带安装",
            "解约费",
            "违约金",
            "生产生活服务*信息系统服务费",
            "通信终端设备*终端费",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.prompt_source)

        self.assertIn("hitItems", self.prompt_source)
        self.assertIn("detailAmount", self.prompt_source)
        self.assertIn("逐项", self.prompt_source)
        self.assertIn("多个项目使用中文顿号", self.prompt_source)

    def test_amount_gate_rejects_when_llm_reduced_invoice_amount(self) -> None:
        expression = self.is_valid_expression
        prohibited = {
            "compliance_llm_status": "success",
            "totalAmount": "298.10",
            "compliance_llm_result": {
                "finalAmount": 194.49,
                "hitItems": "*生产生活服务*信息系统服务费、*通信终端设备*终端费",
            },
        }
        self.assertEqual(zen.evaluate_expression(expression, prohibited), "false")

        allowed = {
            "compliance_llm_status": "success",
            "totalAmount": "298.10",
            "compliance_llm_result": {
                "finalAmount": 298.10,
                "hitItems": "",
            },
        }
        self.assertEqual(zen.evaluate_expression(expression, allowed), "true")

        # 旧版 LLM 没有 hitItems 时仍然不能因为 finalAmount 存在而放行。
        legacy_reduced = {
            "compliance_llm_status": "success",
            "totalAmount": "298.10",
            "compliance_llm_result": {"finalAmount": 194.49},
        }
        self.assertEqual(zen.evaluate_expression(expression, legacy_reduced), "false")

    def test_reject_message_uses_invoice_number_and_llm_hit_items(self) -> None:
        message_expression = self.reject_rule[
            "509fd9ba-3996-4e4a-9021-df6513ed6807"
        ]
        self.assertIn("invoiceNo", message_expression)
        self.assertIn("compliance_llm_result.hitItems", message_expression)
        self.assertIn(
            "通讯费仅允许报销*电信服务*大类下，细分通信服务费、套餐合约费/套餐固定费、通话费/语音通话费、短信服务费、流量费小类。",
            message_expression,
        )
        self.assertNotIn("{发票号}", message_expression)
        self.assertNotIn("{命中项目}", message_expression)
        self.assertNotIn("(goodsName ?? \"\") +", message_expression)


if __name__ == "__main__":
    unittest.main()
