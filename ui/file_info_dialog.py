"""
IDM UI — File Info Preview Dialog
==================================
A dialog that displays file information before download starts.

Features:
    • Shows file metadata (name, size, type, etc.)
    • Displays save location with category
    • Refresh button to re-fetch file info
    • Start Download / Download Later / Cancel buttons
    • Progress indicator during info fetching
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional, Callable

from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, pyqtSlot, QThread, QSize,
)
from PyQt6.QtGui import QFont, QColor, QIcon
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QComboBox, QWidget, QSizePolicy,
    QProgressBar, QMessageBox, QFrame, QLineEdit, QFileDialog,
)

from core.network import format_size, NetworkManager
from utils.categoriser import categorise

log = logging.getLogger("idm.ui.file_info_dialog")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  PREFLIGHT INFO FETCHER THREAD                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class PreflightThread(QThread):
    """
    Worker thread to fetch file info via network preflight check.

    Signals:
        info_fetched(dict) — emitted with preflight result on success
        fetch_failed(str) — emitted with error message on failure
    """

    info_fetched = pyqtSignal(dict)
    fetch_failed = pyqtSignal(str)

    def __init__(
        self,
        url: str,
        config: dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._config = config
        self._network: Optional[NetworkManager] = None

    def run(self) -> None:
        """Fetch preflight info in the worker thread."""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(self._fetch_preflight())
            loop.close()

            if result:
                self.info_fetched.emit(result)
            else:
                self.fetch_failed.emit("No file information available")
        except Exception as e:
            self.fetch_failed.emit(f"Error fetching file info: {str(e)}")
            log.exception("Preflight fetch failed")

    async def _fetch_preflight(self) -> Optional[dict]:
        """Async method to fetch preflight info."""
        try:
            network = NetworkManager(self._config)
            await network.initialize()

            preflight = await network.preflight(self._url)
            await network.close()

            if preflight.error:
                return None

            return {
                "url": preflight.url,
                "filename": preflight.filename,
                "file_size": preflight.file_size,
                "content_type": preflight.content_type,
                "resume_supported": preflight.resume_supported,
                "etag": preflight.etag,
                "last_modified": preflight.last_modified,
                "status_code": preflight.status_code,
            }
        except Exception as e:
            log.error(f"Preflight error: {e}")
            return None


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FILE INFO DIALOG                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class FileInfoDialog(QDialog):
    """
    Modal dialog showing file information before download.

    Signals:
        download_accepted(dict) — emitted when user starts download
    """

    download_accepted = pyqtSignal(dict)

    def __init__(
        self,
        url: str,
        filename: str = "",
        save_dir: str = "",
        config: dict[str, Any] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._filename = filename
        self._save_dir = save_dir or r"D:\idm down"
        self._config = config or {}
        self._preflight_thread: Optional[PreflightThread] = None
        self._file_info: dict[str, Any] = {}

        self.setWindowTitle("Download File Info")
        self.setMinimumWidth(600)
        self.setMinimumHeight(400)
        self.setModal(True)

        self._build_ui()
        self._fetch_file_info()

    # ── UI Construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Build the dialog UI."""
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 20)

        # ── Title ──────────────────────────────────────────────────────────
        title = QLabel("Download File Info")
        title_font = title.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #E5E7EB;")
        layout.addWidget(title)

        subtitle = QLabel("Review the file details before downloading")
        subtitle.setStyleSheet("color: #8B949E; font-size: 12px;")
        layout.addWidget(subtitle)

        layout.addWidget(self._create_separator())

        # ── File Info Display ──────────────────────────────────────────────
        info_group = QFrame()
        info_group.setStyleSheet("""
            QFrame {
                border: 1px solid #21262D;
                border-radius: 8px;
                background: #0D1117;
                padding: 16px;
            }
        """)
        info_layout = QFormLayout(info_group)
        info_layout.setSpacing(12)
        info_layout.setVerticalSpacing(12)
        info_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # URL
        self._url_label = QLabel(self._url[:70] + "..." if len(self._url) > 70 else self._url)
        self._url_label.setStyleSheet("color: #58A6FF; font-family: monospace; font-size: 11px;")
        self._url_label.setWordWrap(True)
        info_layout.addRow("URL:", self._url_label)

        # Filename
        self._filename_label = QLabel(self._filename or "Detecting...")
        self._filename_label.setStyleSheet("color: #E5E7EB; font-weight: 600;")
        self._filename_label.setWordWrap(True)
        info_layout.addRow("Filename:", self._filename_label)

        # File Size
        self._size_label = QLabel("Fetching...")
        self._size_label.setStyleSheet("color: #8B949E;")
        info_layout.addRow("File Size:", self._size_label)

        # Content Type
        self._type_label = QLabel("Detecting...")
        self._type_label.setStyleSheet("color: #8B949E;")
        info_layout.addRow("Content Type:", self._type_label)

        # Resume Support
        self._resume_label = QLabel("Checking...")
        self._resume_label.setStyleSheet("color: #8B949E;")
        info_layout.addRow("Resume Support:", self._resume_label)

        # Save Location (editable)
        location_row = QHBoxLayout()
        location_row.setSpacing(8)

        self._save_dir_input = QLineEdit(self._save_dir)
        self._save_dir_input.setStyleSheet(self._input_style())
        self._save_dir_input.setMinimumHeight(36)
        location_row.addWidget(self._save_dir_input, 1)

        browse_btn = QPushButton("Browse")
        browse_btn.setFixedWidth(90)
        browse_btn.setStyleSheet(self._secondary_button_style())
        browse_btn.clicked.connect(self._on_browse_location)
        location_row.addWidget(browse_btn)

        info_layout.addRow("Save to:", location_row)

        layout.addWidget(info_group)

        # ── Settings ───────────────────────────────────────────────────────
        settings_group = QFrame()
        settings_group.setStyleSheet("""
            QFrame {
                border: 1px solid #21262D;
                border-radius: 8px;
                background: #0D1117;
                padding: 16px;
            }
        """)
        settings_layout = QFormLayout(settings_group)
        settings_layout.setSpacing(10)

        # Category
        self._category_combo = QComboBox()
        self._category_combo.addItems([
            "Auto", "Video", "Audio", "Image",
            "Document", "Software", "Archive", "Other",
        ])
        self._category_combo.setStyleSheet(self._combo_style())
        self._category_combo.setMinimumHeight(36)
        settings_layout.addRow("Category:", self._category_combo)

        layout.addWidget(settings_group)

        # ── Progress Bar (hidden initially) ────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #21262D;
                border-radius: 4px;
                background: #161B22;
                height: 20px;
            }
            QProgressBar::chunk {
                background: #58A6FF;
                border-radius: 2px;
            }
        """)
        self._progress_bar.setMaximum(0)  # Indeterminate
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        # ── Buttons ────────────────────────────────────────────────────────
        layout.addStretch()
        layout.addWidget(self._create_separator())

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFixedWidth(100)
        refresh_btn.setStyleSheet(self._secondary_button_style())
        refresh_btn.clicked.connect(self._fetch_file_info)
        btn_layout.addWidget(refresh_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(100)
        cancel_btn.setStyleSheet(self._secondary_button_style())
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        later_btn = QPushButton("Download Later")
        later_btn.setFixedWidth(140)
        later_btn.setStyleSheet(self._secondary_button_style())
        later_btn.clicked.connect(self._on_download_later)
        btn_layout.addWidget(later_btn)

        self._start_btn = QPushButton("Start Download")
        self._start_btn.setFixedWidth(140)
        self._start_btn.setStyleSheet(self._primary_button_style())
        self._start_btn.clicked.connect(self._on_start)
        btn_layout.addWidget(self._start_btn)

        layout.addLayout(btn_layout)

    # ── File Info Fetching ─────────────────────────────────────────────────

    def _fetch_file_info(self) -> None:
        """Start thread to fetch file information."""
        self._progress_bar.setVisible(True)
        self._start_btn.setEnabled(False)

        if self._preflight_thread and self._preflight_thread.isRunning():
            self._preflight_thread.terminate()
            self._preflight_thread.wait()

        self._preflight_thread = PreflightThread(self._url, self._config, self)
        self._preflight_thread.info_fetched.connect(self._on_info_fetched)
        self._preflight_thread.fetch_failed.connect(self._on_info_failed)
        self._preflight_thread.start()

    @pyqtSlot(dict)
    def _on_info_fetched(self, info: dict) -> None:
        """Handle successful file info fetch."""
        self._file_info = info
        self._progress_bar.setVisible(False)
        self._start_btn.setEnabled(True)

        # Update filename label
        filename = info.get("filename", "Unknown")
        self._filename_label.setText(filename)
        if not self._filename:
            self._filename = filename

        # Update size label
        file_size = info.get("file_size", -1)
        if file_size > 0:
            self._size_label.setText(format_size(file_size))
        else:
            self._size_label.setText("Unknown")

        # Update content type
        content_type = info.get("content_type", "Unknown")
        self._type_label.setText(content_type or "Unknown")

        # Update resume support
        resume = info.get("resume_supported", False)
        self._resume_label.setText("✓ Yes" if resume else "✗ No")
        resume_color = "#3FB950" if resume else "#F85149"
        self._resume_label.setStyleSheet(f"color: {resume_color};")

        # Auto-detect category if not set
        if self._category_combo.currentText() == "Auto" and filename:
            cat = categorise(filename)
            if cat != "Other":
                idx = self._category_combo.findText(cat)
                if idx >= 0:
                    self._category_combo.setCurrentIndex(idx)

    @pyqtSlot(str)
    def _on_info_failed(self, error: str) -> None:
        """Handle file info fetch failure."""
        self._progress_bar.setVisible(False)
        self._start_btn.setEnabled(True)
        QMessageBox.warning(self, "Info Fetch Failed", error)

    @pyqtSlot()
    def _on_browse_location(self) -> None:
        """Ask user where to save the file."""
        current_dir = self._save_dir_input.text().strip() or self._save_dir
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose Download Folder",
            current_dir,
        )
        if selected:
            self._save_dir_input.setText(selected)

    # ── Button Actions ─────────────────────────────────────────────────────

    @pyqtSlot()
    def _on_start(self) -> None:
        """Start download immediately."""
        save_dir = self._save_dir_input.text().strip() or self._save_dir
        category = self._category_combo.currentText()
        if category == "Auto":
            category = categorise(self._filename) if self._filename else "Other"

        self.download_accepted.emit({
            "url": self._url,
            "filename": self._filename,
            "save_dir": save_dir,
            "category": category,
            "immediate": True,
        })
        self.accept()

    @pyqtSlot()
    def _on_download_later(self) -> None:
        """Queue download for later start."""
        save_dir = self._save_dir_input.text().strip() or self._save_dir
        category = self._category_combo.currentText()
        if category == "Auto":
            category = categorise(self._filename) if self._filename else "Other"

        self.download_accepted.emit({
            "url": self._url,
            "filename": self._filename,
            "save_dir": save_dir,
            "category": category,
            "immediate": False,
        })
        self.accept()

    # ── Styling ────────────────────────────────────────────────────────────

    @staticmethod
    def _create_separator() -> QFrame:
        """Create a horizontal separator."""
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setStyleSheet("color: #21262D;")
        return sep

    @staticmethod
    def _input_style() -> str:
        """Return input field style."""
        return """
            QLineEdit, QComboBox {
                padding: 10px 12px;
                border: 1px solid #30363D;
                border-radius: 6px;
                background: #0D1117;
                color: #E5E7EB;
                font-size: 13px;
            }
            QLineEdit:focus, QComboBox:focus { border-color: #58A6FF; }
            QComboBox::drop-down { border: none; }
        """

    @staticmethod
    def _combo_style() -> str:
        """Return combo box style."""
        return """
            QComboBox {
                padding: 10px 12px;
                border: 1px solid #30363D;
                border-radius: 6px;
                background: #0D1117;
                color: #E5E7EB;
                font-size: 13px;
            }
            QComboBox:focus { border-color: #58A6FF; }
            QComboBox::drop-down { border: none; }
            QComboBox::down-arrow {
                image: none;
                width: 0;
            }
        """

    @staticmethod
    def _primary_button_style() -> str:
        """Return primary button style."""
        return """
            QPushButton {
                padding: 10px 20px;
                border-radius: 6px;
                background: #1F6FEB;
                color: #FFFFFF;
                font-weight: 700;
                font-size: 13px;
                border: none;
            }
            QPushButton:hover { background: #388BFD; }
            QPushButton:pressed { background: #1158C7; }
            QPushButton:disabled { background: #21262D; color: #484F58; }
        """

    @staticmethod
    def _secondary_button_style() -> str:
        """Return secondary button style."""
        return """
            QPushButton {
                padding: 10px 16px;
                border: 1px solid #30363D;
                border-radius: 6px;
                background: #0D1117;
                color: #E5E7EB;
                font-size: 13px;
                border: 1px solid #30363D;
            }
            QPushButton:hover {
                border-color: #58A6FF;
                background: #0D1117;
                color: #58A6FF;
            }
            QPushButton:pressed { background: #161B22; }
        """
