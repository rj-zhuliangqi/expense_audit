import json
import unittest
from pathlib import Path

import zen

from apps.builders.entertainment_graph import (
    DST_GRAPH,
    ENTERTAINMENT_CONTENT_PROMPT_SOURCE,
    ENTERTAINMENT_PREPROCESS_EXPRESSIONS,
    FRAUD_PREPROCESS_EXPRESSIONS,
    _build_content_compliance_check_node,
    _build_content_compliance_llm_node,
    _build_content_compliance_postprocess_node,
    _build_company_header_check_node,
    _build_tax_number_check_node,
    _build_fraud_address_llm_node,
    _build_invoice_number_check_node,
    build_entertainment_graph,
)
from expense_audit_orchestrator.core import ReceiptDataPreparer, _lookup_enterprise_info
from expense_audit_orchestrator.paths import OFFICIAL_GRAPH_PATHS


def _is_recently_registered_expression() -> str:
    return next(
        expression["value"]
        for expression in FRAUD_PREPROCESS_EXPRESSIONS
        if expression["key"] == "isRecentlyRegistered"
    )


def _is_company_exists_expression() -> str:
    return next(
        expression["value"]
        for expression in ENTERTAINMENT_PREPROCESS_EXPRESSIONS
        if expression["key"] == "isCompanyExists"
    )


def _is_buyer_company_expression() -> str:
    return next(
        expression["value"]
        for expression in ENTERTAINMENT_PREPROCESS_EXPRESSIONS
        if expression["key"] == "isBuyerCompany"
    )


def _is_tax_exists_expression() -> str:
    return next(
        expression["value"]
        for expression in ENTERTAINMENT_PREPROCESS_EXPRESSIONS
        if expression["key"] == "isTaxExists"
    )


def _is_not_self_expense_expression() -> str:
    return next(
        expression["value"]
        for expression in ENTERTAINMENT_PREPROCESS_EXPRESSIONS
        if expression["key"] == "isNotSelfExpense"
    )


def _is_gift_count_reasonable_expression() -> str:
    return next(
        expression["value"]
        for expression in ENTERTAINMENT_PREPROCESS_EXPRESSIONS
        if expression["key"] == "isGiftCountReasonable"
    )


def _is_invoice_number_continuous_expression() -> str:
    return next(
        expression["value"]
        for expression in ENTERTAINMENT_PREPROCESS_EXPRESSIONS
        if expression["key"] == "isInvoiceNumberContinuous"
    )


class EntertainmentContentGraphWiringTests(unittest.TestCase):
    def test_content_decision_tables_wait_for_llm_postprocess(self) -> None:
        request_id = "9948bfb0-d9fb-416d-b9a2-b22a875094f0"
        postprocess_id = "ent-content-compliance-postprocess"

        # Check both the generated graph and the checked-in runtime graph. The
        # latter is what production loads, so keeping only the builder test
        # would allow the two graph artifacts to drift apart.
        graphs = [
            build_entertainment_graph(),
            json.loads(
                OFFICIAL_GRAPH_PATHS["entertainment"].read_text(encoding="utf-8")
            ),
        ]
        for graph in graphs:
            edges = {(edge["sourceId"], edge["targetId"]) for edge in graph["edges"]}

            # The request edge is intentionally kept only for the pass-through
            # postprocess node, which supplies invoice context to the decision tables.
            self.assertIn((request_id, postprocess_id), edges)
            for check_id in ("ent-content-compliance-check", "ent-recharge-card-check"):
                self.assertIn((postprocess_id, check_id), edges)
                self.assertNotIn((request_id, check_id), edges)


