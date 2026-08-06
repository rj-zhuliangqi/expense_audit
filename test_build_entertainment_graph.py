import unittest
from pathlib import Path

import zen

from build_entertainment_graph import (
    DST_GRAPH,
    ENTERTAINMENT_PREPROCESS_EXPRESSIONS,
    FRAUD_PREPROCESS_EXPRESSIONS,
    _build_content_compliance_llm_node,
    _build_fraud_address_llm_node,
    build_entertainment_graph,
)
from expense_audit_orchestrator.core import ReceiptDataPreparer, _lookup_enterprise_info


def _is_recently_registered_expression() -> str:
    return next(
        expression["value"]
        for expression in FRAUD_PREPROCESS_EXPRESSIONS
        if expression["key"] == "isRecentlyRegistered"
    )


def _is_not_self_expense_expression() -> str:
    return next(
        expression["value"]
        for expression in ENTERTAINMENT_PREPROCESS_EXPRESSIONS
        if expression["key"] == "isNotSelfExpense"
    )


class SelfExpenseExpressionTests(unittest.TestCase):
    def test_llm_nodes_do_not_forward_request_model(self) -> None:
        llm_sources = [
            _build_content_compliance_llm_node()["content"]["source"],
            _build_fraud_address_llm_node()["content"]["source"],
        ]

        self.assertEqual(len(llm_sources), 2)
        for source in llm_sources:
            self.assertNotIn("payload.model", source)
            self.assertNotIn("input.model", source)
            self.assertNotIn("context.llmModel", source)

    def test_passes_when_passenger_name_is_missing_or_empty(self) -> None:
        expression = _is_not_self_expense_expression()
        audit_info = {"verifiUserName": "刘雪涛"}

        self.assertTrue(zen.evaluate_expression(expression, {"serviceData": {"auditInfo": audit_info}}))
        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {"passengerName": "", "serviceData": {"auditInfo": audit_info}},
            )
        )

    def test_passes_when_passenger_name_differs_from_verification_user(self) -> None:
        expression = _is_not_self_expense_expression()

        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "passengerName": "张三",
                    "serviceData": {"auditInfo": {"verifiUserName": "刘雪涛"}},
                },
            )
        )

    def test_rejects_when_passenger_name_matches_verification_user(self) -> None:
        expression = _is_not_self_expense_expression()

        self.assertFalse(
            zen.evaluate_expression(
                expression,
                {
                    "passengerName": "刘雪涛",
                    "serviceData": {"auditInfo": {"verifiUserName": "刘雪涛"}},
                },
            )
        )

    def test_graph_maps_self_expense_false_to_reject_and_true_to_pass(self) -> None:
        graph = build_entertainment_graph()
        node = next(node for node in graph["nodes"] if node.get("id") == "ent-self-expense-check")
        rules = node["content"]["rules"]

        results_by_input = {
            rule["dea9a1bc-66ae-47b3-885f-9e9a1bb07571"]: rule[
                "f35ede49-0eae-4dda-b39e-11a11383697a"
            ]
            for rule in rules
        }
        self.assertEqual(results_by_input["false"], '"REJECT"')
        self.assertEqual(results_by_input["true"], '"PASS"')


class RecentlyRegisteredExpressionTests(unittest.TestCase):
    def test_handles_missing_and_invalid_dates(self) -> None:
        expression = _is_recently_registered_expression()

        cases = [
            {"serviceData": {}, "invoiceDate": "2026-07-15"},
            {"serviceData": {"salerCompanyInfo": None}, "invoiceDate": "2026-07-15"},
            {
                "serviceData": {"salerCompanyInfo": {"establishDate": "invalid"}},
                "invoiceDate": "2026-07-15",
            },
            {
                "serviceData": {"salerCompanyInfo": {"establishDate": "2026-01-15"}},
                "invoiceDate": "",
            },
        ]

        for context in cases:
            with self.subTest(context=context):
                self.assertFalse(zen.evaluate_expression(expression, context))

    def test_compares_six_calendar_month_window(self) -> None:
        expression = _is_recently_registered_expression()

        cases = [
            ("2026-01-15", "2026-01-15", True),
            ("2026-01-15", "2026-07-15", True),
            ("2026-01-15", "2026-07-16", False),
            ("2026-01-15", "2026-01-14", False),
            ("2025-10-31", "2026-04-30", True),
            ("2025-10-31", "2026-05-01", False),
            ("2024-02-29", "2024-08-29", True),
            ("2024-02-29", "2024-08-30", False),
        ]

        for establish_date, invoice_date, expected in cases:
            context = {
                "serviceData": {"salerCompanyInfo": {"establishDate": establish_date}},
                "invoiceDate": invoice_date,
            }
            with self.subTest(establish_date=establish_date, invoice_date=invoice_date):
                self.assertIs(zen.evaluate_expression(expression, context), expected)


