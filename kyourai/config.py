"""Config loader — reads ~/.kyourai/config.yaml with env override.

Behavioral settings live in config.yaml. Secrets (API keys, tokens) live in
~/.kyourai/.env and are NEVER read here — matching Hermes' separation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from kyourai.constants import get_kyourai_home

_CONFIG_CACHE: tuple[float, dict[str, Any]] | None = None


def _config_path() -> Path:
    return get_kyourai_home() / "config.yaml"


def load_config() -> dict[str, Any]:
    """Load config.yaml, mtime-cached. Returns empty dict if missing."""
    global _CONFIG_CACHE
    path = _config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _CONFIG_CACHE = None
        return {}
    if _CONFIG_CACHE and _CONFIG_CACHE[0] == mtime:
        return _CONFIG_CACHE[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    _CONFIG_CACHE = (mtime, data)
    return data


def get_config_value(key: str, default: Any = None) -> Any:
    """Dotted-path config lookup: get('memory.provider', 'builtin')."""
    cfg = load_config()
    parts = key.split(".")
    node: Any = cfg
    for part in parts:
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node if node is not None else default


def save_config(data: dict[str, Any]) -> None:
    """Write config.yaml atomically."""
    global _CONFIG_CACHE
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=True)
    tmp.replace(path)
    _CONFIG_CACHE = None  # invalidate cache
