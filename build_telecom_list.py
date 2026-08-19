"""Compatibility import/launcher for the telecom reference-data builder."""
from __future__ import annotations

from apps.builders import telecom_list as _implementation

for _name, _value in vars(_implementation).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


if __name__ == "__main__":
    _implementation.main()