class EnterpriseInfoPreparationTests(unittest.TestCase):
    def test_injects_source_data_without_computing_rule_result(self) -> None:
        service_data = {}
        enterprise_info = {
            "name": "测试企业",
            "taxNo": "91350000TEST",
            "establishDate": "2026-01-15",
            "address": "测试地址",
        }

        _lookup_enterprise_info(
            service_data,
            lambda _: enterprise_info,
            enterprise_info["name"],
        )

        self.assertEqual(service_data["salerCompanyInfo"], enterprise_info)
        self.assertNotIn("isRecentlyRegistered", service_data)

    def test_entertainment_graph_removes_personal_header_check(self) -> None:
        personal_header_node_id = "e2b630c8-096d-4fea-ad4c-986473d8a880"
        graph = build_entertainment_graph()

        self.assertFalse(
            any(node.get("id") == personal_header_node_id for node in graph["nodes"])
        )
        self.assertFalse(
            any(
                personal_header_node_id in {edge.get("sourceId"), edge.get("targetId")}
                for edge in graph["edges"]
            )
        )
        self.assertNotIn("header_personal_check", str(graph))
        self.assertNotIn("header_personal_result", str(graph))

    def test_multi_invoice_enterprise_info_is_cached_by_company_name(self) -> None:
        lookup_calls: list[str] = []
        enterprise_info = {
            "name": "测试企业",
            "taxNo": "91350000TEST",
            "establishDate": "2026-01-15",
            "address": "测试地址",
        }

        def ocr_provider(file_path: str, *_args: object, **_kwargs: object) -> dict:
            return {
                "salerName": "测试企业",
                "salerTaxNo": "91350000TEST",
                "invoiceNo": Path(file_path).stem,
            }

        def qichacha_provider(company_name: str) -> dict:
            lookup_calls.append(company_name)
            return enterprise_info

        preparer = ReceiptDataPreparer(
            ocr_provider=ocr_provider,
            qichacha_provider=qichacha_provider,
            invoice_info_provider=lambda *_args: [],
        )
        receipt_context = {
            "serviceData": {"auditInfo": {"instanceCode": "REC-001"}, "companyList": []},
            "enterpriseInfoCache": {},
        }
        invoice_one = {"filePath": "/tmp/invoice-one.pdf"}
        invoice_two = {"filePath": "/tmp/invoice-two.pdf"}

        first_input = preparer.prepare_invoice_input("REC-001", invoice_one, receipt_context)
        second_input = preparer.prepare_invoice_input("REC-001", invoice_two, receipt_context)

        self.assertEqual(lookup_calls, ["测试企业"])
        self.assertEqual(first_input["serviceData"]["salerCompanyInfo"], enterprise_info)
        self.assertEqual(second_input["serviceData"]["salerCompanyInfo"], enterprise_info)
        self.assertNotIn("enterpriseInfoCache", first_input["serviceData"])

    def test_missing_company_name_does_not_query_by_tax_number(self) -> None:
        lookup_calls: list[str] = []

        def ocr_provider(*_args: object, **_kwargs: object) -> dict:
            return {"salerName": "", "salerTaxNo": "91350000TAXONLY", "invoiceNo": "1"}

        def qichacha_provider(company_name: str) -> dict:
            lookup_calls.append(company_name)
            return {"establishDate": "2026-01-15"}

        preparer = ReceiptDataPreparer(
            ocr_provider=ocr_provider,
            qichacha_provider=qichacha_provider,
            invoice_info_provider=lambda *_args: [],
        )
        receipt_context = {
            "serviceData": {"auditInfo": {"instanceCode": "REC-003"}, "companyList": []},
            "enterpriseInfoCache": {},
        }

        prepared_input = preparer.prepare_invoice_input(
            "REC-003",
            {"filePath": "/tmp/invoice-tax-only.pdf"},
            receipt_context,
        )

        self.assertEqual(lookup_calls, [])
        self.assertIsNone(prepared_input["serviceData"].get("salerCompanyInfo"))

    def test_multi_invoice_enterprise_info_uses_different_tax_numbers_separately(self) -> None:
        lookup_calls: list[str] = []

        def ocr_provider(file_path: str, *_args: object, **_kwargs: object) -> dict:
            suffix = Path(file_path).stem[-1]
            return {
                "salerName": f"测试企业{suffix}",
                "salerTaxNo": f"91350000TEST{suffix}",
                "invoiceNo": suffix,
            }

        def qichacha_provider(company_name: str) -> dict:
            lookup_calls.append(company_name)
            return {
                "name": company_name,
                "taxNo": company_name,
                "establishDate": "2026-01-15",
            }

        preparer = ReceiptDataPreparer(
            ocr_provider=ocr_provider,
            qichacha_provider=qichacha_provider,
            invoice_info_provider=lambda *_args: [],
        )
        receipt_context = {
            "serviceData": {"auditInfo": {"instanceCode": "REC-002"}, "companyList": []},
            "enterpriseInfoCache": {},
        }

        first_input = preparer.prepare_invoice_input("REC-002", {"filePath": "/tmp/invoice-1.pdf"}, receipt_context)
        second_input = preparer.prepare_invoice_input("REC-002", {"filePath": "/tmp/invoice-2.pdf"}, receipt_context)

        self.assertEqual(lookup_calls, ["测试企业1", "测试企业2"])
        self.assertEqual(first_input["serviceData"]["salerCompanyInfo"]["name"], "测试企业1")
        self.assertEqual(second_input["serviceData"]["salerCompanyInfo"]["name"], "测试企业2")


if __name__ == "__main__":
    unittest.main()