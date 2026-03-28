"""
Unit tests for ui/ — table model and theme (headless, no display required).

Tests cover:
    • DownloadTableModel — CRUD operations, data retrieval, progress updates
    • ProgressDelegate — instantiation
    • Theme — stylesheet is non-empty
    • Column definitions
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Skip all tests if PyQt6 is not installed
pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt, QModelIndex
from PyQt6.QtWidgets import QApplication

from ui.main_window import (
    DownloadTableModel,
    ProgressDelegate,
    COLUMNS,
    COL_FILENAME,
    COL_SIZE,
    COL_PROGRESS,
    COL_STATUS,
    COL_SPEED,
    STATUS_COLORS,
)
from ui.theme import DARK_THEME, apply_dark_theme


# ── QApplication fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    """Create a QApplication instance for the entire test module."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ══════════════════════════════════════════════════════════════════════════════
#  COLUMN DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestColumns:
    def test_column_count(self) -> None:
        assert len(COLUMNS) == 10

    def test_column_names(self) -> None:
        names = [c[1] for c in COLUMNS]
        assert "Filename" in names
        assert "Progress" in names
        assert "Chunks" in names
        assert "Status" in names


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD TABLE MODEL
# ══════════════════════════════════════════════════════════════════════════════

class TestDownloadTableModel:
    @pytest.fixture
    def model(self, qapp) -> DownloadTableModel:
        return DownloadTableModel()

    @pytest.fixture
    def sample_downloads(self) -> list[dict]:
        return [
            {
                "id": "dl-1",
                "url": "http://x.com/a.zip",
                "filename": "a.zip",
                "file_size": 1048576,
                "downloaded_bytes": 524288,
                "progress_percent": 50.0,
                "status": "downloading",
                "priority": "normal",
                "category": "Archive",
                "date_added": "2026-03-18T10:00:00",
                "save_path": "/dl/a.zip",
            },
            {
                "id": "dl-2",
                "url": "http://x.com/b.mp4",
                "filename": "b.mp4",
                "file_size": 10485760,
                "downloaded_bytes": 10485760,
                "progress_percent": 100.0,
                "status": "completed",
                "priority": "high",
                "category": "Video",
                "date_added": "2026-03-18T09:00:00",
                "save_path": "/dl/b.mp4",
            },
        ]

    def test_empty_model(self, model: DownloadTableModel) -> None:
        assert model.rowCount() == 0
        assert model.columnCount() == len(COLUMNS)

    def test_set_downloads(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        assert model.rowCount() == 2

    def test_header_data(self, model: DownloadTableModel) -> None:
        h = model.headerData(
            COL_FILENAME, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
        )
        assert h == "Filename"

    def test_display_data_filename(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        idx = model.index(0, COL_FILENAME)
        value = model.data(idx, Qt.ItemDataRole.DisplayRole)
        assert value == "a.zip"

    def test_display_data_size(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        idx = model.index(0, COL_SIZE)
        value = model.data(idx, Qt.ItemDataRole.DisplayRole)
        assert "MB" in value or "KB" in value

    def test_display_data_status(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        idx = model.index(0, COL_STATUS)
        value = model.data(idx, Qt.ItemDataRole.DisplayRole)
        assert value == "Downloading"

    def test_progress_role(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        idx = model.index(0, COL_PROGRESS)
        value = model.data(idx, Qt.ItemDataRole.UserRole + 1)
        assert value == 50.0

    def test_status_color(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        idx = model.index(0, COL_STATUS)
        color = model.data(idx, Qt.ItemDataRole.ForegroundRole)
        assert color is not None

    def test_add_download(self, model: DownloadTableModel) -> None:
        model.add_download({
            "id": "dl-new",
            "filename": "new.zip",
            "status": "queued",
        })
        assert model.rowCount() == 1
        assert model.get_download_id(0) == "dl-new"

    def test_update_download(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        model.update_download("dl-1", {"filename": "renamed.zip"})

        idx = model.index(0, COL_FILENAME)
        assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "renamed.zip"

    def test_update_progress(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        model.update_progress("dl-1", 786432, 1048576, 100000.0, 5.0)

        idx = model.index(0, COL_SPEED)
        speed_text = model.data(idx, Qt.ItemDataRole.DisplayRole)
        assert "KB/s" in speed_text

    def test_update_status(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        model.update_status("dl-1", "paused")

        idx = model.index(0, COL_STATUS)
        assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "Paused"

    def test_remove_download(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        assert model.rowCount() == 2

        model.remove_download("dl-1")
        assert model.rowCount() == 1
        assert model.get_download_id(0) == "dl-2"

    def test_remove_nonexistent(self, model: DownloadTableModel) -> None:
        model.remove_download("nope")  # should not raise

    def test_get_download_id_out_of_range(
        self, model: DownloadTableModel
    ) -> None:
        assert model.get_download_id(99) is None

    def test_user_role_returns_full_dict(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        idx = model.index(0, COL_FILENAME)
        dl = model.data(idx, Qt.ItemDataRole.UserRole)
        assert isinstance(dl, dict)
        assert dl["id"] == "dl-1"

    def test_completed_clears_speed(
        self, model: DownloadTableModel, sample_downloads: list
    ) -> None:
        model.set_downloads(sample_downloads)
        model._speeds["dl-1"] = 100000.0
        model.update_status("dl-1", "completed")
        assert "dl-1" not in model._speeds


# ══════════════════════════════════════════════════════════════════════════════
#  PROGRESS DELEGATE
# ══════════════════════════════════════════════════════════════════════════════

class TestProgressDelegate:
    def test_instantiation(self, qapp) -> None:
        delegate = ProgressDelegate()
        assert delegate is not None


# ══════════════════════════════════════════════════════════════════════════════
#  THEME
# ══════════════════════════════════════════════════════════════════════════════

class TestTheme:
    def test_dark_theme_not_empty(self) -> None:
        assert len(DARK_THEME) > 100

    def test_contains_main_window(self) -> None:
        assert "QMainWindow" in DARK_THEME

    def test_contains_colors(self) -> None:
        assert "#0D1117" in DARK_THEME  # background
        assert "#E5E7EB" in DARK_THEME  # text

    def test_apply_theme(self, qapp) -> None:
        apply_dark_theme(qapp)
        # Should not raise

    def test_status_colors_defined(self) -> None:
        assert "downloading" in STATUS_COLORS
        assert "completed" in STATUS_COLORS
        assert "failed" in STATUS_COLORS
