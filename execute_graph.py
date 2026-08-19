"""Compatibility launcher for :mod:`apps.cli.execute_graph`."""
from __future__ import annotations

from apps.cli.execute_graph import *  # noqa: F401,F403
from apps.cli.execute_graph import main_cli


if __name__ == "__main__":
    raise SystemExit(main_cli())
