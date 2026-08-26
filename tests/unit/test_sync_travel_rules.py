from __future__ import annotations

import unittest

from apps.cli.sync_travel_rules import EXPECTED_HEADERS, _active_cell_text, normalize_rows


class TravelRuleSyncTests(unittest.TestCase):
    def test_active_cell_text_ignores_struck_fragments(self) -> None:
        cell = {
            "value": "E32 自驾车费用项申请金额超标\nE31-1 自驾车表单发票金额不足",
            "rich_text": [
                {
                    "text": "E32 自驾车费用项申请金额超标\n",
                    "style": {"font_line": "line-through"},
                },
                {"text": "E31-1 自驾车表单发票金额不足", "style": {}},
            ],
        }
        self.assertEqual(_active_cell_text(cell), "E31-1 自驾车表单发票金额不足")

    def test_normalize_rows_uses_active_code_and_continues_e20_family(self) -> None:
        def row(source_row: str, raw: str, active: str) -> dict[str, str]:
            values = {header: "" for header in EXPECTED_HEADERS}
            values.update(
                {
                    "审核时看什么": f"规则 {source_row}",
                    "员工端显示报错问题": raw,
                    "source_row": source_row,
                    "_active_reason_code_source": active,
                }
            )
            return values

        result = normalize_rows(
            [
                row("5", "E20-1 日期错误", "E20-1 日期错误"),
                row("9", "E20-2 日期错误", "E20-2 日期错误"),
                row("15", "E20-3 日期错误", "E20-3 日期错误"),
                row("19", "E20-4 日期错误", "E20-4 日期错误"),
                row("12", "E20 日期错误", "E20 日期错误"),
                row("10", "E32 旧问题\nE31-1 当前问题", "E31-1 当前问题"),
                row("37", "新增 E39 旧问题 修改 W36 当前问题", "修改 W36 当前问题"),
            ]
        )
        self.assertEqual(
            [item["reason_code"] for item in result],
            ["E20-1", "E20-2", "E20-3", "E20-4", "E20-5", "E31-1", "W36"],
        )

    def test_normalize_rows_rejects_multiple_active_codes(self) -> None:
        values = {header: "" for header in EXPECTED_HEADERS}
        values.update(
            {
                "审核时看什么": "重复代码规则",
                "员工端显示报错问题": "E32 旧问题\nE31-1 当前问题",
                "source_row": "10",
                "_active_reason_code_source": "E32 仍有效\nE31-1 也有效",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "exactly one active reason code"):
            normalize_rows([values])


if __name__ == "__main__":
    unittest.main()
