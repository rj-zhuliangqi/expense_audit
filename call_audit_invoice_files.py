"""Compatibility launcher for the invoice-file diagnostic tool."""
from __future__ import annotations

from apps.diagnostics.call_audit_invoice_files import *  # noqa: F401,F403
from apps.diagnostics.call_audit_invoice_files import main_cli


if __name__ == "__main__":
    raise SystemExit(main_cli())
