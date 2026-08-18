"""Compatibility launcher for the manual integration diagnostic."""
from __future__ import annotations

from apps.diagnostics.integration_test import *  # noqa: F401,F403
from apps.diagnostics.integration_test import main


if __name__ == "__main__":
    main()