class CompanyHeaderExpressionTests(unittest.TestCase):
    def test_requires_receipt_company_code_and_matching_company_name(self) -> None:
        expression = _is_company_exists_expression()
        company_list = [
            {"ccode": "111", "companyName": "福建公司"},
            {"ccode": "112", "companyName": "北京公司"},
        ]

        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "instanceComCode": "112",
                    "orgNumber": "111",
                    "orgName": "北京公司",
                    "serviceData": {"companyList": company_list},
                },
            )
        )
        self.assertFalse(
            zen.evaluate_expression(
                expression,
                {
                    "instanceComCode": "112",
                    "orgNumber": "112",
                    "orgName": "福建公司",
                    "serviceData": {"companyList": company_list},
                },
            )
        )

    def test_does_not_match_company_name_from_receipt_company(self) -> None:
        expression = _is_company_exists_expression()
        self.assertFalse(
            zen.evaluate_expression(
                expression,
                {
                    "instanceComCode": "112",
                    "orgNumber": "111",
                    "orgName": "福建公司",
                    "serviceData": {
                        "companyList": [
                            {"ccode": "111", "companyName": "福建公司"},
                            {"ccode": "112", "companyName": "北京公司"},
                        ]
                    },
                },
            )
        )

    def test_supports_actual_company_list_name_fields(self) -> None:
        expression = _is_company_exists_expression()
        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "instanceComCode": "112",
                    "orgNumber": "111",
                    "buyerName": "北京公司",
                    "serviceData": {
                        "companyList": [{"ccode": "112", "cname": "北京公司"}]
                    },
                },
            )
        )

    def test_invoice_org_number_cannot_override_receipt_company(self) -> None:
        expression = _is_company_exists_expression()
        context = {
            "instanceComCode": "111",
            "orgNumber": "112",
            "orgName": "北京公司",
            "serviceData": {
                "companyList": [
                    {"ccode": "111", "companyName": "福建公司"},
                    {"ccode": "112", "companyName": "北京公司"},
                ]
            },
        }
        self.assertFalse(zen.evaluate_expression(expression, context))

    def test_graph_maps_e01_to_pass_and_reject(self) -> None:
        node = _build_company_header_check_node()
        self.assertEqual(node["content"]["inputs"][0]["field"], "header_check")
        self.assertEqual(node["content"]["outputPath"], "header_result")

        results_by_input = {
            rule["dea9a1bc-66ae-47b3-885f-9e9a1bb07571"]: rule[
                "f35ede49-0eae-4dda-b39e-11a11383697a"
            ]
            for rule in node["content"]["rules"]
        }
        self.assertEqual(results_by_input["true"], '"PASS"')
        self.assertEqual(results_by_input["false"], '"REJECT"')
        self.assertTrue(
            all(
                rule["48a29115-f542-44d3-8c02-3ff71e19ee38"] == '"E01"'
                for rule in node["content"]["rules"]
            )
        )

    def test_generated_graph_contains_single_e01_node_connected_to_response(self) -> None:
        graph = build_entertainment_graph()
        e01_nodes = [
            node for node in graph["nodes"]
            if node.get("id") == "d3046965-dbaf-41cd-ba93-0d957fe67ec8"
        ]
        self.assertEqual(len(e01_nodes), 1)
        e01_edges = [
            edge for edge in graph["edges"]
            if edge.get("targetId") == "d3046965-dbaf-41cd-ba93-0d957fe67ec8"
        ]
        self.assertTrue(
            any(edge.get("sourceId") == "c67dcb33-2750-4a43-8af7-8346612c04a9" for edge in e01_edges)
        )
        self.assertTrue(
            any(
                edge.get("sourceId") == "d3046965-dbaf-41cd-ba93-0d957fe67ec8"
                and edge.get("targetId") == "e109e75a-d107-4fd0-a8b3-e3dae7fad15b"
                for edge in graph["edges"]
            )
        )


