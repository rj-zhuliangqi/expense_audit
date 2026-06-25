import os
import unittest
from pathlib import Path
from unittest import mock

from expense_audit_orchestrator.profiles import get_profile
from expense_audit_orchestrator.profiles.telecom.data import (
    DEFAULT_OPERATOR_CITY_CSV_PATH,
    resolve_telecom_csv_path,
)


class ResolveTelecomCsvPathTests(unittest.TestCase):
    def test_explicit_asset_dir_wins(self) -> None:
        with mock.patch.dict(os.environ, {"TELECOM_OPERATOR_CITY_CSV": "/env/override.csv"}, clear=False):
            resolved = resolve_telecom_csv_path(asset_dir="/tmp/x")
        self.assertEqual(resolved, Path("/tmp/x/operator_city.csv"))

    def test_env_used_when_no_asset_dir(self) -> None:
        with mock.patch.dict(os.environ, {"TELECOM_OPERATOR_CITY_CSV": "/env/override.csv"}, clear=False):
            resolved = resolve_telecom_csv_path()
        self.assertEqual(resolved, Path("/env/override.csv"))

    def test_default_when_neither_given(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "TELECOM_OPERATOR_CITY_CSV"}
        with mock.patch.dict(os.environ, env, clear=True):
            resolved = resolve_telecom_csv_path()
        self.assertEqual(resolved, DEFAULT_OPERATOR_CITY_CSV_PATH)


class GetProfileTelecomAssetDirTests(unittest.TestCase):
    def test_asset_dir_override_loads_csv_from_dir(self) -> None:
        import csv
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            asset_dir = Path(tmp)
            csv_path = asset_dir / "operator_city.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["运营商", "城市"])
                writer.writerow(["测试运营商", "测试城市"])

            profile = get_profile("telecom", telecom_asset_dir=asset_dir)

        enricher = profile.receipt_enrichers["telecom_list"]
        telecom_list = enricher("R", {})
        self.assertIn(["测试运营商", "测试城市"], telecom_list)

    def test_no_asset_dir_uses_cached_default_profile(self) -> None:
        profile = get_profile("telecom")
        self.assertEqual(profile.name, "telecom")
        enricher = profile.receipt_enrichers["telecom_list"]
        self.assertIsInstance(enricher("R", {}), list)

    def test_travel_profile_not_implemented(self) -> None:
        with self.assertRaises(NotImplementedError):
            get_profile("travel")

    def test_unknown_profile_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            get_profile("nonexistent")


if __name__ == "__main__":
    unittest.main()
