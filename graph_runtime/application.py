from typing import Any, Protocol


class DecisionEngine(Protocol):
    def evaluate(
        self,
        rule_input: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


def normalize_decision_output(decision_output: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(decision_output)
    raw_status = normalized.get("checkStatus")
    if isinstance(raw_status, str) and raw_status:
        normalized["checkStatus"] = _normalize_top_level_status(raw_status)
        return normalized

    rule_statuses = [
        status
        for status in (
            _normalize_rule_status(rule_result.get("distinguish_result"))
            for rule_result in _iter_rule_results(normalized)
        )
        if status is not None
    ]
    if any(status == "failed" for status in rule_statuses):
        normalized["checkStatus"] = "failed"
    elif any(status == "reject" for status in rule_statuses):
        normalized["checkStatus"] = "reject"
    elif any(status == "warning" for status in rule_statuses):
        normalized["checkStatus"] = "warning"
    elif rule_statuses and all(status == "passed" for status in rule_statuses):
        normalized["checkStatus"] = "passed"
    else:
        status_fields = [
            str(value).lower()
            for key, value in normalized.items()
            if key.endswith("_status") and isinstance(value, str)
        ]
        if any(value == "failed" for value in status_fields):
            normalized["checkStatus"] = "failed"
        elif any(value in {"warning", "warn"} for value in status_fields):
            normalized["checkStatus"] = "warning"
        elif status_fields and all(value == "passed" for value in status_fields):
            normalized["checkStatus"] = "passed"
        else:
            normalized["checkStatus"] = "unknown"

    primary_rule = _select_primary_rule_result(normalized)
    if primary_rule:
        normalized.setdefault("message", primary_rule.get("message"))
        normalized.setdefault("reasonCode", primary_rule.get("reason_code"))

    return normalized


def _iter_rule_results(decision_output: dict[str, Any]) -> list[dict[str, Any]]:
    rule_results: list[dict[str, Any]] = []
    for value in decision_output.values():
        if isinstance(value, dict) and _is_rule_result(value):
            rule_results.append(dict(value))
    return rule_results


def _is_rule_result(value: dict[str, Any]) -> bool:
    return any(
        key in value
        for key in (
            "distinguish_result",
            "reason_code",
            "audit_content",
            "audit_type",
        )
    )


def _normalize_top_level_status(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"pass", "passed"}:
        return "passed"
    if normalized in {"warn", "warning"}:
        return "warning"
    if normalized in {"fail", "failed"}:
        return "failed"
    if normalized == "reject":
        return "reject"
    return normalized


def _normalize_rule_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()
    if normalized in {"pass", "passed"}:
        return "passed"
    if normalized in {"warn", "warning"}:
        return "warning"
    if normalized in {"fail", "failed"}:
        return "failed"
    if normalized == "reject":
        return "reject"
    return normalized or None


def _select_primary_rule_result(decision_output: dict[str, Any]) -> dict[str, Any] | None:
    rule_results = _iter_rule_results(decision_output)
    for candidate_status in ("failed", "reject", "warning"):
        for rule_result in rule_results:
            if _normalize_rule_status(rule_result.get("distinguish_result")) == candidate_status:
                return rule_result
    if rule_results:
        return rule_results[0]
    return None


def _extract_receipt_code(prepared_input: dict[str, Any]) -> str | None:
    receipt = prepared_input.get("receipt")
    if not isinstance(receipt, dict):
        return None

    receipt_code = receipt.get("code")
    if isinstance(receipt_code, str) and receipt_code.strip():
        return receipt_code

    return None


def evaluate_prepared_input(
    decision_engine: DecisionEngine,
    prepared_input: dict[str, Any],
    *,
    receipt_code: str | None = None,
    trace: bool = True,
    max_depth: int | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {"trace": trace}
    if max_depth is not None:
        options["max_depth"] = max_depth
    rule_result = decision_engine.evaluate(prepared_input, options)
    decision_output = normalize_decision_output(rule_result.get("result", {}))

    return {
        "receiptCode": receipt_code or _extract_receipt_code(prepared_input),
        "checkStatus": decision_output.get("checkStatus", "unknown"),
        "message": decision_output.get("message"),
        "decisionOutput": decision_output,
        "preparedInput": prepared_input,
        "ruleInput": prepared_input,
        "trace": rule_result.get("trace"),
        "performance": rule_result.get("performance"),
    }