class TaxNumberExpressionTests(unittest.TestCase):
    def test_classifies_company_suffix_and_employee_name(self) -> None:
        expression = _is_buyer_company_expression()
        self.assertTrue(zen.evaluate_expression(expression, {"buyerName": "北京星网锐捷网络技术有限公司"}))
        self.assertTrue(zen.evaluate_expression(expression, {"buyerName": "某某事务所"}))
        self.assertFalse(zen.evaluate_expression(expression, {"buyerName": "张三"}))

    def test_company_buyer_tax_number_must_match_receipt_company(self) -> None:
        expression = _is_tax_exists_expression()
        base = {
            "buyerName": "北京星网锐捷网络技术有限公司",
            "instanceComCode": "112",
            "orgNumber": "111",
            "serviceData": {
                "companyList": [
                    {"ccode": "111", "companyTax": "TAX-FJ"},
                    {"ccode": "112", "companyTax": "TAX-BJ"},
                ]
            },
        }

        self.assertTrue(zen.evaluate_expression(expression, {**base, "buyerTaxNo": "TAX-BJ"}))
        self.assertFalse(zen.evaluate_expression(expression, {**base, "buyerTaxNo": "TAX-FJ"}))
        self.assertFalse(zen.evaluate_expression(expression, {**base, "buyerTaxNo": ""}))

    def test_invoice_org_number_cannot_override_receipt_company_tax(self) -> None:
        expression = _is_tax_exists_expression()
        context = {
            "buyerName": "北京星网锐捷网络技术有限公司",
            "buyerTaxNo": "91110108668444162H",
            "instanceComCode": "111",
            "orgNumber": "112",
            "serviceData": {
                "companyList": [
                    {"ccode": "111", "companyTax": "913500007549617646"},
                    {"ccode": "112", "companyTax": "91110108668444162H"},
                ]
            },
        }
        self.assertFalse(zen.evaluate_expression(expression, context))

    def test_employee_buyer_name_skips_tax_number_comparison(self) -> None:
        expression = _is_tax_exists_expression()
        context = {
            "buyerName": "张三",
            "buyerTaxNo": "WRONG-TAX",
            "instanceComCode": "112",
            "orgNumber": "111",
            "serviceData": {"companyList": [{"ccode": "112", "companyTax": "TAX-BJ"}]},
        }
        self.assertTrue(zen.evaluate_expression(expression, context))

    def test_graph_maps_e02_to_pass_and_reject(self) -> None:
        node = _build_tax_number_check_node()
        self.assertEqual(node["content"]["inputs"][0]["field"], "tax_check")
        self.assertEqual(node["content"]["outputPath"], "tax_result")
        results_by_input = {
            rule["dea9a1bc-66ae-47b3-885f-9e9a1bb07571"]: rule[
                "f35ede49-0eae-4dda-b39e-11a11383697a"
            ]
            for rule in node["content"]["rules"]
        }
        self.assertEqual(results_by_input["true"], '"PASS"')
        self.assertEqual(results_by_input["false"], '"REJECT"')
        self.assertTrue(
            all(
                rule["48a29115-f542-44d3-8c02-3ff71e19ee38"] == '"E02"'
                for rule in node["content"]["rules"]
            )
        )

    def test_generated_graph_connects_e02_to_response(self) -> None:
        graph = build_entertainment_graph()
        e02_id = "d13f7062-96e4-4d74-a552-dfcc60d98ff4"
        response_id = "e109e75a-d107-4fd0-a8b3-e3dae7fad15b"
        self.assertEqual(sum(node.get("id") == e02_id for node in graph["nodes"]), 1)
        self.assertTrue(
            any(
                edge.get("sourceId") == e02_id and edge.get("targetId") == response_id
                for edge in graph["edges"]
            )
        )


