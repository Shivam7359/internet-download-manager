"""
IDM UI — Add Download Dialog
==============================
A polished dialog for adding new downloads.

Features:
    • URL input with paste-from-clipboard button
    • Live URL metadata fetching (filename, size, resume support)
    • Save-location picker with category auto-detection
    • Priority selector (High / Normal / Low)
    • Chunk count slider
    • Optional hash field for verification
    • Custom headers / referer / cookies fields (collapsible)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional, Callable

from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, pyqtSlot, QThread, QSize,
)
from PyQt6.QtGui import QFont, QColor, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QSlider,
    QFileDialog, QGroupBox, QFrame, QWidget, QSizePolicy,
    QCheckBox, QTextEdit, QMessageBox, QProgressBar,
    QApplication,
)

from utils.categoriser import categorise

log = logging.getLogger("idm.ui.add_dialog")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ADD DOWNLOAD DIALOG                                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class AddDownloadDialog(QDialog):
    """
    Modal dialog for adding a new download.

    Signals:
        download_accepted(url, filename, save_dir, priority, category,
                          chunks, hash_expected, referer, cookies)
    """

    download_accepted = pyqtSignal(dict)

    def __init__(
        self,
        config: dict[str, Any],
        initial_url: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("Add New Download")
        self.setMinimumWidth(580)
        self.setModal(True)

        self._default_dir = config.get("general", {}).get(
            "download_directory",
            r"D:\idm down",
        )
        if not str(self._default_dir).strip():
            self._default_dir = r"D:\idm down"
        self._resolve_thread: Optional[ResolveThread] = None
        self._resolved_url: Optional[str] = None
        self._media_formats: list[dict[str, Any]] = []
        self._media_title: str = "video"
        self._stored_data: dict[str, Any] = {}  # For passing to FileInfoDialog

        self._build_ui()

        if initial_url:
            self._url_input.setText(initial_url)
            self._on_url_changed()
        else:
            self._try_paste_clipboard()

        self._url_input.setFocus()

    # ── UI Construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 20)

        # ── Title ──────────────────────────────────────────────────────────
        title = QLabel("Add Download")
        title.setStyleSheet(
            "font-size: 20px; font-weight: 700; color: #E5E7EB; "
            "background: transparent; margin-bottom: 2px;"
        )
        layout.addWidget(title)

        subtitle = QLabel("Enter the URL of the file you want to download")
        subtitle.setStyleSheet(
            "font-size: 12px; color: #8B949E; background: transparent;"
        )
        layout.addWidget(subtitle)

        layout.addWidget(self._create_separator())

        # ── URL Input ──────────────────────────────────────────────────────
        url_layout = QHBoxLayout()

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://example.com/file.zip")
        self._url_input.setStyleSheet("""
            QLineEdit {
                padding: 12px 16px;
                border: 2px solid #30363D;
                border-radius: 8px;
                background: #161B22;
                color: #E5E7EB;
                font-size: 14px;
                font-family: 'Consolas', 'Fira Code', monospace;
            }
            QLineEdit:focus { border-color: #58A6FF; }
        """)
        self._url_input.textChanged.connect(self._on_url_changed)
        url_layout.addWidget(self._url_input)

        paste_btn = QPushButton("Paste")
        paste_btn.setToolTip("Paste from clipboard")
        paste_btn.setFixedSize(72, 44)
        paste_btn.setStyleSheet("""
            QPushButton {
                border: 2px solid #30363D;
                border-radius: 8px;
                background: #21262D;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover { border-color: #58A6FF; background: #30363D; }
        """)
        paste_btn.clicked.connect(self._on_paste)
        url_layout.addWidget(paste_btn)

        layout.addLayout(url_layout)

        # ── URL Info Badge ─────────────────────────────────────────────────
        self._info_label = QLabel("")
        self._info_label.setStyleSheet(
            "font-size: 11px; color: #8B949E; background: transparent; "
            "padding: 2px 0;"
        )
        layout.addWidget(self._info_label)

        # ── File Details ───────────────────────────────────────────────────
        details_group = QGroupBox("File Details")
        details_group.setStyleSheet("""
            QGroupBox {
                font-weight: 600;
                font-size: 13px;
                color: #58A6FF;
                border: 1px solid #21262D;
                border-radius: 8px;
                margin-top: 12px;
                padding: 20px 16px 16px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 8px;
            }
        """)
        details_layout = QFormLayout(details_group)
        details_layout.setSpacing(10)
        details_layout.setHorizontalSpacing(12)
        details_layout.setVerticalSpacing(10)
        details_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Filename
        self._filename_input = QLineEdit()
        self._filename_input.setPlaceholderText("Auto-detected from URL")
        self._filename_input.setStyleSheet(self._input_style())
        self._filename_input.setMinimumHeight(36)
        details_layout.addRow("Filename:", self._filename_input)

        # Save location
        save_layout = QHBoxLayout()
        self._save_dir_input = QLineEdit(self._default_dir)
        self._save_dir_input.setStyleSheet(self._input_style())
        self._save_dir_input.setMinimumHeight(36)
        save_layout.addWidget(self._save_dir_input)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(90)
        browse_btn.setStyleSheet(self._button_style())
        browse_btn.clicked.connect(self._on_browse)
        save_layout.addWidget(browse_btn)
        details_layout.addRow("Save to:", save_layout)

        # Category
        self._category_combo = QComboBox()
        self._category_combo.addItems([
            "Auto", "Video", "Audio", "Image",
            "Document", "Software", "Archive", "Other",
        ])
        self._category_combo.setStyleSheet(self._combo_style())
        self._category_combo.setMinimumHeight(36)
        details_layout.addRow("Category:", self._category_combo)

        # Media quality (available for resolved media URLs)
        self._quality_combo = QComboBox()
        self._quality_combo.setStyleSheet(self._combo_style())
        self._quality_combo.setMinimumHeight(36)
        self._quality_combo.setEnabled(False)
        self._quality_combo.addItem("Auto (best available)")
        self._quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        details_layout.addRow("Quality:", self._quality_combo)

        # Priority
        self._priority_combo = QComboBox()
        self._priority_combo.addItems(["Normal", "High", "Low"])
        self._priority_combo.setStyleSheet(self._combo_style())
        self._priority_combo.setMinimumHeight(36)
        details_layout.addRow("Priority:", self._priority_combo)

        layout.addWidget(details_group)

        # ── Advanced Options (collapsible) ─────────────────────────────────
        self._advanced_toggle = QPushButton("Show Advanced Options")
        self._advanced_toggle.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                color: #8B949E;
                font-size: 12px;
                text-align: left;
                padding: 4px 0;
            }
            QPushButton:hover { color: #58A6FF; }
        """)
        self._advanced_toggle.setCheckable(True)
        self._advanced_toggle.toggled.connect(self._toggle_advanced)
        layout.addWidget(self._advanced_toggle)

        self._advanced_widget = QWidget()
        self._advanced_widget.setVisible(False)
        adv_layout = QFormLayout(self._advanced_widget)
        adv_layout.setSpacing(10)

        # Chunks
        chunk_layout = QHBoxLayout()
        self._chunk_slider = QSlider(Qt.Orientation.Horizontal)
        self._chunk_slider.setRange(3, 5)
        self._chunk_slider.setValue(
            max(3, min(int(self._config.get("general", {}).get("default_chunks", 5)), 5))
        )
        self._chunk_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 6px;
                background: #21262D;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 16px; height: 16px;
                margin: -5px 0;
                background: #58A6FF;
                border-radius: 8px;
            }
            QSlider::sub-page:horizontal {
                background: #1F6FEB;
                border-radius: 3px;
            }
        """)
        self._chunk_label = QLabel(str(self._chunk_slider.value()))
        self._chunk_label.setFixedWidth(30)
        self._chunk_label.setStyleSheet(
            "color: #58A6FF; font-weight: 700; background: transparent;"
        )
        self._chunk_slider.valueChanged.connect(
            lambda v: self._chunk_label.setText(str(v))
        )
        chunk_layout.addWidget(self._chunk_slider)
        chunk_layout.addWidget(self._chunk_label)
        adv_layout.addRow("Chunks:", chunk_layout)

        # Hash
        self._hash_input = QLineEdit()
        self._hash_input.setPlaceholderText("SHA-256 hash (optional)")
        self._hash_input.setStyleSheet(self._input_style())
        adv_layout.addRow("SHA-256:", self._hash_input)

        # Referer
        self._referer_input = QLineEdit()
        self._referer_input.setPlaceholderText("Referer URL (optional)")
        self._referer_input.setStyleSheet(self._input_style())
        adv_layout.addRow("Referer:", self._referer_input)

        # Cookies
        self._cookies_input = QLineEdit()
        self._cookies_input.setPlaceholderText("Cookies (optional)")
        self._cookies_input.setStyleSheet(self._input_style())
        adv_layout.addRow("Cookies:", self._cookies_input)

        layout.addWidget(self._advanced_widget)

        # ── Buttons ────────────────────────────────────────────────────────
        layout.addWidget(self._create_separator())

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(100)
        cancel_btn.setStyleSheet(self._button_style())
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        self._start_btn = QPushButton("Start Download")
        self._start_btn.setFixedWidth(160)
        self._start_btn.setStyleSheet("""
            QPushButton {
                padding: 10px 20px;
                border-radius: 8px;
                background: #1F6FEB;
                color: #FFFFFF;
                font-weight: 700;
                font-size: 13px;
                border: none;
            }
            QPushButton:hover { background: #388BFD; }
            QPushButton:pressed { background: #1158C7; }
            QPushButton:disabled { background: #21262D; color: #484F58; }
        """)
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start)
        btn_layout.addWidget(self._start_btn)

        layout.addLayout(btn_layout)

    # ── Slots ──────────────────────────────────────────────────────────────

    @pyqtSlot()
    def _on_url_changed(self) -> None:
        url = self._url_input.text().strip()
        from utils.media_extractor import MediaExtractor
        is_media = MediaExtractor.is_supported(url)
        valid = url.startswith(("http://", "https://", "ftp://", "magnet:")) or is_media

        self._start_btn.setEnabled(valid)

        if valid:
            self._info_label.setText("Valid URL detected")
            self._info_label.setStyleSheet(
                "font-size: 11px; color: #3FB950; background: transparent;"
            )

            if is_media:
                self._resolve_media(url)
            else:
                self._media_formats = []
                self._quality_combo.clear()
                self._quality_combo.addItem("Auto (best available)")
                self._quality_combo.setEnabled(False)
                self._resolved_url = None

                # Auto-detect filename from URL
                from core.network import extract_filename_from_url
                filename = extract_filename_from_url(url)
                if filename and not self._filename_input.text():
                    self._filename_input.setText(filename)

                # Auto-detect category
                if filename:
                    cat = categorise(filename)
                    if cat != "Other":
                        idx = self._category_combo.findText(cat)
                        if idx >= 0:
                            self._category_combo.setCurrentIndex(idx)
        elif url:
            self._info_label.setText("Enter a valid HTTP, HTTPS, FTP, magnet, or video URL")
            self._info_label.setStyleSheet(
                "font-size: 11px; color: #F85149; background: transparent;"
            )
        else:
            self._info_label.setText("")

    def _resolve_media(self, url: str) -> None:
        """Start a thread to resolve media URL via yt-dlp."""
        self._info_label.setText("Resolving video metadata...")
        self._info_label.setStyleSheet("color: #58A6FF;")

        if self._resolve_thread and self._resolve_thread.isRunning():
            self._resolve_thread.terminate()

        self._resolve_thread = ResolveThread(url, self._config)
        self._resolve_thread.resolved.connect(self._on_media_resolved)
        self._resolve_thread.failed.connect(self._on_media_failed)
        self._resolve_thread.start()

    @pyqtSlot(dict)
    def _on_media_resolved(self, info: dict) -> None:
        title = str(info.get("title") or "video")
        self._media_title = title
        self._info_label.setText(f"Resolved: {title[:50]}...")
        self._info_label.setStyleSheet("color: #3FB950;")

        formats = [f for f in info.get("formats", []) if f.get("url")]
        self._media_formats = formats
        self._quality_combo.clear()
        self._quality_combo.addItem("Auto (best available)")

        for f in formats:
            self._quality_combo.addItem(self._format_quality_label(f))

        self._quality_combo.setEnabled(bool(formats))

        if not self._filename_input.text():
            ext = self._preferred_ext(info)
            safe_title = title.replace("/", "_").replace("\\", "_")
            self._filename_input.setText(f"{safe_title}.{ext}")

        idx = self._category_combo.findText("Video")
        if idx >= 0:
            self._category_combo.setCurrentIndex(idx)

        # Default to best available media URL for Auto mode.
        self._resolved_url = info.get("url")
        if not self._resolved_url and formats:
            self._resolved_url = formats[-1].get("url")

    @pyqtSlot(str)
    def _on_media_failed(self, error: str) -> None:
        self._info_label.setText(f"Extraction failed: {error}")
        self._info_label.setStyleSheet("color: #F85149;")
        self._media_formats = []
        self._quality_combo.clear()
        self._quality_combo.addItem("Auto (best available)")
        self._quality_combo.setEnabled(False)
        self._resolved_url = None

    @pyqtSlot(int)
    def _on_quality_changed(self, index: int) -> None:
        if index <= 0:
            # Auto mode keeps the best URL selected by resolver.
            return

        format_idx = index - 1
        if 0 <= format_idx < len(self._media_formats):
            selected = self._media_formats[format_idx]
            selected_url = selected.get("url")
            if selected_url:
                self._resolved_url = str(selected_url)

            current_name = self._filename_input.text().strip()
            if current_name:
                stem = Path(current_name).stem
                ext = str(selected.get("ext") or "").strip()
                if ext:
                    self._filename_input.setText(f"{stem}.{ext}")

    @pyqtSlot()
    def _on_paste(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard:
            text = clipboard.text().strip()
            if text:
                self._url_input.setText(text)

    @pyqtSlot()
    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select Download Directory", self._save_dir_input.text()
        )
        if directory:
            self._save_dir_input.setText(directory)

    @pyqtSlot(bool)
    def _toggle_advanced(self, checked: bool) -> None:
        self._advanced_widget.setVisible(checked)
        self._advanced_toggle.setText(
            "Hide Advanced Options" if checked else "Show Advanced Options"
        )
        self.adjustSize()

    @pyqtSlot()
    def _on_start(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            return

        filename = self._filename_input.text().strip()
        save_dir = self._save_dir_input.text().strip() or self._default_dir
        category = self._category_combo.currentText()
        if category == "Auto":
            category = categorise(filename) if filename else "Other"
        priority = self._priority_combo.currentText().lower()

        current_url = url
        if hasattr(self, "_resolved_url") and self._resolved_url:
            current_url = self._resolved_url

        # Store advanced options for later
        self._stored_data = {
            "url": current_url,
            "original_url": url,
            "filename": filename,
            "save_dir": save_dir,
            "save_path": str(Path(save_dir) / filename) if filename else "",
            "category": category,
            "priority": priority,
            "chunks": self._chunk_slider.value(),
            "hash_expected": self._hash_input.text().strip(),
            "referer": self._referer_input.text().strip(),
            "cookies": self._cookies_input.text().strip(),
        }

        # Show file info preview dialog
        self._show_file_info_preview(current_url, filename, save_dir)

    def _show_file_info_preview(self, url: str, filename: str = "", save_dir: str = "") -> None:
        """Show file info preview before download."""
        from ui.file_info_dialog import FileInfoDialog

        dialog = FileInfoDialog(
            url=url,
            filename=filename,
            save_dir=save_dir,
            config=self._config,
            parent=self,
        )

        def _on_file_info_accepted(file_info_data: dict) -> None:
            # Merge file info data with stored data
            final_data = self._stored_data.copy()
            if file_info_data.get("filename"):
                final_data["filename"] = file_info_data["filename"]
            if file_info_data.get("save_dir"):
                final_data["save_dir"] = file_info_data["save_dir"]
            if file_info_data.get("category"):
                final_data["category"] = file_info_data["category"]
            final_data["start_immediately"] = bool(file_info_data.get("immediate", True))

            filename = str(final_data.get("filename", "")).strip()
            save_dir_selected = str(final_data.get("save_dir", save_dir)).strip()
            if filename and save_dir_selected:
                final_data["save_path"] = str(Path(save_dir_selected) / filename)

            self.download_accepted.emit(final_data)
            self.accept()

        dialog.download_accepted.connect(_on_file_info_accepted)
        dialog.exec()

    def _try_paste_clipboard(self) -> None:
        """Auto-paste URL from clipboard if it looks downloadable."""
        clipboard = QApplication.clipboard()
        if clipboard:
            text = clipboard.text().strip()
            if text and text.startswith(("http://", "https://", "ftp://")):
                self._url_input.setText(text)

    @staticmethod
    def _preferred_ext(info: dict[str, Any]) -> str:
        ext = str(info.get("ext") or "").strip()
        if ext:
            return ext
        formats = info.get("formats", []) or []
        if formats:
            fmt_ext = str(formats[-1].get("ext") or "").strip()
            if fmt_ext:
                return fmt_ext
        return "mp4"

    @staticmethod
    def _format_quality_label(fmt: dict[str, Any]) -> str:
        from core.network import format_size

        quality = str(fmt.get("resolution") or "").strip()
        if not quality:
            height = fmt.get("height")
            quality = f"{height}p" if height else "audio"

        ext = str(fmt.get("ext") or "?").upper()
        note = str(fmt.get("note") or "").strip()
        size = int(fmt.get("filesize") or 0)
        size_text = format_size(size) if size > 0 else "unknown size"

        parts = [quality, ext]
        if note:
            parts.append(note)
        parts.append(size_text)
        return " • ".join(parts)

    @staticmethod
    def _input_style() -> str:
        return """
            QLineEdit {
                padding: 8px 12px;
                border: 1px solid #30363D;
                border-radius: 6px;
                background: #161B22;
                color: #E5E7EB;
                font-size: 13px;
            }
            QLineEdit:focus { border-color: #58A6FF; }
        """

    @staticmethod
    def _button_style() -> str:
        return """
            QPushButton {
                padding: 8px 16px;
                border: 1px solid #30363D;
                border-radius: 6px;
                background: #21262D;
                color: #C9D1D9;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover { background: #30363D; border-color: #58A6FF; }
        """

    @staticmethod
    def _combo_style() -> str:
        return """
            QComboBox {
                padding: 8px 12px;
                border: 1px solid #30363D;
                border-radius: 6px;
                background: #161B22;
                color: #E5E7EB;
                font-size: 13px;
            }
            QComboBox:hover { border-color: #58A6FF; }
            QComboBox QAbstractItemView {
                background: #161B22;
                border: 1px solid #30363D;
                selection-background-color: #1F6FEB;
            }
        """

    @staticmethod
    def _create_separator() -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: #21262D; max-height: 1px;")
        return sep


class ResolveThread(QThread):
    """Thread for running yt-dlp resolution."""
    resolved = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, url: str, config: dict) -> None:
        super().__init__()
        self.url = url
        self.config = config

    def run(self) -> None:
        try:
            import asyncio
            from utils.media_extractor import MediaExtractor

            # Run the extraction in a local event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            extractor = MediaExtractor(self.config)

            info = loop.run_until_complete(extractor.get_info(self.url))
            if info:
                # Find best direct URL
                best_url = info.get("url")
                if not best_url and info.get("formats"):
                    best_url = info["formats"][-1].get("url")

                info["url"] = best_url
                self.resolved.emit(info)
            else:
                self.failed.emit("No metadata found")
        except Exception as e:
            self.failed.emit(str(e))
