import unittest
from typing import Any

from expense_audit_orchestrator.receipt_summary import (
    build_ai_audit_advice,
    build_ai_audit_summary_finance,
)
from expense_audit_orchestrator.writeback import assemble_result_audit_info


def _writeback_fixture(
    is_eor: Any = "0",
    *,
    include_is_eor: bool = True,
) -> tuple[dict, dict]:
    service_data = {
        "auditInfo": {
            "instanceCode": "REC-EOR-WRITEBACK-001",
            "applyAmount": 100,
            "isEor": is_eor,
        }
    }
    prepared = {
        "receiptCode": "REC-EOR-WRITEBACK-001",
        "serviceData": service_data,
        "invoicePreparations": [
            {
                "invoiceKey": "FID-EOR-001",
                "preparedInput": {
                    "invoiceNo": "INV-EOR-001",
                    "totalAmount": 40,
                    "serviceData": {
                        "currentInvoiceInfo": {"aiiid": "AIIID-EOR-001"},
                        "currentAuditInvoiceFile": {"afiid": "AFID-EOR-001"},
                    },
                },
            }
        ],
    }
    processed = {
        "receiptCode": "REC-EOR-WRITEBACK-001",
        "serviceData": service_data,
        "invoiceResults": [
            {
                "invoiceKey": "FID-EOR-001",
                "decisionOutput": {
                    "amount_result": {
                        "reason_code": "E31",
                        "distinguish_result": "REJECT",
                        "message": "金额不足",
                    }
                },
                "decisionStatus": "reject",
                "executionStatus": "SUCCEEDED",
            }
        ],
        "applyAmount": 100,
        "validInvoiceTotal": 40,
        "isAmountSufficient": False,
    }
    if not include_is_eor:
        service_data["auditInfo"].pop("isEor", None)
    return prepared, processed


class EorE31WritebackAndSummaryTests(unittest.TestCase):
    def test_eor_shortage_is_warning_in_writeback_but_non_eor_remains_reject(self) -> None:
        eor_prepared, eor_processed = _writeback_fixture("1")
        eor_payload = assemble_result_audit_info(
            eor_prepared,
            eor_processed,
            expense_profile="telecom",
        )
        self.assertEqual(eor_payload["auditLogs"][0]["reasonCode"], "E31")
        self.assertEqual(eor_payload["auditLogs"][0]["distinguishResult"], "warning")
        self.assertEqual(eor_payload["auditInvoiceInfos"][0]["reasonCode"], "E31")
        self.assertNotIn("isEor", eor_payload)

        normal_prepared, normal_processed = _writeback_fixture("0")
        normal_payload = assemble_result_audit_info(
            normal_prepared,
            normal_processed,
            expense_profile="telecom",
        )
        self.assertEqual(normal_payload["auditLogs"][0]["distinguishResult"], "reject")
        self.assertNotIn("isEor", normal_payload)

    def test_is_eor_values_drive_e31_without_entering_writeback_dto(self) -> None:
        for profile, value, expected in (
            ("telecom", True, "1"),
            ("personal_transport", False, "0"),
            ("entertainment", "true", "1"),
        ):
            with self.subTest(profile=profile, value=value):
                prepared, processed = _writeback_fixture(value)
                payload = assemble_result_audit_info(
                    prepared,
                    processed,
                    expense_profile=profile,
                )
                self.assertNotIn("isEor", payload)
                self.assertEqual(
                    payload["auditLogs"][0]["distinguishResult"],
                    "warning" if expected == "1" else "reject",
                )

    def test_is_eor_legacy_key_aliases_are_normalized(self) -> None:
        for profile, key in (
            ("telecom", "IsEor"),
            ("personal_transport", "isEOR"),
            ("entertainment", "is_eor"),
        ):
            with self.subTest(profile=profile, key=key):
                prepared, processed = _writeback_fixture("0")
                audit_info = prepared["serviceData"]["auditInfo"]
                audit_info.pop("isEor")
                audit_info[key] = True
                payload = assemble_result_audit_info(
                    prepared,
                    processed,
                    expense_profile=profile,
                )
                self.assertNotIn("isEor", payload)
                self.assertEqual(payload["auditLogs"][0]["distinguishResult"], "warning")

    def test_missing_is_eor_defaults_to_non_eor_for_all_supported_profiles(self) -> None:
        for profile in ("telecom", "personal_transport", "entertainment"):
            with self.subTest(profile=profile):
                prepared, processed = _writeback_fixture(include_is_eor=False)
                payload = assemble_result_audit_info(
                    prepared,
                    processed,
                    expense_profile=profile,
                )
                self.assertNotIn("isEor", payload)
                self.assertEqual(
                    payload["auditLogs"][0]["distinguishResult"],
                    "reject",
                )

    def test_eor_e31_warning_behavior_applies_to_telecom_and_personal_transport(self) -> None:
        for profile in ("telecom", "personal_transport", "entertainment"):
            with self.subTest(profile=profile):
                prepared, processed = _writeback_fixture("1")
                payload = assemble_result_audit_info(
                    prepared,
                    processed,
                    expense_profile=profile,
                )
                self.assertNotIn("isEor", payload)
                self.assertEqual(payload["auditLogs"][0]["distinguishResult"], "warning")

    def test_eor_e31_is_high_risk_and_overall_advice_is_warning(self) -> None:
        prepared, processed = _writeback_fixture("1")
        summary = build_ai_audit_summary_finance(
            prepared,
            processed,
            audit_logs=[{"reasonCode": "E31", "distinguishResult": "WARNING"}],
            audit_risk_catalog={"E31": {"riskLevel": "blocking"}},
            expense_profile="telecom",
        )
        self.assertEqual(
            summary,
            "本单高风险 1 项、中低风险 0 项，阻断 0 项，已通过 0 项稽核项。",
        )
        advice = build_ai_audit_advice(
            prepared,
            processed,
            expense_profile="telecom",
        )
        self.assertIn("WARNING稽核项", advice or "")
        self.assertNotIn("REJECT稽核项", advice or "")
