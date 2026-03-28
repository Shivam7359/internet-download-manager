"""Lightweight config loader for public desktop folder structure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

CONFIG_PATH = Path("config.json")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return cast("dict[str, Any]", json.load(handle))


def save_config(data: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
