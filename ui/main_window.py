"""
IDM UI — Main Window
=====================
The primary application window containing the download queue table,
toolbar, status bar, and menu system.

Architecture:
    • MainWindow (QMainWindow) — top-level frame
    • DownloadTableModel (QAbstractTableModel) — data model for queue
    • DownloadDelegate (QStyledItemDelegate) — custom progress bar rendering
    • Toolbar with Add / Pause / Resume / Cancel / Settings actions
    • Status bar showing active downloads, speed, and scheduler state

All UI updates are dispatched via Qt signals from the engine thread.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from PyQt6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
    QTimer, pyqtSignal, pyqtSlot, QSize, QRect, QUrl,
    QPoint, QPointF, QItemSelectionModel,
)
from PyQt6.QtGui import (
    QAction, QColor, QPainter, QBrush,
    QLinearGradient, QDesktopServices, QKeySequence, QCloseEvent,
    QIcon, QFont,
)
from PyQt6.QtWidgets import (
    QMainWindow, QTableView, QHeaderView, QToolBar, QStatusBar,
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QLineEdit,
    QComboBox, QMenu, QMessageBox, QPushButton,
    QFileDialog, QApplication,
    QStyledItemDelegate, QStyleOptionViewItem, QSplitter,
    QFrame, QSizePolicy, QAbstractItemView, QTreeWidget, QTreeWidgetItem,
    QScrollArea,
)

from core.network import format_speed, format_size, format_eta
log = logging.getLogger("idm.ui.main_window")

# ── Column definitions ─────────────────────────────────────────────────────────
COLUMNS = [
    ("filename", "Filename", 280),
    ("file_size", "Size", 90),
    ("progress", "Progress", 160),
    ("chunks", "Chunks", 70),
    ("speed", "Speed", 100),
    ("eta", "ETA", 80),
    ("status", "Status", 90),
    ("priority", "Priority", 70),
    ("category", "Category", 80),
    ("date_added", "Date Added", 140),
]

COL_FILENAME = 0
COL_SIZE = 1
COL_PROGRESS = 2
COL_CHUNKS = 3
COL_SPEED = 4
COL_ETA = 5
COL_STATUS = 6
COL_PRIORITY = 7
COL_CATEGORY = 8
COL_DATE = 9

# Status → display color mapping
STATUS_COLORS = {
    "queued": "#6B7280",
    "downloading": "#3B82F6",
    "paused": "#F59E0B",
    "completed": "#10B981",
    "failed": "#EF4444",
    "cancelled": "#9CA3AF",
    "merging": "#8B5CF6",
    "verifying": "#06B6D4",
}


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DOWNLOAD TABLE MODEL                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class DownloadTableModel(QAbstractTableModel):
    """
    Qt table model backed by download records.

    Each row is a dict with download data.  The model supports
    efficient partial updates via ``update_download()`` which only
    emits ``dataChanged`` for the affected row.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._downloads: list[dict[str, Any]] = []
        self._id_to_row: dict[str, int] = {}
        self._speeds: dict[str, float] = {}
        self._etas: dict[str, float] = {}
        self._chunks: dict[str, dict[str, int]] = {}  # id -> {completed, total}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._downloads)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][1]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._downloads):
            return None

        dl = self._downloads[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_data(dl, col)

        if role == Qt.ItemDataRole.ForegroundRole and col == COL_STATUS:
            color = STATUS_COLORS.get(dl.get("status", ""), "#FFFFFF")
            return QColor(color)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (COL_SIZE, COL_CHUNKS, COL_SPEED, COL_ETA, COL_PRIORITY):
                return Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.ToolTipRole:
            error_message = str(dl.get("error_message", "")).strip()
            if error_message and dl.get("status") == "failed":
                return error_message
            if col == COL_FILENAME:
                save_path = str(dl.get("save_path", "")).strip()
                if save_path:
                    return save_path
            return self._display_data(dl, col)

        if role == Qt.ItemDataRole.UserRole:
            return dl  # full data dict

        if role == Qt.ItemDataRole.UserRole + 1:
            # Progress value 0-100
            return dl.get("progress_percent", 0.0)

        if role == Qt.ItemDataRole.UserRole + 2:
            return dl.get("status", "")

        return None

    def _display_data(self, dl: dict[str, Any], col: int) -> str:
        dl_id = str(dl.get("id", ""))

        if col == COL_FILENAME:
            return self._friendly_filename(dl)
        elif col == COL_SIZE:
            size = dl.get("file_size", -1)
            return format_size(size)
        elif col == COL_PROGRESS:
            pct = dl.get("progress_percent", 0.0)
            downloaded = dl.get("downloaded_bytes", 0)
            total = dl.get("file_size", -1)
            if total > 0:
                return f"{pct:.1f}% ({format_size(downloaded)})"
            return f"{format_size(downloaded)}"
        elif col == COL_CHUNKS:
            chunks_info = self._chunks.get(dl_id, {})
            completed = chunks_info.get("completed", 0)
            total = chunks_info.get("total", dl.get("chunks_count", 0))
            if total > 1:
                return f"{completed}/{total}"
            if total == 1:
                return "1/1"
            return "—"
        elif col == COL_SPEED:
            return format_speed(self._speeds.get(dl_id, 0.0))
        elif col == COL_ETA:
            eta = self._etas.get(dl_id, 0.0)
            return format_eta(eta) if eta > 0 else "—"
        elif col == COL_STATUS:
            return str(dl.get("status", "unknown")).capitalize()
        elif col == COL_PRIORITY:
            return str(dl.get("priority", "normal")).capitalize()
        elif col == COL_CATEGORY:
            return str(dl.get("category", "Other"))
        elif col == COL_DATE:
            date_str = dl.get("date_added", "")
            date_text = str(date_str) if date_str else ""
            return date_text[:19].replace("T", " ") if date_text else ""

        return ""

    @staticmethod
    def _looks_like_generated_id(value: str) -> bool:
        if not value:
            return False
        compact = value.replace("-", "")
        if len(compact) in (32, 36) and all(c in "0123456789abcdefABCDEF-" for c in value):
            return True
        return False

    def _friendly_filename(self, dl: dict[str, Any]) -> str:
        """Prefer readable file names over UUID/hash placeholders."""
        raw = str(dl.get("filename", "")).strip()
        if raw and not self._looks_like_generated_id(raw):
            # Some historical records store full path in filename.
            # Show only basename in the Filename column.
            if "\\" in raw or "/" in raw:
                return Path(raw).name or raw
            return raw

        save_path = str(dl.get("save_path", "")).strip()
        if save_path:
            name = Path(save_path).name.strip()
            if name:
                return name

        url = str(dl.get("url", "")).strip()
        if url:
            parsed = urlparse(url)
            leaf = Path(unquote(parsed.path)).name.strip()
            if leaf:
                return leaf

        if raw:
            return raw
        return "Unknown"

    # ── Data manipulation ──────────────────────────────────────────────────

    def set_downloads(self, downloads: list[dict[str, Any]]) -> None:
        """Replace all downloads (initial load)."""
        self.beginResetModel()
        self._downloads = downloads
        self._id_to_row = {dl["id"]: i for i, dl in enumerate(downloads)}
        # Reconciliation snapshots don't carry real-time speed/ETA samples.
        # Drop cached values to prevent stale numbers from persisting.
        self._speeds.clear()
        self._etas.clear()
        self._chunks.clear()
        self.endResetModel()

    def add_download(self, dl: dict[str, Any]) -> None:
        """Add a single download to the top."""
        row = 0
        self.beginInsertRows(QModelIndex(), row, row)
        self._downloads.insert(row, dl)
        self._rebuild_index()
        self.endInsertRows()

    def update_download(self, dl_id: str, updates: dict[str, Any]) -> None:
        """Update specific fields for a download row."""
        row = self._id_to_row.get(dl_id)
        if row is None:
            return

        self._downloads[row].update(updates)
        left = self.index(row, 0)
        right = self.index(row, len(COLUMNS) - 1)
        self.dataChanged.emit(left, right)

    def update_chunks(self, dl_id: str, completed: int, total: int) -> None:
        """Update chunk progress for a download."""
        self._chunks[dl_id] = {"completed": completed, "total": total}
        row = self._id_to_row.get(dl_id)
        if row is not None:
            idx = self.index(row, COL_CHUNKS)
            self.dataChanged.emit(idx, idx)

    def update_progress(
        self, dl_id: str, downloaded: int, total: int,
        speed: float, eta: float,
    ) -> None:
        """Update progress, speed, and ETA for a download."""
        row = self._id_to_row.get(dl_id)
        if row is None:
            return

        dl = self._downloads[row]
        dl["downloaded_bytes"] = downloaded
        if total > 0:
            dl["file_size"] = total
            dl["progress_percent"] = (downloaded / total) * 100.0
        self._speeds[dl_id] = speed
        self._etas[dl_id] = eta

        # Emit a single dataChanged for the bounding box of updated columns
        left = self.index(row, min(COL_PROGRESS, COL_SPEED, COL_ETA))
        right = self.index(row, max(COL_PROGRESS, COL_SPEED, COL_ETA))
        self.dataChanged.emit(left, right)

    def update_status(self, dl_id: str, status: str, error_message: str = "") -> None:
        """Update the status of a download."""
        updates: dict[str, Any] = {"status": status}
        if error_message:
            updates["error_message"] = error_message
        elif status != "failed":
            updates["error_message"] = ""
        self.update_download(dl_id, updates)
        if status not in ("downloading", "merging", "verifying"):
            self._speeds.pop(dl_id, None)
            self._etas.pop(dl_id, None)

    def remove_download(self, dl_id: str) -> None:
        """Remove a download from the model."""
        row = self._id_to_row.get(dl_id)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._downloads.pop(row)
        self._speeds.pop(dl_id, None)
        self._etas.pop(dl_id, None)
        self._chunks.pop(dl_id, None)
        self._rebuild_index()
        self.endRemoveRows()

    def get_download_id(self, row: int) -> Optional[str]:
        """Get the download ID for a row."""
        if 0 <= row < len(self._downloads):
            return self._downloads[row].get("id")
        return None

    def get_download(self, dl_id: str) -> Optional[dict[str, Any]]:
        """Return a download row dict by ID."""
        row = self._id_to_row.get(dl_id)
        if row is None:
            return None
        return self._downloads[row]

    def get_row_for_id(self, dl_id: str) -> Optional[int]:
        """Return the source-model row for a download ID."""
        return self._id_to_row.get(dl_id)

    def _rebuild_index(self) -> None:
        self._id_to_row = {dl["id"]: i for i, dl in enumerate(self._downloads)}


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  PROGRESS BAR DELEGATE                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class ProgressDelegate(QStyledItemDelegate):
    """Custom delegate that renders the progress column as a gradient bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._brushes = {}

    def paint(
        self, painter: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        if painter is None:
            return
        if index.column() != COL_PROGRESS:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect.adjusted(4, 4, -4, -4)
        progress = index.data(Qt.ItemDataRole.UserRole + 1) or 0.0
        status = index.data(Qt.ItemDataRole.UserRole + 2) or ""
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""

        # Background track
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#161B22"))
        painter.drawRoundedRect(rect, 5, 5)

        # Progress fill with gradient
        if progress > 0:
            fill_width = int(rect.width() * min(progress, 100.0) / 100.0)
            fill_rect = QRect(rect.x(), rect.y(), fill_width, rect.height())

            cache_key = (fill_width, fill_rect.height(), status)
            if cache_key in self._brushes:
                brush = self._brushes[cache_key]
            else:
                gradient = QLinearGradient(
                    QPointF(fill_rect.topLeft()),
                    QPointF(fill_rect.topRight()),
                )
                if status == "failed":
                    gradient.setColorAt(0, QColor("#DA3633"))
                    gradient.setColorAt(1, QColor("#F85149"))
                elif status == "paused":
                    gradient.setColorAt(0, QColor("#D29922"))
                    gradient.setColorAt(1, QColor("#E3B341"))
                elif progress >= 100 or status == "completed":
                    gradient.setColorAt(0, QColor("#238636"))
                    gradient.setColorAt(1, QColor("#3FB950"))
                else:
                    gradient.setColorAt(0, QColor("#1F6FEB"))
                    gradient.setColorAt(1, QColor("#58A6FF"))
                brush = QBrush(gradient)
                
                if len(self._brushes) > 300:
                    self._brushes.clear()
                self._brushes[cache_key] = brush

            painter.setBrush(brush)
            painter.drawRoundedRect(fill_rect, 4, 4)

        # Text overlay
        painter.setPen(QColor("#E6EDF3"))
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        painter.restore()

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QSize:
        if index.column() == COL_PROGRESS:
            return QSize(160, 30)
        return super().sizeHint(option, index)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  MAIN WINDOW                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class MainWindow(QMainWindow):
    """
    The primary IDM application window.

    Signals:
        add_download_requested — emitted when user adds a URL
        pause_requested — emitted with download_id
        resume_requested — emitted with download_id
        cancel_requested — emitted with download_id
    """

    # Signals for engine communication (cross-thread)
    add_download_requested = pyqtSignal(dict)            # full data dict
    pause_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    cancel_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str, bool)             # id, delete_file
    settings_changed = pyqtSignal(dict)

    def __init__(
        self,
        config: dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._pairing_code_raw = ""

        self.setWindowTitle("Internet Download Manager")
        self.setMinimumSize(960, 600)
        self.resize(1200, 700)

        # ── Data model ─────────────────────────────────────────────────
        self._model = DownloadTableModel(self)
        self._proxy_model = QSortFilterProxyModel(self)
        self._proxy_model.setSourceModel(self._model)
        self._proxy_model.setFilterCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self._proxy_model.setFilterKeyColumn(COL_FILENAME)

        # ── Build UI ───────────────────────────────────────────────────
        self._build_menubar()
        self._build_toolbar()
        self._build_central_widget()
        self._build_statusbar()

        # Theme is applied globally via ui.theme — no inline override needed.

        # ── Refresh timer ──────────────────────────────────────────────
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start(1000)  # 1 second

        log.info("MainWindow created")


    @property
    def model(self) -> DownloadTableModel:
        return self._model

    # ── Menu Bar ───────────────────────────────────────────────────────────

    def _build_menubar(self) -> None:
        menubar = self.menuBar()
        assert menubar is not None

        # File menu
        file_menu = menubar.addMenu("&File")
        assert file_menu is not None

        add_action = QAction("&Add URL…", self)
        add_action.setShortcut(QKeySequence("Ctrl+N"))
        add_action.triggered.connect(self._on_add_url)
        file_menu.addAction(add_action)

        file_menu.addSeparator()

        import_action = QAction("&Import List…", self)
        import_action.triggered.connect(self._on_import_list)
        file_menu.addAction(import_action)

        export_action = QAction("&Export List…", self)
        export_action.triggered.connect(self._on_export_list)
        file_menu.addAction(export_action)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Downloads menu
        dl_menu = menubar.addMenu("&Downloads")
        assert dl_menu is not None

        pause_all = QAction("Pause &All", self)
        pause_all.triggered.connect(self._on_pause_all)
        dl_menu.addAction(pause_all)

        cancel_all = QAction("&Cancel All", self)
        cancel_all.triggered.connect(self._on_cancel_all)
        dl_menu.addAction(cancel_all)

        start_all = QAction("&Start All", self)
        start_all.triggered.connect(self._on_start_all)
        dl_menu.addAction(start_all)

        resume_all = QAction("&Resume All", self)
        resume_all.triggered.connect(self._on_resume_all)
        dl_menu.addAction(resume_all)

        dl_menu.addSeparator()

        delete_all = QAction("&Delete All", self)
        delete_all.triggered.connect(self._on_delete_all)
        dl_menu.addAction(delete_all)

        dl_menu.addSeparator()

        clear_done = QAction("&Clear Completed", self)
        clear_done.triggered.connect(self._on_clear_completed)
        dl_menu.addAction(clear_done)

        # View menu
        view_menu = menubar.addMenu("&View")
        assert view_menu is not None

        self._filter_actions: dict[str, QAction] = {}
        for status in ["All", "Downloading", "Queued", "Paused", "Completed", "Failed"]:
            action = QAction(status, self)
            action.setCheckable(True)
            action.setChecked(status == "All")
            action.triggered.connect(lambda checked, s=status: self._on_filter_status(s))
            view_menu.addAction(action)
            self._filter_actions[status] = action

        # Help menu
        help_menu = menubar.addMenu("&Help")
        assert help_menu is not None

        settings_action = QAction("&Settings…", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self._on_settings)
        help_menu.addAction(settings_action)

        help_menu.addSeparator()

        analytics_action = QAction("Download &Analytics…", self)
        analytics_action.triggered.connect(self._on_show_analytics)
        help_menu.addAction(analytics_action)

        help_menu.addSeparator()

        about_action = QAction("&About", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    # ── Toolbar ────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main Toolbar", self)
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        # Add URL button
        self._btn_add = QAction("\u2795  Add URL", self)
        self._btn_add.setToolTip("Add a new download (Ctrl+N)")
        self._btn_add.triggered.connect(self._on_add_url)
        toolbar.addAction(self._btn_add)

        toolbar.addSeparator()

        # Pause / Resume / Cancel
        self._btn_pause = QAction("\u23F8  Pause", self)
        self._btn_pause.setToolTip("Pause selected download")
        self._btn_pause.triggered.connect(self._on_pause_selected)
        toolbar.addAction(self._btn_pause)

        self._btn_resume = QAction("\u25B6  Resume", self)
        self._btn_resume.setToolTip("Resume selected download")
        self._btn_resume.triggered.connect(self._on_resume_selected)
        toolbar.addAction(self._btn_resume)

        self._btn_cancel = QAction("\u2716  Cancel", self)
        self._btn_cancel.setToolTip("Cancel selected download")
        self._btn_cancel.triggered.connect(self._on_cancel_selected)
        toolbar.addAction(self._btn_cancel)

        self._btn_delete = QAction("\U0001F5D1  Delete", self)
        self._btn_delete.setToolTip("Delete selected download")
        self._btn_delete.triggered.connect(self._on_delete_selected)
        toolbar.addAction(self._btn_delete)

        toolbar.addSeparator()

        self._btn_pause_all = QAction("\u23F8\u23F8  Pause All", self)
        self._btn_pause_all.triggered.connect(self._on_pause_all)
        toolbar.addAction(self._btn_pause_all)

        self._btn_cancel_all = QAction("\u2716\u2716  Cancel All", self)
        self._btn_cancel_all.triggered.connect(self._on_cancel_all)
        toolbar.addAction(self._btn_cancel_all)

        self._btn_start_all = QAction("\u25B6\u25B6  Start All", self)
        self._btn_start_all.triggered.connect(self._on_start_all)
        toolbar.addAction(self._btn_start_all)

        self._btn_delete_all = QAction("\U0001F5D1\U0001F5D1  Delete All", self)
        self._btn_delete_all.triggered.connect(self._on_delete_all)
        toolbar.addAction(self._btn_delete_all)

        toolbar.addSeparator()

        # Settings button
        self._btn_settings = QAction("\u2699  Settings", self)
        self._btn_settings.setToolTip("Open settings (Ctrl+,)")
        self._btn_settings.triggered.connect(self._on_settings)
        toolbar.addAction(self._btn_settings)

        toolbar.addSeparator()

        # Search bar
        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText("\U0001F50D  Search downloads...")
        self._search_input.setFixedWidth(260)
        self._search_input.setFixedHeight(36)
        self._search_input.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self._search_input)

    # ── Central Widget ─────────────────────────────────────────────────────

    def _build_central_widget(self) -> None:
        """
        Build the main content area with sidebar navigation and download list.
        
        Layout:
            Left sidebar (categories) | Main area (dashboard + table)
        """
        central = QWidget(self)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── LEFT SIDEBAR ───────────────────────────────────────────────────
        self._sidebar = self._build_sidebar()
        main_layout.addWidget(self._sidebar)

        # ── VERTICAL DIVIDER ───────────────────────────────────────────────
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color: #21262D;")
        divider.setFixedWidth(1)
        main_layout.addWidget(divider)

        # ── RIGHT PANEL (dashboard + table) ────────────────────────────────
        right_panel = QWidget(central)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(10, 8, 10, 8)
        right_layout.setSpacing(8)

        # Dashboard strip
        right_layout.addWidget(self._build_dashboard_strip())

        # Download table (no speed graph splitter)
        self._table = QTableView()
        self._table.setModel(self._proxy_model)
        self._table.setItemDelegate(ProgressDelegate(self._table))
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.setShowGrid(False)
        
        # Table inherits from global theme — no inline override needed.
        
        vertical_header = self._table.verticalHeader()
        assert vertical_header is not None
        vertical_header.setVisible(False)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.doubleClicked.connect(self._on_table_double_clicked)

        # Column sizing
        header = self._table.horizontalHeader()
        assert header is not None
        for i, (_, _, width) in enumerate(COLUMNS):
            header.resizeSection(i, width)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(
            COL_FILENAME, QHeaderView.ResizeMode.Stretch
        )

        # Row height
        vertical_header = self._table.verticalHeader()
        assert vertical_header is not None
        vertical_header.setDefaultSectionSize(40)

        right_layout.addWidget(self._table)

        main_layout.addWidget(right_panel, 1)
        self.setCentralWidget(central)

    def _build_sidebar(self) -> QWidget:
        """
        Build the left sidebar with category navigation.
        
        Shows categories for quick filtering of downloads.
        """
        sidebar = QFrame()
        sidebar.setStyleSheet("""
            QFrame {
                background-color: #0D1117;
                border: none;
            }
        """)
        sidebar.setFixedWidth(200)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header ─────────────────────────────────────────────────────────
        header_frame = QFrame()
        header_frame.setStyleSheet("background-color: #161B22; border-bottom: 1px solid #21262D;")
        header_frame.setFixedHeight(50)
        header_layout = QVBoxLayout(header_frame)
        header_layout.setContentsMargins(12, 10, 12, 10)

        title_label = QLabel("Categories")
        title_label.setStyleSheet("color: #E6EDF3; font-weight: 700; font-size: 13px;")
        header_layout.addWidget(title_label)

        layout.addWidget(header_frame)

        # ── Categories Tree ────────────────────────────────────────────────
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setStyleSheet("""
            QTreeWidget {
                background-color: #0D1117;
                border: none;
                color: #C9D1D9;
                font-size: 12px;
            }
            QTreeWidget::item {
                padding: 8px 6px;
                border: none;
                border-radius: 6px;
                margin: 1px 6px;
            }
            QTreeWidget::item:selected {
                background-color: rgba(31, 111, 235, 0.15);
                color: #58A6FF;
            }
            QTreeWidget::item:hover:!selected {
                background-color: rgba(88, 166, 255, 0.06);
            }
        """)

        # Disable default selection
        tree.setUniformRowHeights(True)

        # Category icon map — emoji prefix in labels
        _ICONS = {
            "all": "\U0001F4E5",
            "status:downloading": "\u2B07",
            "status:queued": "\u23F3",
            "status:paused": "\u23F8",
            "status:completed": "\u2705",
            "status:failed": "\u274C",
            "category:Video": "\U0001F3AC",
            "category:Audio": "\U0001F3B5",
            "category:Image": "\U0001F5BC",
            "category:Document": "\U0001F4C4",
            "category:Software": "\u2699",
            "category:Archive": "\U0001F4E6",
            "category:Other": "\U0001F4CB",
            "status:not_completed": "\U0001F4DD",
        }

        categories = [
            ("All Downloads", "all"),
            (None, None),  # Separator
            ("By Status", None, [
                ("Downloading", "status:downloading"),
                ("Queued", "status:queued"),
                ("Paused", "status:paused"),
                ("Completed", "status:completed"),
                ("Failed", "status:failed"),
            ]),
            (None, None),  # Separator
            ("By Type", None, [
                ("Video", "category:Video"),
                ("Audio", "category:Audio"),
                ("Image", "category:Image"),
                ("Document", "category:Document"),
                ("Software", "category:Software"),
                ("Archive", "category:Archive"),
                ("Other", "category:Other"),
            ]),
            (None, None),  # Separator
            ("Other", None, [
                ("Unfinished", "status:not_completed"),
                ("Finished", "status:completed"),
            ]),
        ]

        root = tree.invisibleRootItem()

        for category_data in categories:
            if category_data[0] is None:
                # Separator
                sep = QTreeWidgetItem(root)
                sep.setText(0, "")
                sep.setFlags(sep.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                sep.setSizeHint(0, QSize(0, 8))
            elif len(category_data) == 2:
                # Single item
                label, tag = category_data
                icon = _ICONS.get(tag, "")
                item = QTreeWidgetItem(root)
                item.setText(0, f"{icon}  {label}" if icon else label)
                item.setData(0, Qt.ItemDataRole.UserRole, tag)

                # Styling
                if tag == "all":
                    font = item.font(0)
                    font.setBold(True)
                    item.setFont(0, font)
                    item.setForeground(0, QColor("#58A6FF"))
            else:
                # Parent with children
                parent_label = category_data[0]
                parent = QTreeWidgetItem(root)
                parent.setText(0, parent_label)
                parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsDropEnabled)

                font = parent.font(0)
                font.setBold(True)
                font.setPointSize(9)
                parent.setFont(0, font)
                parent.setForeground(0, QColor("#8B949E"))
                parent.setExpanded(True)

                # Add child items
                for child_data in category_data[2]:
                    child_label, child_tag = child_data
                    icon = _ICONS.get(child_tag, "")
                    child = QTreeWidgetItem(parent)
                    child.setText(0, f"{icon}  {child_label}" if icon else child_label)
                    child.setData(0, Qt.ItemDataRole.UserRole, child_tag)

        tree.itemClicked.connect(self._on_category_selected)
        layout.addWidget(tree, 1)

        # ── Footer ─────────────────────────────────────────────────────────
        footer = QFrame()
        footer.setStyleSheet("background-color: #161B22; border-top: 1px solid #21262D;")
        footer.setFixedHeight(50)
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(12, 8, 12, 8)

        clear_filters_btn = QLabel("\U0001F504  Clear Filters")
        clear_filters_btn.setStyleSheet("""
            QLabel {
                color: #58A6FF;
                font-size: 11px;
                font-weight: 600;
                padding: 6px;
                border-radius: 6px;
            }
            QLabel:hover {
                background-color: rgba(88, 166, 255, 0.08);
            }
        """)
        clear_filters_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_filters_btn.mousePressEvent = lambda e: self._on_clear_filters()

        footer_layout.addWidget(clear_filters_btn)
        layout.addWidget(footer)

        return sidebar

    # Icons are now embedded directly in tree item text — see _build_sidebar().

    @pyqtSlot(QTreeWidgetItem)
    def _on_category_selected(self, item: QTreeWidgetItem) -> None:
        """Handle category selection in sidebar."""
        tag = item.data(0, Qt.ItemDataRole.UserRole)
        if not tag:
            return

        # Clear existing filters
        self._proxy_model.setFilterFixedString("")

        if tag == "all":
            # Show all
            self._proxy_model.setFilterFixedString("")
        elif tag.startswith("status:"):
            # Filter by status
            status = tag.split(":", 1)[1]
            self._proxy_model.setFilterKeyColumn(COL_STATUS)
            self._proxy_model.setFilterFixedString(status)
        elif tag.startswith("category:"):
            # Filter by category
            category = tag.split(":", 1)[1]
            self._proxy_model.setFilterKeyColumn(COL_CATEGORY)
            self._proxy_model.setFilterFixedString(category)

    @pyqtSlot()
    def _on_clear_filters(self) -> None:
        """Clear all filters and show all downloads."""
        self._proxy_model.setFilterFixedString("")

    def _build_dashboard_strip(self) -> QWidget:
        """Create a compact live overview strip above the downloads table."""
        strip = QFrame(self)
        strip.setObjectName("card")
        strip.setStyleSheet(
            "QFrame#card {"
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            "stop:0 rgba(22,27,34,0.9), stop:1 rgba(28,35,51,0.9));"
            "border: 1px solid #21262D;"
            "border-radius: 12px;"
            "}"
            "QLabel { background: transparent; }"
        )

        row = QHBoxLayout(strip)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)

        self._kpi_active = self._make_kpi_chip("Active", "0", "#60A5FA")
        self._kpi_queued = self._make_kpi_chip("Queued", "0", "#F59E0B")
        self._kpi_completed = self._make_kpi_chip("Completed", "0", "#34D399")
        self._kpi_failed = self._make_kpi_chip("Failed", "0", "#F87171")

        for chip in (
            self._kpi_active,
            self._kpi_queued,
            self._kpi_completed,
            self._kpi_failed,
        ):
            row.addWidget(chip)

        row.addStretch()

        self._bridge_badge = QLabel("Bridge Offline")
        self._bridge_badge.setStyleSheet(
            "padding: 6px 10px; border-radius: 12px; "
            "font-weight: 700; color: #FDE68A; background: #3F2A12;"
        )
        row.addWidget(self._bridge_badge)

        self._copy_pair_code_btn = QPushButton("Copy")
        self._copy_pair_code_btn.setToolTip("Copy current pairing code")
        self._copy_pair_code_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_pair_code_btn.setEnabled(False)
        self._copy_pair_code_btn.setStyleSheet(
            "QPushButton {"
            "padding: 6px 10px; border-radius: 10px; "
            "background: #1F2A4D; border: 1px solid #304574; color: #E5E7EB;"
            "font-weight: 700;"
            "}"
            "QPushButton:hover { background: #25345F; border-color: #3C5690; }"
            "QPushButton:disabled { color: #6B7280; border-color: #374151; background: #1A2238; }"
        )
        self._copy_pair_code_btn.clicked.connect(self._on_copy_pair_code)
        row.addWidget(self._copy_pair_code_btn)

        return strip

    def _make_kpi_chip(self, title: str, value: str, accent: str) -> QFrame:
        chip = QFrame(self)
        chip.setStyleSheet(
            "QFrame { background: #0F172A; border: 1px solid #1E293B; border-radius: 8px; }"
            "QLabel { background: transparent; }"
        )

        layout = QVBoxLayout(chip)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(1)

        title_label = QLabel(title)
        title_label.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 600;")

        value_label = QLabel(value)
        value_label.setProperty("kpi_value", True)
        value_label.setStyleSheet(f"color: {accent}; font-size: 16px; font-weight: 800;")

        layout.addWidget(title_label)
        layout.addWidget(value_label)

        return chip

    @staticmethod
    def _set_kpi_value(chip: QFrame, value: int) -> None:
        for child in chip.findChildren(QLabel):
            if bool(child.property("kpi_value")):
                child.setText(str(value))
                return

    # ── Status Bar ─────────────────────────────────────────────────────────

    def _build_statusbar(self) -> None:
        statusbar = QStatusBar(self)
        self.setStatusBar(statusbar)

        self._status_downloads = QLabel("Downloads: 0")
        self._status_chunks = QLabel("Chunks: —")
        self._status_speed = QLabel("Speed: 0 B/s")
        self._status_scheduler = QLabel("")
        self._status_bridge = QLabel("Bridge: offline")
        self._status_pairing = QLabel("Pair code: —")

        statusbar.addWidget(self._status_downloads)
        statusbar.addWidget(self._create_separator())
        statusbar.addWidget(self._status_chunks)
        statusbar.addWidget(self._create_separator())
        statusbar.addWidget(self._status_speed)
        statusbar.addWidget(self._create_separator())
        statusbar.addWidget(self._status_bridge)
        statusbar.addWidget(self._create_separator())
        statusbar.addWidget(self._status_pairing)
        statusbar.addPermanentWidget(self._status_scheduler)

    def set_bridge_status(
        self,
        enabled: bool,
        host: str,
        port: int,
        pairing_code: str = "",
    ) -> None:
        """Show bridge server state so extension/app endpoint mismatches are obvious."""
        host_text = (host or "127.0.0.1").strip()
        code_raw = str(pairing_code or "").strip().upper()
        code = self._format_pairing_code(code_raw)
        self._pairing_code_raw = "".join(ch for ch in code_raw if ch.isalnum())
        if enabled:
            self._status_bridge.setText(f"Bridge: online ({host_text}:{port})")
            self._status_bridge.setStyleSheet("color: #10B981;")
            self._status_pairing.setText(
                f"Pair code: {code}" if code else "Pair code: not active (Tray -> Pairing Code)"
            )
            self._status_pairing.setStyleSheet("color: #FDE68A;")
            self._bridge_badge.setText(f"Bridge Online • Pair {code}" if code else "Bridge Online")
            self._bridge_badge.setStyleSheet(
                "padding: 6px 10px; border-radius: 12px; "
                "font-weight: 700; color: #86EFAC; background: #123222;"
            )
            self._copy_pair_code_btn.setEnabled(bool(self._pairing_code_raw))
        else:
            self._status_bridge.setText(f"Bridge: offline ({host_text}:{port})")
            self._status_bridge.setStyleSheet("color: #F59E0B;")
            self._status_pairing.setText("Pair code: —")
            self._status_pairing.setStyleSheet("color: #8B949E;")
            self._bridge_badge.setText("Bridge Offline")
            self._bridge_badge.setStyleSheet(
                "padding: 6px 10px; border-radius: 12px; "
                "font-weight: 700; color: #FDE68A; background: #3F2A12;"
            )
            self._copy_pair_code_btn.setEnabled(False)

    @staticmethod
    def _format_pairing_code(code: str) -> str:
        """Render pairing code with a visual separator for readability."""
        normalized = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
        if len(normalized) == 8:
            return f"{normalized[:4]}-{normalized[4:]}"
        return normalized

    @pyqtSlot()
    def _on_copy_pair_code(self) -> None:
        """Copy the current pairing code to clipboard."""
        if not self._pairing_code_raw:
            return

        clipboard = QApplication.clipboard()
        if clipboard is None:
            return

        clipboard.setText(self._format_pairing_code(self._pairing_code_raw))
        self.statusBar().showMessage("Pairing code copied", 2500)

    @staticmethod
    def _create_separator() -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        return sep

    # ── Slots ──────────────────────────────────────────────────────────────

    def _get_selected_ids(self) -> list[str]:
        """Get download IDs for all selected rows."""
        ids: list[str] = []
        selection_model = self._table.selectionModel()
        if selection_model is None:
            return ids
        for index in selection_model.selectedRows():
            source_index = self._proxy_model.mapToSource(index)
            dl_id = self._model.get_download_id(source_index.row())
            if dl_id:
                ids.append(dl_id)
        return ids

    @pyqtSlot(list)
    def apply_reconciled_downloads(self, downloads: list[dict[str, Any]]) -> None:
        """Apply full snapshot updates without losing current row selection."""
        selected_ids = self._get_selected_ids()
        self._model.set_downloads(downloads)

        if not selected_ids:
            return

        selection_model = self._table.selectionModel()
        if selection_model is None:
            return

        selection_model.clearSelection()
        for dl_id in selected_ids:
            source_row = self._model.get_row_for_id(dl_id)
            if source_row is None:
                continue
            source_index = self._model.index(source_row, 0)
            proxy_index = self._proxy_model.mapFromSource(source_index)
            if not proxy_index.isValid():
                continue

            selection_model.select(
                proxy_index,
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
            )

    @pyqtSlot()
    def _on_add_url(self) -> None:
        from ui.add_dialog import AddDownloadDialog

        dialog = AddDownloadDialog(self._config, parent=self)

        def _on_accepted(data: dict) -> None:
            self.add_download_requested.emit(data)

        dialog.download_accepted.connect(_on_accepted)
        dialog.exec()

    @pyqtSlot()
    def _on_pause_selected(self) -> None:
        for dl_id in self._get_selected_ids():
            self.pause_requested.emit(dl_id)

    @pyqtSlot()
    def _on_resume_selected(self) -> None:
        for dl_id in self._get_selected_ids():
            self.resume_requested.emit(dl_id)

    @pyqtSlot()
    def _on_cancel_selected(self) -> None:
        ids = self._get_selected_ids()
        if not ids:
            return
        reply = QMessageBox.question(
            self, "Cancel Downloads",
            f"Cancel {len(ids)} download(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for dl_id in ids:
                self.cancel_requested.emit(dl_id)

    @pyqtSlot()
    def _on_delete_selected(self) -> None:
        ids = self._get_selected_ids()
        if not ids:
            return
        reply = QMessageBox.question(
            self, "Delete Downloads",
            f"Delete {len(ids)} download(s)?\n\nThis removes them from the list.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for dl_id in ids:
                self.delete_requested.emit(dl_id, False)

    @pyqtSlot(str)
    def _on_search_changed(self, text: str) -> None:
        self._proxy_model.setFilterFixedString(text)

    @pyqtSlot(str)
    def _on_filter_status(self, status: str) -> None:
        for name, action in self._filter_actions.items():
            action.setChecked(name == status)

        if status == "All":
            self._proxy_model.setFilterKeyColumn(COL_FILENAME)
            self._proxy_model.setFilterFixedString("")
        else:
            self._proxy_model.setFilterKeyColumn(COL_STATUS)
            self._proxy_model.setFilterFixedString(status)

    def _on_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        ids = self._get_selected_ids()
        if not ids:
            return

        menu.addAction("Resume", self._on_resume_selected)
        menu.addAction("Pause", self._on_pause_selected)
        menu.addAction("Cancel", self._on_cancel_selected)
        menu.addSeparator()
        menu.addAction("Delete", self._on_delete_selected)
        menu.addSeparator()

        # Refresh file info (single selection only)
        if len(ids) == 1:
            menu.addAction("Refresh File Info", lambda: self._on_refresh_file_info(ids[0]))
            menu.addSeparator()

        # Open file location
        if len(ids) == 1:
            menu.addAction("Open File", lambda: self._open_file(ids[0]))
            menu.addAction("Open Folder", lambda: self._open_folder(ids[0]))

        viewport = self._table.viewport()
        if viewport is None:
            return
        menu.exec(viewport.mapToGlobal(pos))

    def _get_download(self, dl_id: str) -> Optional[dict[str, Any]]:
        row = self._model._id_to_row.get(dl_id)
        if row is None:
            return None
        if row < 0 or row >= len(self._model._downloads):
            return None
        return self._model._downloads[row]

    @pyqtSlot(str)
    def _on_refresh_file_info(self, dl_id: str) -> None:
        """Show file info preview dialog for the selected download."""
        from ui.file_info_dialog import FileInfoDialog

        dl = self._get_download(dl_id)
        if not dl:
            return

        url = dl.get("url", "")
        filename = dl.get("filename", "")
        save_dir = dl.get("save_dir", r"D:\idm down")

        if not url:
            QMessageBox.warning(self, "Refresh Failed", "URL not available for this download")
            return

        dialog = FileInfoDialog(
            url=url,
            filename=filename,
            save_dir=save_dir,
            config=self._config,
            parent=self,
        )

        def _on_updated(file_info_data: dict) -> None:
            # Optionally show a notification
            QMessageBox.information(self, "File Info Refreshed", "File information has been updated.")

        dialog.download_accepted.connect(_on_updated)
        dialog.exec()

    def _open_file(self, dl_id: str) -> None:
        dl = self._get_download(dl_id)
        if not dl:
            return

        save_path = dl.get("save_path", "")
        if not save_path:
            return

        path = Path(save_path)
        if not path.exists() or not path.is_file():
            return

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_folder(self, dl_id: str) -> None:
        dl = self._get_download(dl_id)
        if not dl:
            return

        save_path = dl.get("save_path", "")
        if not save_path:
            return

        folder = Path(save_path).parent
        if folder.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    @pyqtSlot(QModelIndex)
    def _on_table_double_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return

        src_index = self._proxy_model.mapToSource(index)
        if not src_index.isValid():
            return

        row = src_index.row()
        if row < 0 or row >= len(self._model._downloads):
            return

        dl_id = self._model._downloads[row].get("id", "")
        if not dl_id:
            return

        dl = self._get_download(dl_id)
        if not dl:
            return

        save_path = dl.get("save_path", "")
        if not save_path:
            QMessageBox.information(
                self,
                "Open Download",
                "No local path is available for this item yet.",
            )
            return

        path = Path(save_path)
        if path.exists() and path.is_file():
            self._open_file(dl_id)
            return

        folder = path.parent
        if folder.exists():
            self._open_folder(dl_id)
            return

        QMessageBox.information(
            self,
            "Open Download",
            f"File and folder not found:\n{save_path}",
        )

    @pyqtSlot()
    def _on_pause_all(self) -> None:
        active = [
            dl["id"] for dl in self._model._downloads
            if dl.get("status") in ("downloading", "merging", "verifying")
        ]
        if not active:
            return

        reply = QMessageBox.question(
            self,
            "Pause All Downloads",
            f"Pause {len(active)} active download(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for dl_id in active:
            self.pause_requested.emit(dl_id)

    @pyqtSlot()
    def _on_cancel_all(self) -> None:
        to_cancel = [
            dl["id"] for dl in self._model._downloads
            if dl.get("status") in (
                "queued", "downloading", "paused", "failed", "merging", "verifying"
            )
        ]
        if not to_cancel:
            return

        reply = QMessageBox.question(
            self,
            "Cancel All Downloads",
            f"Cancel {len(to_cancel)} download(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for dl_id in to_cancel:
            self.cancel_requested.emit(dl_id)

    @pyqtSlot()
    def _on_start_all(self) -> None:
        to_start = [
            dl["id"] for dl in self._model._downloads
            if dl.get("status") in ("queued", "paused", "failed", "cancelled")
        ]
        if not to_start:
            return

        for dl_id in to_start:
            self.resume_requested.emit(dl_id)

    @pyqtSlot()
    def _on_resume_all(self) -> None:
        for dl in self._model._downloads:
            if dl.get("status") == "paused":
                self.resume_requested.emit(dl["id"])

    @pyqtSlot()
    def _on_delete_all(self) -> None:
        ids = [dl["id"] for dl in self._model._downloads]
        if not ids:
            return

        reply = QMessageBox.question(
            self,
            "Delete All Downloads",
            (
                f"Delete all {len(ids)} download(s) from the list?\n\n"
                "Choose Yes to remove only from IDM list, or No to cancel."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        disk_reply = QMessageBox.question(
            self,
            "Delete Files From Disk",
            "Also delete downloaded files from disk?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        delete_file = disk_reply == QMessageBox.StandardButton.Yes

        for dl_id in ids:
            self.delete_requested.emit(dl_id, delete_file)

    @pyqtSlot()
    def _on_clear_completed(self) -> None:
        completed = [
            dl["id"] for dl in self._model._downloads
            if dl.get("status") == "completed"
        ]
        for dl_id in completed:
            self.delete_requested.emit(dl_id, False)

    @pyqtSlot()
    def _on_import_list(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import URL List", "", "Text Files (*.txt);;All Files (*)"
        )
        if path:
            try:
                with open(path) as f:
                    for line in f:
                        url = line.strip()
                        if url and url.startswith(("http://", "https://", "ftp://")):
                            self.add_download_requested.emit(
                                {
                                    "url": url,
                                    "filename": "",
                                    "category": "Other",
                                }
                            )
            except OSError as exc:
                QMessageBox.warning(self, "Import Error", str(exc))

    @pyqtSlot()
    def _on_export_list(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export URL List", "downloads.txt", "Text Files (*.txt)"
        )
        if path:
            try:
                with open(path, "w") as f:
                    for dl in self._model._downloads:
                        f.write(dl.get("url", "") + "\n")
            except OSError as exc:
                QMessageBox.warning(self, "Export Error", str(exc))

    @pyqtSlot()
    def _on_show_analytics(self) -> None:
        """Show a compact analytics summary for download history."""
        downloads = list(self._model._downloads)
        total = len(downloads)

        if total == 0:
            QMessageBox.information(self, "Download Analytics", "No downloads in history yet.")
            return

        status_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        total_completed_bytes = 0

        for dl in downloads:
            status = str(dl.get("status", "unknown")).lower()
            status_counts[status] = status_counts.get(status, 0) + 1

            category = str(dl.get("category", "Other"))
            category_counts[category] = category_counts.get(category, 0) + 1

            if status == "completed":
                file_size = int(dl.get("file_size", -1) or -1)
                downloaded = int(dl.get("downloaded_bytes", 0) or 0)
                total_completed_bytes += max(downloaded, file_size if file_size > 0 else 0)

        top_categories = sorted(
            category_counts.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )[:3]
        top_categories_text = ", ".join(f"{name} ({count})" for name, count in top_categories) or "—"

        msg = (
            "<h3>Download History Analytics</h3>"
            f"<p><b>Total items:</b> {total}</p>"
            f"<p><b>Completed:</b> {status_counts.get('completed', 0)} &nbsp; "
            f"<b>Failed:</b> {status_counts.get('failed', 0)} &nbsp; "
            f"<b>Paused:</b> {status_counts.get('paused', 0)} &nbsp; "
            f"<b>Queued:</b> {status_counts.get('queued', 0)}</p>"
            f"<p><b>Total completed data:</b> {format_size(total_completed_bytes)}</p>"
            f"<p><b>Top categories:</b> {top_categories_text}</p>"
        )

        QMessageBox.information(self, "Download Analytics", msg)

    @pyqtSlot()
    def _on_about(self) -> None:
        QMessageBox.about(
            self, "About IDM",
            "<h2>Internet Download Manager</h2>"
            "<p>Version 1.0.0</p>"
            "<p>A professional download manager with multi-threaded "
            "parallel downloading, resume support, and browser integration.</p>"
        )

    @pyqtSlot()
    def _on_settings(self) -> None:
        from ui.settings_dialog import SettingsDialog
        from main import deep_merge

        dialog = SettingsDialog(self._config, parent=self)

        def _on_saved(values: dict) -> None:
            merged = deep_merge(self._config, values)
            self._config.update(merged)
            self.settings_changed.emit(merged)
            log.info("Settings updated")

        dialog.settings_saved.connect(_on_saved)
        dialog.exec()

    @pyqtSlot()
    def _on_refresh_tick(self) -> None:
        """Periodic UI refresh for status bar and speed graph."""
        active = sum(
            1 for dl in self._model._downloads
            if dl.get("status") in ("downloading", "merging", "verifying")
        )
        queued = sum(1 for dl in self._model._downloads if dl.get("status") == "queued")
        completed = sum(1 for dl in self._model._downloads if dl.get("status") == "completed")
        failed = sum(1 for dl in self._model._downloads if dl.get("status") == "failed")
        total = len(self._model._downloads)
        total_speed = sum(self._model._speeds.values())
        
        # Calculate total chunks being used
        total_chunks = sum(
            dl.get("chunks_count", 0) for dl in self._model._downloads
            if dl.get("status") in ("downloading", "merging", "verifying") and dl.get("chunks_count", 0) > 1
        )

        self._status_downloads.setText(
            f"Downloads: {active} active / {total} total"
        )
        if total_chunks > 0:
            self._status_chunks.setText(f"Chunks: {total_chunks}")
        else:
            self._status_chunks.setText("Chunks: —")
        self._status_speed.setText(f"Speed: {format_speed(total_speed)}")

        self._set_kpi_value(self._kpi_active, active)
        self._set_kpi_value(self._kpi_queued, queued)
        self._set_kpi_value(self._kpi_completed, completed)
        self._set_kpi_value(self._kpi_failed, failed)

        # Dynamic throttling to save CPU when idle
        if active == 0 and self._refresh_timer.interval() != 3000:
            self._refresh_timer.setInterval(3000)
        elif active > 0 and self._refresh_timer.interval() != 1000:
            self._refresh_timer.setInterval(1000)

    # ── Public API for engine bridge ───────────────────────────────────────

    @pyqtSlot(str, int, int, float, float)
    def on_engine_progress(
        self, dl_id: str, downloaded: int, total: int,
        speed: float, eta: float,
    ) -> None:
        """Called from engine thread via signal."""
        self._model.update_progress(dl_id, downloaded, total, speed, eta)

    @pyqtSlot(str, int, int)
    def on_engine_chunks(self, dl_id: str, completed: int, total: int) -> None:
        """Called when chunk completion counters change."""
        self._model.update_chunks(dl_id, completed, total)

    @pyqtSlot(str, str, str)
    def on_engine_status(self, dl_id: str, status: str, error: str = "") -> None:
        """Called when download status changes."""
        self._model.update_status(dl_id, status, error)

    @pyqtSlot(str, dict)
    def on_engine_download_added(self, dl_id: str, data: dict) -> None:
        """Called when a new download is added."""
        self._model.add_download(data)

    @pyqtSlot(str, bool)
    def on_engine_download_deleted(self, dl_id: str, success: bool) -> None:
        """Called when backend delete operation finishes."""
        if success:
            self._model.remove_download(dl_id)
            return

        QMessageBox.warning(
            self,
            "Delete Failed",
            "Could not delete one or more selected downloads. Please try again.",
        )

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Handle close behavior based on user preference."""
        tray = getattr(self, "_tray", None)
        general_cfg = self._config.get("general", {})
        close_behavior = str(
            general_cfg.get(
                "close_button_behavior",
                "minimize_to_tray" if general_cfg.get("minimize_to_tray", True) else "quit",
            )
        )

        tray_available = (
            tray is not None and getattr(tray, "isVisible", lambda: False)()
        )

        should_minimize = close_behavior == "minimize_to_tray"
        if close_behavior == "ask":
            choice = QMessageBox.question(
                self,
                "Close IDM",
                "What do you want to do?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            # Yes: minimize to tray, No: quit, Cancel: abort close.
            if choice == QMessageBox.StandardButton.Cancel:
                if event is not None:
                    event.ignore()
                return
            should_minimize = choice == QMessageBox.StandardButton.Yes

        if should_minimize and tray_available:
            self.hide()
            if event is not None:
                event.ignore()
            return

        if event is not None:
            event.accept()
        app = QApplication.instance()
        if app is not None:
            app.quit()
