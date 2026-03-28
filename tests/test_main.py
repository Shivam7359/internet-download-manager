"""
Unit tests for main.py — configuration loading, deep merge, and bootstrap.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

# Add project root to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import (
    BatchedConfigSaver,
    deep_merge,
    get_default_config,
    load_config,
    save_config,
    get_theme_stylesheet,
)


class TestDeepMerge:
    """Tests for the deep_merge utility function."""

    def test_simple_merge(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self) -> None:
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        override = {"a": {"y": 99, "z": 100}}
        result = deep_merge(base, override)
        assert result == {"a": {"x": 1, "y": 99, "z": 100}, "b": 3}

    def test_does_not_mutate_inputs(self) -> None:
        base = {"a": {"x": 1}}
        override = {"a": {"y": 2}}
        _ = deep_merge(base, override)
        assert base == {"a": {"x": 1}}
        assert override == {"a": {"y": 2}}

    def test_empty_override(self) -> None:
        base = {"a": 1}
        result = deep_merge(base, {})
        assert result == {"a": 1}

    def test_empty_base(self) -> None:
        override = {"a": 1}
        result = deep_merge({}, override)
        assert result == {"a": 1}

    def test_override_replaces_non_dict_with_dict(self) -> None:
        base = {"a": "string_value"}
        override = {"a": {"nested": True}}
        result = deep_merge(base, override)
        assert result == {"a": {"nested": True}}

    def test_result_does_not_share_nested_list_references(self) -> None:
        base = {"categories": {"Video": [".mp4", ".mkv"]}}
        result = deep_merge(base, {})

        result["categories"]["Video"].append(".avi")

        assert base["categories"]["Video"] == [".mp4", ".mkv"]


class TestDefaultConfig:
    """Tests for get_default_config."""

    def test_has_required_sections(self) -> None:
        config = get_default_config()
        required = [
            "general", "network", "scheduler", "categories",
            "server", "clipboard", "file_conflicts", "advanced",
        ]
        for section in required:
            assert section in config, f"Missing config section: {section}"

    def test_default_download_chunks(self) -> None:
        config = get_default_config()
        assert config["general"]["default_chunks"] == 5

    def test_default_max_concurrent(self) -> None:
        config = get_default_config()
        assert config["general"]["max_concurrent_downloads"] == 4

    def test_default_theme_is_dark(self) -> None:
        config = get_default_config()
        assert config["general"]["theme"] == "dark"

    def test_server_port(self) -> None:
        config = get_default_config()
        assert config["server"]["port"] == 6800


class TestConfigIO:
    """Tests for load_config and save_config."""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        config_path = tmp_path / "test_config.json"
        original = get_default_config()
        original["general"]["theme"] = "light"

        save_config(config_path, original)
        loaded = load_config(config_path)

        assert loaded["general"]["theme"] == "light"

    def test_load_missing_file_creates_default(self, tmp_path: Path) -> None:
        config_path = tmp_path / "nonexistent.json"
        config = load_config(config_path)

        assert config_path.exists()
        assert config["general"]["max_concurrent_downloads"] == 4

    def test_load_corrupt_file_returns_default(self, tmp_path: Path) -> None:
        config_path = tmp_path / "corrupt.json"
        config_path.write_text("NOT VALID JSON {{{", encoding="utf-8")

        config = load_config(config_path)
        # Should fall back to defaults
        assert config["general"]["default_chunks"] == 5

    def test_forward_compatibility(self, tmp_path: Path) -> None:
        """Config files missing new keys should gain them via deep_merge."""
        config_path = tmp_path / "old.json"
        # Simulate an old config that only has 'general'
        old = {"general": {"theme": "light"}}
        config_path.write_text(json.dumps(old), encoding="utf-8")

        config = load_config(config_path)
        # Should have the user's theme
        assert config["general"]["theme"] == "light"
        # Should also have sections from defaults
        assert "network" in config
        assert "advanced" in config


class TestThemeStylesheet:
    """Tests for get_theme_stylesheet."""

    def test_dark_theme_returns_string(self) -> None:
        qss = get_theme_stylesheet("dark")
        assert isinstance(qss, str)
        assert len(qss) > 100
        assert "#0d1117" in qss  # dark bg color

    def test_light_theme_returns_string(self) -> None:
        qss = get_theme_stylesheet("light")
        assert isinstance(qss, str)
        assert "#ffffff" in qss  # light bg color

    def test_unknown_theme_defaults_to_dark(self) -> None:
        qss = get_theme_stylesheet("cyberpunk")
        assert "#0d1117" in qss


class TestBatchedConfigSaver:
    """Tests for debounced configuration persistence."""

    def test_schedule_coalesces_to_latest_snapshot(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        writes: list[dict[str, Any]] = []
        done = threading.Event()

        def fake_save(path: Path, cfg: dict[str, Any]) -> None:
            assert path == config_path
            writes.append(cfg)
            done.set()

        saver = BatchedConfigSaver(
            config_path=config_path,
            save_fn=fake_save,
            debounce_seconds=0.05,
        )

        saver.schedule({"general": {"theme": "dark"}})
        saver.schedule({"general": {"theme": "light"}})

        assert done.wait(timeout=1.0)
        assert len(writes) == 1
        assert writes[0]["general"]["theme"] == "light"

    def test_flush_writes_immediately_without_waiting_timer(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        writes: list[dict[str, Any]] = []

        def fake_save(path: Path, cfg: dict[str, Any]) -> None:
            assert path == config_path
            writes.append(cfg)

        saver = BatchedConfigSaver(
            config_path=config_path,
            save_fn=fake_save,
            debounce_seconds=0.5,
        )

        saver.schedule({"network": {"verify_ssl": True}})
        saver.flush()

        assert len(writes) == 1
        assert writes[0]["network"]["verify_ssl"] is True

        # No delayed second write should occur after flush.
        time.sleep(0.2)
        assert len(writes) == 1
