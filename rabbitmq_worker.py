"""Compatibility launcher for :mod:`apps.workers.rabbitmq_worker`."""
from __future__ import annotations

from apps.workers.rabbitmq_worker import *  # noqa: F401,F403
from apps.workers.rabbitmq_worker import main_cli


if __name__ == "__main__":
    raise SystemExit(main_cli())
