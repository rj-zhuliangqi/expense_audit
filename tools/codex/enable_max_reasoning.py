#!/usr/bin/env python3
"""Enable the ``max`` reasoning effort for the local Codex CLI and VS Code plugin.

The Codex CLI gets its choices from the configured model catalog.  The current
Codex VS Code webview also keeps a separate default allow-list and filters the
model/list response through it.  This script repairs both layers and is safe
to run repeatedly after cc-switch or a VS Code extension upgrade.

This is intentionally a local-environment tool.  It does not commit files from
~/.codex or ~/.vscode-server; those paths are patched when the script runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")
DESCRIPTIONS = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
    "max": "Maximum reasoning depth for the hardest problems",
}
BUNDLE_DEFAULT = "i0e=[`low`,`medium`,`high`,`xhigh`]"
BUNDLE_WITH_MAX = "i0e=[`low`,`medium`,`high`,`xhigh`,`max`]"


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home()))).expanduser()


def _default_extension_dir() -> Path | None:
    roots = [
        _home() / ".vscode-server" / "extensions",
        _home() / ".vscode" / "extensions",
    ]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(root.glob("openai.chatgpt-*-linux-x64"))
        candidates.extend(root.glob("openai.chatgpt-*-linux-arm64"))
        candidates.extend(root.glob("openai.chatgpt-*"))
    candidates = [p for p in candidates if p.is_dir()]
    if not candidates:
        return None
    return sorted(set(candidates), key=lambda p: (p.name, p.stat().st_mtime))[-1]


def _catalog_model(catalog: dict[str, Any], model_name: str) -> dict[str, Any]:
    for model in catalog.get("models", []):
        if isinstance(model, dict) and (
            model.get("slug") == model_name or model.get("model") == model_name
        ):
            return model
    raise ValueError(f"model {model_name!r} was not found in the catalog")


def ensure_catalog(path: Path, model_name: str, check: bool) -> bool:
    if not path.exists():
        raise FileNotFoundError(path)
    catalog = json.loads(path.read_text(encoding="utf-8"))
    model = _catalog_model(catalog, model_name)
    current = model.get("supported_reasoning_levels")
    if not isinstance(current, list):
        current = []

    by_effort: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for entry in current:
        if not isinstance(entry, dict) or not isinstance(entry.get("effort"), str):
            continue
        effort = entry["effort"]
        if effort in REASONING_LEVELS:
            by_effort[effort] = entry
        else:
            # Keep provider-specific levels such as ``none`` intact.
            extras.append(entry)
    desired = list(extras)
    for effort in REASONING_LEVELS:
        entry = dict(by_effort.get(effort, {}))
        entry["effort"] = effort
        entry.setdefault("description", DESCRIPTIONS[effort])
        desired.append(entry)

    changed = current != desired
    if changed and not check:
        model["supported_reasoning_levels"] = desired
        path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def _replace_root_toml_value(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    replacement = f'{key} = "{value}"'
    result: list[str] = []
    found = False
    for line in lines:
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            if not found:
                result.append(replacement)
                found = True
            continue
        result.append(line)
    if not found:
        insert_at = next((i for i, line in enumerate(result) if line.startswith("[")), len(result))
        result.insert(insert_at, replacement)
    return "\n".join(result).rstrip() + "\n"


def ensure_config(path: Path, catalog_name: str, check: bool) -> bool:
    if not path.exists():
        raise FileNotFoundError(path)
    original = path.read_text(encoding="utf-8")
    updated = _replace_root_toml_value(original, "model_catalog_json", catalog_name)
    changed = original != updated
    if changed and not check:
        path.write_text(updated, encoding="utf-8")
    return changed


def _backup(path: Path) -> Path:
    backup = path.with_name(path.name + ".codex-max.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def patch_webview(extension_dir: Path, check: bool) -> tuple[list[Path], list[Path]]:
    assets = extension_dir / "webview" / "assets"
    if not assets.is_dir():
        raise FileNotFoundError(assets)
    patched: list[Path] = []
    already: list[Path] = []
    found_bundle = False
    for path in sorted(assets.glob("app-initial-*.js")):
        text = path.read_text(encoding="utf-8")
        if BUNDLE_DEFAULT in text:
            found_bundle = True
            if not check:
                _backup(path)
                path.write_text(text.replace(BUNDLE_DEFAULT, BUNDLE_WITH_MAX, 1), encoding="utf-8")
            patched.append(path)
        elif BUNDLE_WITH_MAX in text:
            found_bundle = True
            already.append(path)
    if not found_bundle:
        raise RuntimeError(
            "no Codex webview bundle with the known reasoning allow-list was found; "
            "the extension may have changed and needs a new patch pattern"
        )
    return patched, already


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-5.6-luna", help="catalog model to expose at max")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=_home() / ".codex" / "aifault-codex-model-catalog.json",
        help="Codex model catalog JSON",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_home() / ".codex" / "config.toml",
        help="Codex config.toml",
    )
    parser.add_argument(
        "--extension-dir",
        type=Path,
        default=None,
        help="VS Code Codex extension directory (auto-detected when omitted)",
    )
    parser.add_argument("--check", action="store_true", help="report whether a repair is needed without writing")
    args = parser.parse_args(argv)

    catalog_name = args.catalog.name
    catalog_changed = ensure_catalog(args.catalog, args.model, args.check)
    config_changed = ensure_config(args.config, catalog_name, args.check)

    extension_dir = args.extension_dir or _default_extension_dir()
    if extension_dir is None:
        raise FileNotFoundError("an OpenAI Codex VS Code extension was not found")
    extension_dir = extension_dir.expanduser().resolve()
    patched, already = patch_webview(extension_dir, args.check)

    verb = "would repair" if args.check else "repaired"
    print(f"{verb} CLI catalog: {args.catalog} ({'changed' if catalog_changed else 'ok'})")
    print(f"{verb} CLI config:  {args.config} ({'changed' if config_changed else 'ok'})")
    if patched:
        print(f"{verb} plugin bundle(s): {len(patched)}")
        for path in patched:
            print(f"  - {path}")
    else:
        print(f"plugin bundle(s) already include max: {len(already)}")
    if not args.check:
        print("Reload VS Code / restart the Codex extension before checking the picker.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
