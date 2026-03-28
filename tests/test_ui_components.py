"""
Unit tests for ui/add_dialog.py and ui/tray.py (headless).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from ui.add_dialog import AddDownloadDialog
from ui.tray import SystemTray, create_tray_icon_pixmap


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ══════════════════════════════════════════════════════════════════════════════
#  ADD DOWNLOAD DIALOG
# ══════════════════════════════════════════════════════════════════════════════

class TestAddDownloadDialog:
    @pytest.fixture
    def config(self) -> dict:
        return {
            "general": {
                "download_directory": "/tmp/dl",
                "default_chunks": 5,
            },
        }

    def test_creation(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        assert dialog.windowTitle() == "Add New Download"

    def test_initial_url(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config, initial_url="https://x.com/file.zip")
        assert dialog._url_input.text() == "https://x.com/file.zip"

    def test_url_validation_valid(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        dialog._url_input.setText("https://example.com/file.zip")
        assert dialog._start_btn.isEnabled() is True

    def test_url_validation_invalid(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        dialog._url_input.setText("not-a-url")
        assert dialog._start_btn.isEnabled() is False

    def test_url_validation_empty(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        dialog._url_input.setText("")
        assert dialog._start_btn.isEnabled() is False

    def test_auto_filename(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        dialog._url_input.setText("https://example.com/movie.mp4")
        # filename should be auto-detected
        assert "movie.mp4" in dialog._filename_input.text()

    def test_auto_category(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        dialog._url_input.setText("https://example.com/video.mkv")
        assert dialog._category_combo.currentText() == "Video"

    def test_advanced_toggle(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        assert dialog._advanced_widget.isHidden() is True
        dialog._toggle_advanced(True)
        assert dialog._advanced_widget.isHidden() is False
        dialog._toggle_advanced(False)
        assert dialog._advanced_widget.isHidden() is True

    def test_chunk_slider(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        dialog._toggle_advanced(True)
        dialog._chunk_slider.setValue(5)
        assert dialog._chunk_label.text() == "5"

    def test_signal_emitted(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        dialog._url_input.setText("https://x.com/test.zip")
        dialog._filename_input.setText("test.zip")

        received = []
        dialog.download_accepted.connect(lambda data: received.append(data))
        dialog._on_start()

        assert len(received) == 1
        assert received[0]["url"] == "https://x.com/test.zip"
        assert received[0]["filename"] == "test.zip"

    def test_browse_default_dir(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        assert dialog._save_dir_input.text() == "/tmp/dl"

    def test_ftp_url_valid(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        dialog._url_input.setText("ftp://files.example.com/data.bin")
        assert dialog._start_btn.isEnabled() is True

    def test_magnet_url_valid(self, qapp, config: dict) -> None:
        dialog = AddDownloadDialog(config)
        dialog._url_input.setText("magnet:?xt=urn:btih:abc123")
        assert dialog._start_btn.isEnabled() is True


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM TRAY
# ══════════════════════════════════════════════════════════════════════════════

class TestTrayIcon:
    def test_create_pixmap(self, qapp) -> None:
        pixmap = create_tray_icon_pixmap(64)
        assert pixmap.width() == 64
        assert pixmap.height() == 64
        assert not pixmap.isNull()

    def test_create_pixmap_custom_size(self, qapp) -> None:
        pixmap = create_tray_icon_pixmap(128)
        assert pixmap.width() == 128

    def test_system_tray_creation(self, qapp) -> None:
        config = {"general": {"show_notifications": True}}
        tray = SystemTray(config)
        assert tray.toolTip().startswith("IDM")

    def test_tray_update_status(self, qapp) -> None:
        tray = SystemTray({})
        tray.update_status(3, 1048576.0)
        tooltip = tray.toolTip()
        assert "3" in tooltip
        assert "active" in tooltip.lower() or "download" in tooltip.lower()

    def test_tray_update_status_idle(self, qapp) -> None:
        tray = SystemTray({})
        tray.update_status(0, 0.0)
        assert "No active" in tray.toolTip()

    def test_signals_exist(self, qapp) -> None:
        tray = SystemTray({})
        # Verify signals are declared
        assert hasattr(tray, "show_window_requested")
        assert hasattr(tray, "add_url_requested")
        assert hasattr(tray, "pause_all_requested")
        assert hasattr(tray, "resume_all_requested")
        assert hasattr(tray, "quit_requested")

    def test_notify_methods_dont_raise(self, qapp) -> None:
        tray = SystemTray({"general": {"show_notifications": False}})
        # With notifications disabled, these should be no-ops
        tray.notify_complete("file.zip")
        tray.notify_error("file.zip", "Error")
        tray.notify_added("file.zip")
