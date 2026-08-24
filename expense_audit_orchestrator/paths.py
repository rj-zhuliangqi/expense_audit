"""Canonical project paths and asset resolution.

Runtime code should import paths from this module instead of deriving paths from
individual module locations.  The four official graph files live under
``resources/graphs``.  Their filenames remain stable because they are part of
the deployment and workflow contract.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROJECT_ROOT  # Backwards-compatible alias used by existing integrations.

RESOURCES_ROOT = PROJECT_ROOT / "resources"
GRAPHS_ROOT = RESOURCES_ROOT / "graphs"
SAMPLES_ROOT = RESOURCES_ROOT / "samples"
REFERENCE_ROOT = RESOURCES_ROOT / "reference"
PROMPTS_ROOT = PROJECT_ROOT / "expense_audit_orchestrator" / "prompts"

OFFICIAL_GRAPH_PATHS: dict[str, Path] = {
    "telecom": GRAPHS_ROOT / "graph-latest-telecom-0727-1900.json",
    "entertainment": GRAPHS_ROOT / "graph-latest-entertainment-0722.json",
    "personal_transport": GRAPHS_ROOT / "graph-latest-personal-transport-0722.json",
    "travel": GRAPHS_ROOT / "graph-latest-travel-0807.json",
}

DEFAULT_GRAPH_PATH = OFFICIAL_GRAPH_PATHS["telecom"]
DEFAULT_OCR_PATH = SAMPLES_ROOT / "prepare_test.json"
DEFAULT_OPERATOR_CITY_CSV_PATH = REFERENCE_ROOT / "operator_city.csv"

# Existing .env files and operator commands may still refer to a graph by its
# former root-level filename.  Resolve those names to the archived asset rather
# than requiring a flag/configuration change during this layout-only migration.
_OFFICIAL_GRAPH_BY_BASENAME = {path.name: path for path in OFFICIAL_GRAPH_PATHS.values()}

# Rule-source files are often supplied locally and may contain business data.
# Keep their legacy locations as optional candidates, but never silently invent
# a missing source path when running a builder in a clean checkout.
TRAVEL_RULE_SOURCE = REFERENCE_ROOT / "travel_rules.csv"
# Backwards-compatible export name used by the travel builder.
LEGACY_TRAVEL_RULE_SOURCE = TRAVEL_RULE_SOURCE
LEGACY_ENTERTAINMENT_RULE_SOURCE = PROJECT_ROOT / "业务招待费"
LEGACY_PERSONAL_TRANSPORT_RULE_SOURCE = PROJECT_ROOT / "交通费"


def resolve_project_path(
    value: str | os.PathLike[str] | None,
    default: Path | None = None,
) -> Path | None:
    """Resolve an optional path, treating relative values as project-relative."""
    if value is None or not str(value).strip():
        return default
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate

    project_relative = PROJECT_ROOT / candidate
    if candidate.parent == Path(".") and candidate.name in _OFFICIAL_GRAPH_BY_BASENAME:
        return _OFFICIAL_GRAPH_BY_BASENAME[candidate.name]
    return project_relative


def resolve_env_path(env_key: str, default: Path | None = None) -> Path | None:
    """Resolve a project-relative path from an environment variable."""
    return resolve_project_path(os.getenv(env_key), default)


def require_path(path: Path | str, description: str) -> Path:
    """Return an existing path or raise an actionable configuration error."""
    resolved = Path(path)
    if not resolved.exists():
        raise FileNotFoundError(
            f"{description} not found: {resolved}. "
            "Provide the correct path explicitly or restore the required project asset."
        )
    return resolved
