import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from expense_audit_orchestrator.overall_advice import (
    DEFAULT_NODE_GATEWAY_URL,
    LlmOverallAdviceProvider,
    NoopOverallAdviceProvider,
    _build_problems_digest,
    create_overall_advice_provider_from_env,
)

PROMPT_DIR = Path(__file__).resolve().parent / "expense_audit_orchestrator" / "prompts"


def _make_provider(handler, *, model="gpt-4o-mini", prompt_dir=PROMPT_DIR, timeout=5.0):
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    return LlmOverallAdviceProvider(
        node_gateway_url="http://ng.local",
        model=model,
        prompt_dir=prompt_dir,
        client_factory=factory,
    )


def _invoice(key, rule_results, *, decision_status="reject"):
    decision_output = {}
    for name, rr in rule_results.items():
        decision_output[name] = rr
    return {
        "invoiceKey": key,
        "decisionOutput": decision_output,
        "decisionStatus": decision_status,
        "executionStatus": "SUCCEEDED",
    }


def _rule(reason_code, status, *, message="m", suggestion="s"):
    return {
        "reason_code": reason_code,
        "distinguish_result": status,
        "audit_content": "c",
        "audit_type": "general-rules",
        "message": message,
        "employeeSuggestionTips": suggestion,
    }


class ProblemsDigestTests(unittest.TestCase):
    def test_digest_includes_reject_and_warning_skips_pass(self):
        invoice_results = [
            _invoice(
                "INV-1",
                {
                    "amount_result": _rule("E31", "REJECT", message="金额不足"),
                    "header_result": _rule("E01", "PASS"),
                },
            ),
            _invoice("INV-2", {"phone_result": _rule("W32", "WARNING", message="手机号不匹配")}),
        ]
        digest = _build_problems_digest(invoice_results)
        self.assertIn("E31", digest)
        self.assertIn("INV-1", digest)
        self.assertIn("W32", digest)
        self.assertIn("INV-2", digest)
        self.assertNotIn("E01", digest)  # PASS skipped
        self.assertIn("建议: s", digest)

    def test_digest_empty_when_all_pass(self):
        invoice_results = [_invoice("INV-1", {"header_result": _rule("E01", "PASS")})]
        self.assertEqual(_build_problems_digest(invoice_results), "")

    def test_digest_empty_when_no_invoices(self):
        self.assertEqual(_build_problems_digest([]), "")


class LlmOverallAdviceProviderTests(unittest.TestCase):
    def test_success_returns_suggestion_and_interpolates_prompt(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read().decode("utf-8")
            return httpx.Response(
                200,
                json={
                    "llmStatus": "success",
                    "llmResult": {"aiAuditAdvice": "本核销单建议补传合规发票"},
                    "rawContent": None,
                    "errorMessage": None,
                },
            )

        provider = _make_provider(handler)
        invoice_results = [_invoice("INV-1", {"r": _rule("E05", "REJECT")})]
        suggestion = provider("REC-001", invoice_results)
        self.assertEqual(suggestion, "本核销单建议补传合规发票")
        # prompt placeholders were interpolated
        self.assertIn("REC-001", captured["body"])
        self.assertIn("E05", captured["body"])
        self.assertNotIn("{{", captured["body"])

    def test_llm_status_error_returns_none(self):
        def handler(request):
            return httpx.Response(200, json={"llmStatus": "error", "errorMessage": "missing key", "llmResult": None})

        provider = _make_provider(handler)
        self.assertIsNone(provider("REC-001", [_invoice("INV-1", {"r": _rule("E05", "REJECT")})]))

    def test_llm_result_without_suggestion_returns_none(self):
        def handler(request):
            return httpx.Response(200, json={"llmStatus": "success", "llmResult": {"foo": "bar"}})

        provider = _make_provider(handler)
        self.assertIsNone(provider("REC-001", [_invoice("INV-1", {"r": _rule("E05", "REJECT")})]))

    def test_falls_back_to_raw_content_json(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "llmStatus": "success",
                    "llmResult": None,
                    "rawContent": '```json\n{"aiAuditAdvice": "回退建议"}\n```',
                },
            )

        provider = _make_provider(handler)
        self.assertEqual(provider("REC-001", [_invoice("INV-1", {"r": _rule("E05", "REJECT")})]), "回退建议")

    def test_http_500_returns_none(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        provider = _make_provider(handler)
        self.assertIsNone(provider("REC-001", [_invoice("INV-1", {"r": _rule("E05", "REJECT")})]))

    def test_transport_error_returns_none(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        provider = _make_provider(handler)
        self.assertIsNone(provider("REC-001", [_invoice("INV-1", {"r": _rule("E05", "REJECT")})]))

    def test_all_pass_still_calls_llm(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"llmStatus": "success", "llmResult": {"aiAuditAdvice": "均合规"}})

        provider = _make_provider(handler)
        suggestion = provider("REC-001", [_invoice("INV-1", {"r": _rule("E01", "PASS")})])
        self.assertEqual(suggestion, "均合规")
        self.assertEqual(calls["n"], 1)

    def test_noop_returns_none(self):
        self.assertIsNone(NoopOverallAdviceProvider()("REC-001", []))


class EnvFactoryTests(unittest.TestCase):
    def test_disabled_returns_noop(self):
        with patch.dict(os.environ, {"OVERALL_ADVICE_ENABLED": "false"}, clear=False):
            provider = create_overall_advice_provider_from_env()
            self.assertIsInstance(provider, NoopOverallAdviceProvider)

    def test_enabled_returns_llm_with_url(self):
        with patch.dict(
            os.environ,
            {"OVERALL_ADVICE_ENABLED": "true", "NODE_GATEWAY_URL": "http://example:9999"},
            clear=False,
        ):
            provider = create_overall_advice_provider_from_env()
            self.assertIsInstance(provider, LlmOverallAdviceProvider)
            self.assertTrue(provider._endpoint.startswith("http://example:9999"))

    def test_defaults_when_unset(self):
        env = {"OVERALL_ADVICE_ENABLED": "true"}
        # ensure NODE_GATEWAY_URL not present
        with patch.dict(os.environ, env, clear=True):
            provider = create_overall_advice_provider_from_env()
            self.assertIsInstance(provider, LlmOverallAdviceProvider)
            self.assertTrue(provider._endpoint.startswith(DEFAULT_NODE_GATEWAY_URL))


if __name__ == "__main__":
    unittest.main()
