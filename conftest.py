"""Pytest configuration.

`integration_test.py` is a manually-run CLI smoke script (it calls live audit
services), not a unit-test module. Its step helpers happen to be prefixed with
`test_`, so pytest would otherwise try to collect them and fail at setup
looking for fixtures like `fid`/`receipt_code`. Exclude it from collection.

Run the unit suite via pytest or, as documented in readme.md, via unittest:

    .venv/bin/python -m unittest test_execute_graph.py test_main.py -v
"""

collect_ignore = ["integration_test.py"]
