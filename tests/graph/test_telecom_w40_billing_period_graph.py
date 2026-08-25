import json
import shutil
import subprocess
import unittest
from pathlib import Path

import zen


GRAPH_PATH = Path(__file__).parents[2] / "resources" / "graphs" / "graph-latest-telecom-0727-1900.json"
PROMPT_NODE_ID = "6acb7b84-51a3-4d7d-9556-960d459d518d"
POSTPROCESS_NODE_ID = "a6e16f2e-1e43-4f71-8895-0ee44210a4c7"
W40_NODE_ID = "f27969dd-cdcd-43da-9e75-328e4546239c"
W40_MESSAGE_FIELD_ID = "509fd9ba-3996-4e4a-9021-df6513ed6807"
W40_INPUT_FIELD_ID = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"


class TelecomW40BillingPeriodGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.prompt_node = next(node for node in cls.graph["nodes"] if node["id"] == PROMPT_NODE_ID)
        cls.w40_node = next(node for node in cls.graph["nodes"] if node["id"] == W40_NODE_ID)
        cls.postprocess_node = next(node for node in cls.graph["nodes"] if node["id"] == POSTPROCESS_NODE_ID)

    def test_prompt_extracts_qizhi_period_and_preserves_year_check(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is required to execute graph function-node source")

        source = self.prompt_node["content"]["source"]
        result = self._run_prompt_handler(
            source,
            "15259918011 起止时间:20241112-20241112;;销方开户银行：中国建设银行股份有限公司福建省分行",
        )

        self.assertEqual(result["billingPeriod"], "20241112-20241112")
        self.assertEqual(result["billingPeriodExtracted"], "20241112-20241112")
        self.assertFalse(result["fallbackIsInvoiceYearMatched"])

    def test_prompt_keeps_existing_账期月_format(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is required to execute graph function-node source")

        source = self.prompt_node["content"]["source"]
        result = self._run_prompt_handler(
            source,
            "业务号码:13601304869 账期月:20251201--20251231 应付:248.00",
        )

        self.assertEqual(result["billingPeriod"], "20251201--20251231")
        self.assertTrue(result["fallbackIsInvoiceYearMatched"])

    def test_prompt_requires_llm_to_return_billing_period(self) -> None:
        source = self.prompt_node["content"]["source"]

        self.assertIn('"billingPeriod": "标准化账期，未识别时为空字符串"', source)
        self.assertIn("不要把“开票日期”“开票时间”“出票日期”“收款日期”等日期当作 billingPeriod", source)

    def test_postprocess_prefers_non_empty_llm_billing_period(self) -> None:
        expression = self._postprocess_expression("billingPeriod")
        result = zen.evaluate_expression(
            expression,
            {
                "llm_status": "success",
                "llm_result": {"billingPeriod": "20241112-20241112"},
                "billingPeriodExtracted": "regex-period",
            },
        )

        self.assertEqual(result, "20241112-20241112")

    def test_postprocess_falls_back_for_empty_success_but_marks_llm_failure(self) -> None:
        expression = self._postprocess_expression("billingPeriod")

        empty_result = zen.evaluate_expression(
            expression,
            {
                "llm_status": "success",
                "llm_result": {"billingPeriod": ""},
                "billingPeriodExtracted": "20241112-20241112",
            },
        )
        failed_result = zen.evaluate_expression(
            expression,
            {
                "llm_status": "error",
                "llm_result": None,
                "billingPeriodExtracted": "20241112-20241112",
            },
        )

        self.assertEqual(empty_result, "20241112-20241112")
        self.assertEqual(failed_result, "error")

    def test_postprocess_falls_back_to_regex_when_llm_phone_is_empty(self) -> None:
        expression = self._postprocess_expression("remarkPhone")
        result = zen.evaluate_expression(
            expression,
            {
                "llm_status": "success",
                "llm_result": {"remarkPhone": ""},
                "remarkPhoneExtracted": "15259918011",
            },
        )

        self.assertEqual(result, "15259918011")

    def test_w40_message_falls_back_when_billing_period_is_empty(self) -> None:
        reject_rule = next(
            rule
            for rule in self.w40_node["content"]["rules"]
            if rule[W40_INPUT_FIELD_ID] == "false"
        )
        message_expression = reject_rule[W40_MESSAGE_FIELD_ID]
        prepared = {
            "invoiceNo": "24357000000034577982",
            "billingPeriod": "",
            "remark": "15259918011 起止时间:20241112-20241112",
            "serviceData": {"auditInfo": {"submitTime": "2026-08-17 16:27:01"}},
        }

        message = zen.evaluate_expression(message_expression, prepared)

        self.assertIn("账单周期为 15259918011 起止时间:20241112-20241112", message)
        self.assertNotIn("账单周期为 ，", message)

    def _postprocess_expression(self, key: str) -> str:
        return next(
            expression["value"]
            for expression in self.postprocess_node["content"]["expressions"]
            if expression["key"] == key
        )

    @staticmethod
    def _run_prompt_handler(source: str, remark: str) -> dict:
        script = (
            source
            + "\n"
            + "handler({remark: "
            + json.dumps(remark, ensure_ascii=False)
            + ", context: {serviceData: {auditInfo: {submitTime: '2026-08-17 16:27:01'}}}})"
            + ".then((result) => process.stdout.write(JSON.stringify(result)));"
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
