import json
from pathlib import Path

from tools.codex.enable_max_reasoning import (
    BUNDLE_WITH_MAX,
    ensure_catalog,
    ensure_config,
    patch_webview,
)


def test_catalog_adds_max_and_keeps_provider_specific_levels(tmp_path: Path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "supported_reasoning_levels": [
                            {"effort": "none", "description": "Disable thinking"},
                            {"effort": "high", "description": "High"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert ensure_catalog(catalog, "gpt-5.6-luna", check=False)
    levels = [
        item["effort"]
        for item in json.loads(catalog.read_text(encoding="utf-8"))["models"][0][
            "supported_reasoning_levels"
        ]
    ]
    assert levels == ["none", "low", "medium", "high", "xhigh", "max"]
    assert not ensure_catalog(catalog, "gpt-5.6-luna", check=False)


def test_config_points_to_independent_catalog(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model_provider = "custom"\nmodel_catalog_json = "old.json"\n[projects."/tmp"]\n',
        encoding="utf-8",
    )

    assert ensure_config(config, "new.json", check=False)
    assert 'model_catalog_json = "new.json"' in config.read_text(encoding="utf-8")
    assert not ensure_config(config, "new.json", check=False)


def test_webview_patch_is_idempotent(tmp_path: Path):
    assets = tmp_path / "webview" / "assets"
    assets.mkdir(parents=True)
    bundle = assets / "app-initial-test.js"
    bundle.write_text("prefix i0e=[`low`,`medium`,`high`,`xhigh`] suffix", encoding="utf-8")

    patched, already = patch_webview(tmp_path, check=False)
    assert patched == [bundle]
    assert already == []
    assert BUNDLE_WITH_MAX in bundle.read_text(encoding="utf-8")
    assert bundle.with_name(bundle.name + ".codex-max.bak").exists()

    patched, already = patch_webview(tmp_path, check=False)
    assert patched == []
    assert already == [bundle]
