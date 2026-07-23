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

    def test_travel_profile_registered_but_enricher_deferred(self) -> None:
        # travel profile（差旅）已注册到注册表，但 enricher 实现仍 deferred
        profile = get_profile("travel")
        self.assertEqual(profile.name, "travel")
        enricher = profile.receipt_enrichers["travel_data"]
        with self.assertRaises(NotImplementedError):
            enricher("R", {})

    def test_personal_transport_profile_registered(self) -> None:
        # personal_transport profile（个人交通费）已注册，图路径指向个人交通费图
        from expense_audit_orchestrator.profiles import PERSONAL_TRANSPORT_GRAPH_PATH

        profile = get_profile("personal_transport")
        self.assertEqual(profile.name, "personal_transport")
        self.assertEqual(profile.default_graph_path, PERSONAL_TRANSPORT_GRAPH_PATH)
        enricher = profile.receipt_enrichers["personal_transport_data"]
        # 个人交通费 enricher 当前返回空 dict（无专属数据）
        self.assertEqual(enricher("R", {}), {})

    def test_graph_path_env_override(self) -> None:
        """图路径支持通过 .env 环境变量覆盖，无需改代码。"""
        import importlib

        from expense_audit_orchestrator import profiles as profiles_mod

        env_overrides = {
            "TELECOM_GRAPH_PATH": "/env/telecom.json",
            "PERSONAL_TRANSPORT_GRAPH_PATH": "/env/personal_transport.json",
            "TRAVEL_GRAPH_PATH": "/env/travel.json",
            "ENTERTAINMENT_GRAPH_PATH": "/env/entertainment.json",
        }
        with mock.patch.dict(os.environ, env_overrides, clear=False):
            importlib.reload(profiles_mod)
            try:
                self.assertEqual(
                    str(profiles_mod.PERSONAL_TRANSPORT_GRAPH_PATH),
                    "/env/personal_transport.json",
                )
                self.assertEqual(
                    str(profiles_mod.TRAVEL_GRAPH_PATH),
                    "/env/travel.json",
                )
                self.assertEqual(
                    str(profiles_mod.ENTERTAINMENT_GRAPH_PATH),
                    "/env/entertainment.json",
                )
                # telecom profile 的图路径也应被 env 覆盖
                telecom = profiles_mod.get_profile("telecom")
                self.assertEqual(str(telecom.default_graph_path), "/env/telecom.json")
            finally:
                # 恢复模块默认状态，避免污染后续测试
                for k in env_overrides:
                    os.environ.pop(k, None)
                importlib.reload(profiles_mod)

    def test_entertainment_profile_registered(self) -> None:
        profile = get_profile("entertainment")
        self.assertEqual(profile.name, "entertainment")
        enricher = profile.receipt_enrichers["entertainment_data"]
        # 招待费 enricher 当前返回空 dict（无专属数据）
        self.assertEqual(enricher("R", {}), {})

    def test_unknown_profile_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            get_profile("nonexistent")


class ProfileResolverTests(unittest.TestCase):
    def test_resolve_known_ei_code(self) -> None:
        from expense_audit_orchestrator.profiles import ProfileResolver

        resolver = ProfileResolver(ei_code_map={"EI001": "telecom"})
        profile = resolver.resolve("EI001")
        self.assertEqual(profile.name, "telecom")

    def test_resolve_unknown_ei_code_raises(self) -> None:
        from expense_audit_orchestrator.profiles import (
            ProfileResolver,
            UnknownExpenseTypeError,
        )

        resolver = ProfileResolver(ei_code_map={"EI001": "telecom"})
        with self.assertRaises(UnknownExpenseTypeError):
            resolver.resolve("EI999")

    def test_resolve_empty_ei_code_raises(self) -> None:
        from expense_audit_orchestrator.profiles import (
            ProfileResolver,
            UnknownExpenseTypeError,
        )

        resolver = ProfileResolver(ei_code_map={"EI001": "telecom"})
        with self.assertRaises(UnknownExpenseTypeError):
            resolver.resolve(None)
        with self.assertRaises(UnknownExpenseTypeError):
            resolver.resolve("")

    def test_from_map_file_loads_json(self) -> None:
        import json
        import tempfile

        from expense_audit_orchestrator.profiles import ProfileResolver

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump({"_comment": "test", "EI001": "telecom", "EI002": "travel"}, fh)
            map_path = fh.name

        try:
            resolver = ProfileResolver.from_map_file(map_path)
            self.assertEqual(resolver.ei_code_map["EI001"], "telecom")
            self.assertEqual(resolver.ei_code_map["EI002"], "travel")
            # _comment 键应被忽略
            self.assertNotIn("_comment", resolver.ei_code_map)
        finally:
            os.unlink(map_path)

    def test_create_from_env_uses_default_file(self) -> None:
        from expense_audit_orchestrator.profiles import (
            DEFAULT_EI_CODE_MAP_PATH,
            create_profile_resolver_from_env,
        )

        env = {k: v for k, v in os.environ.items() if k != "EI_CODE_MAP_PATH"}
        with mock.patch.dict(os.environ, env, clear=True):
            resolver = create_profile_resolver_from_env()
        # 默认映射文件应包含 telecom 映射
        self.assertIn("EI001", resolver.ei_code_map)
        self.assertEqual(resolver.ei_code_map["EI001"], "telecom")
        # 默认路径应指向包内文件
        self.assertTrue(DEFAULT_EI_CODE_MAP_PATH.exists())


if __name__ == "__main__":
    unittest.main()
