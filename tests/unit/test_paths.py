from __future__ import annotations

import json
import unittest
from pathlib import Path

from expense_audit_orchestrator.paths import (
    DEFAULT_GRAPH_PATH,
    DEFAULT_OCR_PATH,
    DEFAULT_OPERATOR_CITY_CSV_PATH,
    GRAPHS_ROOT,
    OFFICIAL_GRAPH_PATHS,
    PROJECT_ROOT,
    resolve_project_path,
)


class ProjectPathContractTests(unittest.TestCase):
    def test_official_graphs_are_present_and_valid_json(self) -> None:
        self.assertEqual(set(OFFICIAL_GRAPH_PATHS), {
            "telecom",
            "entertainment",
            "personal_transport",
            "travel",
        })
        for graph_path in OFFICIAL_GRAPH_PATHS.values():
            self.assertTrue(graph_path.is_file(), graph_path)
            self.assertEqual(graph_path.parent, GRAPHS_ROOT)
            payload = json.loads(graph_path.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, dict)

    def test_default_assets_are_present(self) -> None:
        for asset_path in (DEFAULT_OCR_PATH, DEFAULT_OPERATOR_CITY_CSV_PATH):
            self.assertTrue(asset_path.is_file(), asset_path)

    def test_default_graph_is_the_telecom_official_graph(self) -> None:
        self.assertEqual(DEFAULT_GRAPH_PATH, OFFICIAL_GRAPH_PATHS["telecom"])

    def test_relative_paths_are_project_relative(self) -> None:
        self.assertEqual(resolve_project_path("resources/samples/prepare_test.json"), DEFAULT_OCR_PATH)
        self.assertEqual(resolve_project_path("resources/graphs/graph-latest-travel-0807.json"), OFFICIAL_GRAPH_PATHS["travel"])
        self.assertEqual(resolve_project_path("graph-latest-travel-0807.json"), OFFICIAL_GRAPH_PATHS["travel"])
        self.assertEqual(resolve_project_path("/tmp/custom.json"), Path("/tmp/custom.json"))
        self.assertEqual(resolve_project_path(None, DEFAULT_GRAPH_PATH), DEFAULT_GRAPH_PATH)

    def test_project_root_contains_runtime_packages(self) -> None:
        self.assertTrue((PROJECT_ROOT / "expense_audit_orchestrator").is_dir())
        self.assertTrue((PROJECT_ROOT / "graph_runtime").is_dir())
        self.assertTrue((PROJECT_ROOT / "node_gateway").is_dir())
