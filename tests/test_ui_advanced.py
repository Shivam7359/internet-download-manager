"""
Unit tests for ui/speed_graph.py and ui/settings_dialog.py (headless).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from ui.speed_graph import SpeedGraphWidget, SpeedPanel
from ui.settings_dialog import SettingsDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def full_config() -> dict:
    return {
        "general": {
            "download_directory": "/tmp/dl",
            "max_concurrent_downloads": 4,
            "default_chunks": 5,
            "auto_start_downloads": True,
            "minimize_to_tray": True,
            "start_minimized": False,
            "start_with_system": False,
            "language": "en",
            "theme": "dark",
            "confirm_on_exit": True,
            "sound_on_complete": True,
            "show_notifications": True,
        },
        "network": {
            "bandwidth_limit_kbps": 0,
            "per_download_bandwidth_kbps": 0,
            "connection_timeout_seconds": 30,
            "read_timeout_seconds": 60,
            "max_retries": 5,
            "proxy": {
                "enabled": False,
                "type": "http",
                "host": "",
                "port": 0,
                "username": "",
                "password": "",
            },
            "verify_ssl": True,
        },
        "scheduler": {
            "enabled": False,
            "start_time": "02:00",
            "end_time": "06:00",
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "action_after_complete": "none",
        },
        "advanced": {
            "dynamic_chunk_adjustment": True,
            "min_chunk_size_bytes": 262144,
            "max_chunk_size_bytes": 52428800,
            "first_byte_timeout_seconds": 15,
            "hash_verify_on_complete": True,
            "speed_sample_interval_ms": 500,
            "history_retention_days": 90,
            "chunk_buffer_size_bytes": 65536,
            "temp_directory": "",
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
#  SPEED GRAPH WIDGET
# ══════════════════════════════════════════════════════════════════════════════

class TestSpeedGraphWidget:
    def test_creation(self, qapp) -> None:
        g = SpeedGraphWidget()
        assert g.current_speed == 0.0
        assert g.peak_speed == 0.0

    def test_add_sample(self, qapp) -> None:
        g = SpeedGraphWidget(max_samples=10)
        g.add_sample(1024.0)
        assert g.current_speed == 1024.0
        assert g.peak_speed == 1024.0

    def test_peak_tracking(self, qapp) -> None:
        g = SpeedGraphWidget(max_samples=10)
        g.add_sample(1000.0)
        g.add_sample(5000.0)
        g.add_sample(3000.0)
        assert g.peak_speed == 5000.0
        assert g.current_speed == 3000.0

    def test_reset(self, qapp) -> None:
        g = SpeedGraphWidget(max_samples=10)
        g.add_sample(1000.0)
        g.reset()
        assert g.current_speed == 0.0
        assert g.peak_speed == 0.0

    def test_max_samples(self, qapp) -> None:
        g = SpeedGraphWidget(max_samples=5)
        for i in range(10):
            g.add_sample(float(i * 100))
        assert len(g._samples) == 5

    def test_nice_max_small(self, qapp) -> None:
        assert SpeedGraphWidget._nice_max(150) == 200.0

    def test_nice_max_zero(self, qapp) -> None:
        assert SpeedGraphWidget._nice_max(0) == 1024.0

    def test_nice_max_large(self, qapp) -> None:
        result = SpeedGraphWidget._nice_max(3_500_000)
        assert result >= 3_500_000

    def test_minimum_size(self, qapp) -> None:
        g = SpeedGraphWidget()
        assert g.minimumWidth() >= 300
        assert g.minimumHeight() >= 120


# ══════════════════════════════════════════════════════════════════════════════
#  SPEED PANEL
# ══════════════════════════════════════════════════════════════════════════════

class TestSpeedPanel:
    def test_creation(self, qapp) -> None:
        p = SpeedPanel()
        assert p.graph is not None

    def test_update_stats(self, qapp) -> None:
        p = SpeedPanel()
        p.update_stats(1048576.0, 3)
        assert p.graph.current_speed == 1048576.0

    def test_update_stats_zero(self, qapp) -> None:
        p = SpeedPanel()
        p.update_stats(0.0, 0)
        assert p.graph.current_speed == 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS DIALOG
# ══════════════════════════════════════════════════════════════════════════════

class TestSettingsDialog:
    def test_creation(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        assert dialog.windowTitle() == "Settings"

    def test_tabs_present(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        assert dialog._tabs.count() == 4
        assert dialog._tabs.tabText(0) == "General"
        assert dialog._tabs.tabText(1) == "Network"
        assert dialog._tabs.tabText(2) == "Scheduler"
        assert dialog._tabs.tabText(3) == "Advanced"

    def test_loads_general_values(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        assert dialog._download_dir.text() == "/tmp/dl"
        assert dialog._max_concurrent.value() == 4
        assert dialog._default_chunks.value() == 5
        assert dialog._theme_combo.currentText() == "dark"
        assert dialog._auto_start.isChecked() is True

    def test_loads_network_values(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        assert dialog._bandwidth_limit.value() == 0
        assert dialog._per_download_bandwidth_limit.value() == 0
        assert dialog._conn_timeout.value() == 30
        assert dialog._first_byte_timeout.value() == 15
        assert dialog._max_retries.value() == 5
        assert dialog._verify_ssl.isChecked() is True

    def test_loads_scheduler_values(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        assert dialog._scheduler_enabled.isChecked() is False
        assert dialog._sched_start.text() == "02:00"
        assert dialog._sched_end.text() == "06:00"
        # All days should be checked
        for cb in dialog._day_checks.values():
            assert cb.isChecked() is True

    def test_loads_advanced_values(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        assert dialog._dynamic_chunks.isChecked() is True
        assert dialog._hash_verify.isChecked() is True
        assert dialog._speed_interval.value() == 500
        assert dialog._history_days.value() == 90

    def test_collect_values(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        values = dialog._collect_values()
        assert "general" in values
        assert "network" in values
        assert "scheduler" in values
        assert "advanced" in values
        assert values["general"]["download_directory"] == "/tmp/dl"

    def test_signal_emitted_on_save(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        received = []
        dialog.settings_saved.connect(lambda v: received.append(v))
        dialog._on_save()
        assert len(received) == 1
        assert "general" in received[0]

    def test_modify_and_collect(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        dialog._max_concurrent.setValue(8)
        dialog._bandwidth_limit.setValue(500)
        dialog._per_download_bandwidth_limit.setValue(125)
        dialog._first_byte_timeout.setValue(9)
        values = dialog._collect_values()
        assert values["general"]["max_concurrent_downloads"] == 8
        assert values["network"]["bandwidth_limit_kbps"] == 500
        assert values["network"]["per_download_bandwidth_kbps"] == 125
        assert values["advanced"]["first_byte_timeout_seconds"] == 9

    def test_proxy_values(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        dialog._proxy_enabled.setChecked(True)
        dialog._proxy_host.setText("192.168.1.1")
        dialog._proxy_port.setValue(8080)
        values = dialog._collect_values()
        assert values["network"]["proxy"]["enabled"] is True
        assert values["network"]["proxy"]["host"] == "192.168.1.1"
        assert values["network"]["proxy"]["port"] == 8080

    def test_scheduler_days_none(self, qapp, full_config: dict) -> None:
        dialog = SettingsDialog(full_config)
        for cb in dialog._day_checks.values():
            cb.setChecked(False)
        values = dialog._collect_values()
        assert values["scheduler"]["days"] == []

    def test_advanced_chunk_size_conversion(
        self, qapp, full_config: dict
    ) -> None:
        dialog = SettingsDialog(full_config)
        dialog._min_chunk_size.setValue(512)  # 512 KB
        dialog._max_chunk_size.setValue(100)  # 100 MB
        values = dialog._collect_values()
        assert values["advanced"]["min_chunk_size_bytes"] == 512 * 1024
        assert values["advanced"]["max_chunk_size_bytes"] == 100 * 1024 * 1024


class TestAddDownloadDialogMediaQuality:
    def test_media_resolution_populates_quality_choices(self, qapp, full_config: dict) -> None:
        from ui.add_dialog import AddDownloadDialog

        dialog = AddDownloadDialog(full_config)
        info = {
            "title": "Sample Clip",
            "ext": "mp4",
            "url": "https://cdn.example.com/video-best.mp4",
            "formats": [
                {
                    "format_id": "18",
                    "resolution": "360p",
                    "ext": "mp4",
                    "filesize": 5_000_000,
                    "url": "https://cdn.example.com/video-360.mp4",
                },
                {
                    "format_id": "22",
                    "resolution": "720p",
                    "ext": "mp4",
                    "filesize": 15_000_000,
                    "url": "https://cdn.example.com/video-720.mp4",
                },
            ],
        }

        dialog._on_media_resolved(info)

        assert dialog._quality_combo.isEnabled() is True
        assert dialog._quality_combo.count() == 3  # auto + 2 formats

        dialog._quality_combo.setCurrentIndex(2)
        assert dialog._resolved_url == "https://cdn.example.com/video-720.mp4"
