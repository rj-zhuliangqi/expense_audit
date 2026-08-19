import json
import shutil
import subprocess
import unittest
from pathlib import Path

import zen


GRAPH_PATH = Path(__file__).parents[2] / "resources" / "graphs" / "graph-latest-0727-1900.json"
PROMPT_NODE_ID = "6acb7b84-51a3-4d7d-9556-960d459d518d"
E32_NODE_ID = "f27969dd-cdcd-43da-9e75-328e4546239c"
E32_MESSAGE_FIELD_ID = "509fd9ba-3996-4e4a-9021-df6513ed6807"
E32_INPUT_FIELD_ID = "dea9a1bc-66ae-47b3-885f-9e9a1bb07571"


class TelecomE32BillingPeriodGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        cls.prompt_node = next(node for node in cls.graph["nodes"] if node["id"] == PROMPT_NODE_ID)
        cls.e32_node = next(node for node in cls.graph["nodes"] if node["id"] == E32_NODE_ID)

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

    def test_e32_message_falls_back_when_billing_period_is_empty(self) -> None:
        reject_rule = next(
            rule
            for rule in self.e32_node["content"]["rules"]
            if rule[E32_INPUT_FIELD_ID] == "false"
        )
        message_expression = reject_rule[E32_MESSAGE_FIELD_ID]
        prepared = {
            "invoiceNo": "24357000000034577982",
            "billingPeriod": "",
            "remark": "15259918011 起止时间:20241112-20241112",
            "serviceData": {"auditInfo": {"submitTime": "2026-08-17 16:27:01"}},
        }

        message = zen.evaluate_expression(message_expression, prepared)

        self.assertIn("账单周期为 15259918011 起止时间:20241112-20241112", message)
        self.assertNotIn("账单周期为 ，", message)

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