class GiftCountExpressionTests(unittest.TestCase):
    def test_compares_gift_reception_count_with_total_goods_count(self) -> None:
        expression = _is_gift_count_reasonable_expression()
        base = {"serviceData": {"entertainment_data": {"hasGiftItem": True, "giftReceptionCount": 10}}}

        self.assertFalse(zen.evaluate_expression(expression, {**base, "totalGoodsCount": 1}))
        self.assertTrue(zen.evaluate_expression(expression, {**base, "totalGoodsCount": 10}))
        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {**base, "totalGoodsCount": 1, "isLastInvoice": False},
            )
        )
        self.assertFalse(
            zen.evaluate_expression(
                expression,
                {**base, "totalGoodsCount": 1, "isLastInvoice": True},
            )
        )

    def test_non_gift_project_is_not_checked(self) -> None:
        expression = _is_gift_count_reasonable_expression()
        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "serviceData": {
                        "entertainment_data": {
                            "hasGiftItem": False,
                            "giftReceptionCount": 10,
                        }
                    },
                    "totalGoodsCount": 1,
                },
            )
        )

    def test_warning_rule_message_is_evaluable(self) -> None:
        graph = json.loads(DST_GRAPH.read_text(encoding="utf-8"))
        node = next(node for node in graph["nodes"] if node.get("id") == "ent-gift-count-check")
        warning_rule = next(
            rule
            for rule in node["content"]["rules"]
            if rule["dea9a1bc-66ae-47b3-885f-9e9a1bb07571"] == "false"
        )
        message_expression = warning_rule["509fd9ba-3996-4e4a-9021-df6513ed6807"]
        self.assertIn("全部发票购买商品数量", zen.evaluate_expression(
            message_expression,
            {
                "invoiceNo": "INV-1",
                "totalGoodsCount": 1,
                "items": [],
                "serviceData": {"entertainment_data": {"giftReceptionCount": 10}},
            },
        ))


class InvoiceNumberContinuityTests(unittest.TestCase):
    @staticmethod
    def _w34_data(*, applicable: bool = True, batch_hit: bool = False, history_numbers=None):
        return {
            "w34InvoiceSerial": {
                "isApplicable": applicable,
                "batchHit": batch_hit,
                "historyNumbers": history_numbers or [],
            }
        }

    def test_flags_same_receipt_invoice_numbers_with_difference_at_most_ten(self) -> None:
        expression = _is_invoice_number_continuous_expression()

        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "invoiceNo": "100010",
                    "previousW34InvoiceNumbers": ["100000"],
                    "serviceData": self._w34_data(),
                },
            )
        )
        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "invoiceNo": "100001",
                    "previousW34InvoiceNumbers": ["100000"],
                    "serviceData": self._w34_data(),
                },
            )
        )
        self.assertFalse(
            zen.evaluate_expression(
                expression,
                {
                    "invoiceNo": "100011",
                    "previousW34InvoiceNumbers": ["100000"],
                    "serviceData": self._w34_data(),
                },
            )
        )

    def test_flags_cross_receipt_history_numbers_with_difference_at_most_ten(self) -> None:
        expression = _is_invoice_number_continuous_expression()
        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "invoiceNo": "200010",
                    "serviceData": self._w34_data(history_numbers=["200000"]),
                },
            )
        )
        self.assertFalse(
            zen.evaluate_expression(
                expression,
                {
                    "invoiceNo": "200011",
                    "serviceData": self._w34_data(history_numbers=["200000"]),
                },
            )
        )

    def test_non_w34_invoice_type_is_not_checked(self) -> None:
        expression = _is_invoice_number_continuous_expression()
        self.assertFalse(
            zen.evaluate_expression(
                expression,
                {
                    "invoiceNo": "100001",
                    "previousW34InvoiceNumbers": ["100000"],
                    "serviceData": self._w34_data(applicable=False, batch_hit=True),
                },
            )
        )

    def test_first_or_missing_invoice_number_is_safe(self) -> None:
        expression = _is_invoice_number_continuous_expression()
        self.assertFalse(
            zen.evaluate_expression(
                expression,
                {"invoiceNo": "100000", "serviceData": self._w34_data()},
            )
        )
        self.assertFalse(
            zen.evaluate_expression(
                expression,
                {
                    "invoiceNo": "",
                    "previousW34InvoiceNumbers": ["100000"],
                    "serviceData": self._w34_data(),
                },
            )
        )

    def test_w34_warns_only_when_continuity_risk_is_true(self) -> None:
        node = _build_invoice_number_check_node()
        results_by_input = {
            rule["dea9a1bc-66ae-47b3-885f-9e9a1bb07571"]: rule[
                "f35ede49-0eae-4dda-b39e-11a11383697a"
            ]
            for rule in node["content"]["rules"]
        }
        self.assertEqual(results_by_input["true"], '"WARNING"')
        self.assertEqual(results_by_input["false"], '"PASS"')
        self.assertEqual(
            node["content"]["inputs"][0]["field"],
            "isInvoiceNumberContinuous",
        )


