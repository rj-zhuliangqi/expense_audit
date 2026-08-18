"""Compatibility launcher for :mod:`apps.cli.prepare_from_queue`."""
from __future__ import annotations

from apps.cli.prepare_from_queue import *  # noqa: F401,F403
from apps.cli.prepare_from_queue import main_cli


if __name__ == "__main__":
    raise SystemExit(main_cli())
