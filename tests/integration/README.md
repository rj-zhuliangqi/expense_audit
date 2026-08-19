# Integration tests

Live-service and manual integration diagnostics live under `apps/diagnostics/`.
The regular test suite intentionally excludes those scripts so that `pytest -q`
remains offline and deterministic.