class ContentComplianceLlmTests(unittest.TestCase):
    def test_prompt_uses_project_name_semantics_not_keyword_matching(self) -> None:
        self.assertIn("JSON.stringify(goodsName)", ENTERTAINMENT_CONTENT_PROMPT_SOURCE)
        self.assertIn("黄金针菇", ENTERTAINMENT_CONTENT_PROMPT_SOURCE)
        self.assertIn("默认通过", ENTERTAINMENT_CONTENT_PROMPT_SOURCE)
        for prohibited_item in ("黄金", "珠宝", "首饰", "茅台", "五粮液", "礼品卡", "充值卡"):
            self.assertIn(prohibited_item, ENTERTAINMENT_CONTENT_PROMPT_SOURCE)

    def test_postprocess_maps_both_blacklist_categories_to_e36_results(self) -> None:
        node = _build_content_compliance_postprocess_node()
        expression = node["content"]["expressions"][0]["value"]

        self.assertEqual(zen.evaluate_expression(expression, {
            "llm_status": "success",
            "llm_result": {"passed": True, "violationType": "none"},
        }), "pass")
        self.assertEqual(zen.evaluate_expression(expression, {
            "llm_status": "success",
            "llm_result": {"passed": False, "violationType": "prohibited_item"},
        }), "prohibited_item")
        self.assertEqual(zen.evaluate_expression(expression, {
            "llm_status": "success",
            "llm_result": {"passed": False, "violationType": "recharge_card"},
        }), "recharge_card")

    def test_e36_contains_recharge_card_result_row(self) -> None:
        node = _build_content_compliance_check_node()
        rules = node["content"]["rules"]
        recharge_rule = next(
            rule for rule in rules
            if rule["dea9a1bc-66ae-47b3-885f-9e9a1bb07571"] == '"recharge_card"'
        )
        self.assertEqual(recharge_rule["48a29115-f542-44d3-8c02-3ff71e19ee38"], '"E36"')
        self.assertEqual(recharge_rule["f35ede49-0eae-4dda-b39e-11a11383697a"], '"REJECT"')


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
        e15_type = {"isApplicable": True}

        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {"serviceData": {"auditInfo": audit_info, "e15InvoiceType": e15_type}},
            )
        )
        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "passengerName": "",
                    "serviceData": {"auditInfo": audit_info, "e15InvoiceType": e15_type},
                },
            )
        )

    def test_passes_when_passenger_name_differs_from_verification_user(self) -> None:
        expression = _is_not_self_expense_expression()

        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "passengerName": "张三",
                    "serviceData": {
                        "auditInfo": {"verifiUserName": "刘雪涛"},
                        "e15InvoiceType": {"isApplicable": True},
                    },
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
                    "serviceData": {
                        "auditInfo": {"verifiUserName": "刘雪涛"},
                        "e15InvoiceType": {"isApplicable": True},
                    },
                },
            )
        )

    def test_skips_name_comparison_for_non_applicable_invoice_type(self) -> None:
        expression = _is_not_self_expense_expression()

        self.assertTrue(
            zen.evaluate_expression(
                expression,
                {
                    "passengerName": "刘雪涛",
                    "serviceData": {
                        "auditInfo": {"verifiUserName": "刘雪涛"},
                        "e15InvoiceType": {"isApplicable": False},
                    },
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
