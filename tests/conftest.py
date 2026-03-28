"""
Shared pytest fixtures for the IDM test suite.

Provides:
    - Temporary directories for downloads and chunks
    - In-memory SQLite database
    - Sample configuration dictionary
    - Mock aiohttp session
"""

from __future__ import annotations

import json
import asyncio
import tempfile
from pathlib import Path
from typing import Any, Generator

import pytest


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for test artifacts."""
    return tmp_path


@pytest.fixture
def sample_config(tmp_path: Path) -> dict[str, Any]:
    """
    Return a test configuration with paths pointing to tmp_path.

    This ensures tests never write to the real filesystem.
    """
    return {
        "general": {
            "download_directory": str(tmp_path / "downloads"),
            "max_concurrent_downloads": 2,
            "default_chunks": 4,
            "auto_start_downloads": False,
            "minimize_to_tray": False,
            "start_minimized": False,
            "start_with_system": False,
            "language": "en",
            "theme": "dark",
            "confirm_on_exit": False,
            "sound_on_complete": False,
            "show_notifications": False,
        },
        "network": {
            "bandwidth_limit_kbps": 0,
            "connection_timeout_seconds": 10,
            "read_timeout_seconds": 30,
            "max_retries": 2,
            "retry_base_delay_seconds": 1,
            "retry_max_delay_seconds": 10,
            "user_agent": "IDM-Test/1.0",
            "proxy": {
                "enabled": False,
                "type": "http",
                "host": "",
                "port": 0,
                "username": "",
                "password": "",
                "use_for_all": True,
            },
            "ipv6_enabled": True,
            "verify_ssl": False,
        },
        "advanced": {
            "dynamic_chunk_adjustment": False,
            "min_chunk_size_bytes": 1024,
            "max_chunk_size_bytes": 1_048_576,
            "speed_sample_interval_ms": 100,
            "history_retention_days": 7,
            "max_speed_history_points": 60,
            "chunk_buffer_size_bytes": 4096,
            "hash_verify_on_complete": True,
            "hash_algorithm": "sha256",
            "temp_directory": str(tmp_path / "chunks"),
        },
        "categories": {
            "Video": [".mp4", ".mkv"],
            "Audio": [".mp3", ".flac"],
            "Document": [".pdf", ".txt"],
            "Other": [],
        },
    }


@pytest.fixture
def config_file(tmp_path: Path, sample_config: dict[str, Any]) -> Path:
    """Write sample_config to a temporary JSON file and return its path."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(sample_config, indent=2), encoding="utf-8"
    )
    return config_path
