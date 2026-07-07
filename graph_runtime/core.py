import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import Any

import zen


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GRAPH_PATH = ROOT / "graph-latest-0625-1814.json"

_COMPILED_GRAPH_CACHE: OrderedDict[str, zen.ZenDecision] = OrderedDict()
_COMPILED_GRAPH_CACHE_LOCK = RLock()
_MAX_COMPILED_GRAPH_CACHE_SIZE = 32


def _cache_decision(cache_key: str, content: str) -> zen.ZenDecision:
    with _COMPILED_GRAPH_CACHE_LOCK:
        cached = _COMPILED_GRAPH_CACHE.get(cache_key)
        if cached is not None:
            _COMPILED_GRAPH_CACHE.move_to_end(cache_key)
            return cached

        engine = zen.ZenEngine()
        decision = engine.create_decision(content)
        _COMPILED_GRAPH_CACHE[cache_key] = decision
        if len(_COMPILED_GRAPH_CACHE) > _MAX_COMPILED_GRAPH_CACHE_SIZE:
            _COMPILED_GRAPH_CACHE.popitem(last=False)
        return decision


def _normalize_graph_content(graph_content: Mapping[str, object] | str) -> str:
    if isinstance(graph_content, str):
        return graph_content

    return json.dumps(graph_content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_decision(graph_path: Path | str = DEFAULT_GRAPH_PATH) -> zen.ZenDecision:
    resolved_graph_path = Path(graph_path).resolve()
    stat = resolved_graph_path.stat()
    content = resolved_graph_path.read_text(encoding="utf-8")
    cache_key = f"path:{resolved_graph_path}:{stat.st_mtime_ns}:{stat.st_size}"
    return _cache_decision(cache_key, content)


def load_decision_from_content(graph_content: Mapping[str, Any] | str) -> zen.ZenDecision:
    content = _normalize_graph_content(graph_content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return _cache_decision(f"content:{digest}", content)