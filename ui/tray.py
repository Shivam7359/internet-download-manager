"""
IDM UI — System Tray Icon
===========================
System tray integration with context menu and notifications.

Features:
    • Minimize to tray on close (configurable)
    • Tray context menu: Show / Add URL / Pause All / Resume All / Quit
    • Native desktop notifications on download complete / error
    • Dynamic tray icon tooltip showing active downloads and speed
    • Badge-style tooltip with download count
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import (
    QAction, QIcon, QPixmap, QPainter, QColor, QFont, QBrush,
)
from PyQt6.QtWidgets import (
    QSystemTrayIcon, QMenu, QWidget, QApplication, QMainWindow,
)

from core.network import format_speed

log = logging.getLogger("idm.ui.tray")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  TRAY ICON GENERATOR                                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def create_tray_icon_pixmap(
    size: int = 64,
    bg_color: str = "#1F6FEB",
    text_color: str = "#FFFFFF",
) -> QPixmap:
    """
    Generate a simple tray icon programmatically.

    Creates a blue rounded square with a down-arrow symbol.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Background circle
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(bg_color))
    margin = size // 8
    painter.drawRoundedRect(
        margin, margin, size - 2 * margin, size - 2 * margin,
        size // 4, size // 4,
    )

    # Down arrow
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(text_color))

    center_x = size // 2
    arrow_w = size // 3
    arrow_h = size // 4
    top_y = size // 3

    # Arrow shaft (rectangle)
    shaft_w = arrow_w // 2
    painter.drawRect(
        center_x - shaft_w // 2, top_y - 2,
        shaft_w, arrow_h,
    )

    # Arrow head (triangle)
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPolygonF
    head_y = top_y + arrow_h - 2
    triangle = QPolygonF([
        QPointF(center_x - arrow_w // 2, head_y),
        QPointF(center_x + arrow_w // 2, head_y),
        QPointF(center_x, head_y + arrow_h // 2 + 2),
    ])
    painter.drawPolygon(triangle)

    painter.end()
    return pixmap


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SYSTEM TRAY                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class SystemTray(QSystemTrayIcon):
    """
    System tray icon with context menu and notification support.

    Signals:
        show_window_requested — user wants to show the main window
        add_url_requested — user wants to add a new URL
        pause_all_requested — pause all downloads
        resume_all_requested — resume all downloads
        quit_requested — user wants to quit the application
    """

    show_window_requested = pyqtSignal()
    add_url_requested = pyqtSignal()
    pause_all_requested = pyqtSignal()
    resume_all_requested = pyqtSignal()
    show_pairing_code_requested = pyqtSignal()
    reset_pairing_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(
        self,
        config: dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._active_count = 0
        self._total_speed = 0.0
        self._pairing_code = ""

        # Generate icon
        pixmap = create_tray_icon_pixmap()
        self.setIcon(QIcon(pixmap))
        self.setToolTip("IDM - Internet Download Manager\nNo active downloads")

        # Context menu
        self._build_menu()

        # Double-click shows window
        self.activated.connect(self._on_activated)

        # Tooltip refresh timer
        self._tooltip_timer = QTimer()
        self._tooltip_timer.timeout.connect(self._update_tooltip)
        self._tooltip_timer.start(2000)

    def _build_menu(self) -> None:
        menu = QMenu()

        # Title (non-interactive)
        title_action = QAction("IDM Download Manager", menu)
        title_action.setEnabled(False)
        font = title_action.font()
        font.setBold(True)
        title_action.setFont(font)
        menu.addAction(title_action)
        menu.addSeparator()

        # Show window
        show_action = QAction("Open IDM", menu)
        show_action.triggered.connect(self.show_window_requested.emit)
        menu.addAction(show_action)

        pairing_action = QAction("Pairing Code", menu)
        pairing_action.triggered.connect(self.show_pairing_code_requested.emit)
        menu.addAction(pairing_action)

        reset_pairing_action = QAction("Reset Pairing", menu)
        reset_pairing_action.triggered.connect(self.reset_pairing_requested.emit)
        menu.addAction(reset_pairing_action)

        # Add URL
        add_action = QAction("Add URL...", menu)
        add_action.triggered.connect(self.add_url_requested.emit)
        menu.addAction(add_action)

        menu.addSeparator()

        # Pause / Resume all
        self._pause_action = QAction("Pause All", menu)
        self._pause_action.triggered.connect(self.pause_all_requested.emit)
        menu.addAction(self._pause_action)

        self._resume_action = QAction("Resume All", menu)
        self._resume_action.triggered.connect(self.resume_all_requested.emit)
        menu.addAction(self._resume_action)

        menu.addSeparator()

        # Status
        self._status_action = QAction("No active downloads", menu)
        self._status_action.setEnabled(False)
        menu.addAction(self._status_action)

        menu.addSeparator()

        # Quit
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window_requested.emit()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window_requested.emit()

    # ── Public API ─────────────────────────────────────────────────────────

    def update_status(self, active_count: int, total_speed: float) -> None:
        """Update the tray status with current download info."""
        self._active_count = active_count
        self._total_speed = total_speed
        self._update_tooltip()

    def set_pairing_code(self, pairing_code: str) -> None:
        """Update pairing code shown via tooltip/menu interaction."""
        raw = "".join(ch for ch in str(pairing_code or "").strip().upper() if ch.isalnum())
        self._pairing_code = f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else raw
        self._update_tooltip()

    @pyqtSlot()
    def _update_tooltip(self) -> None:
        if self._active_count > 0:
            tooltip = (
                f"IDM - Internet Download Manager\n"
                f"{self._active_count} active download(s)\n"
                f"Speed: {format_speed(self._total_speed)}"
            )
            self._status_action.setText(
                f"{self._active_count} active · {format_speed(self._total_speed)}"
            )
        else:
            tooltip = "IDM - Internet Download Manager\nNo active downloads"
            self._status_action.setText("No active downloads")

        if self._pairing_code:
            tooltip += f"\nPairing: {self._pairing_code}"

        self.setToolTip(tooltip)

    def notify_complete(self, filename: str, size_text: str = "") -> None:
        """Show a notification when a download completes."""
        if not self._config.get("general", {}).get("show_notifications", True):
            return

        detail = f" ({size_text})" if size_text else ""

        self.showMessage(
            "Download Complete",
            f"{filename}{detail} has been downloaded successfully.",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def notify_error(self, filename: str, error: str) -> None:
        """Show a notification when a download fails."""
        if not self._config.get("general", {}).get("show_notifications", True):
            return

        self.showMessage(
            "Download Failed",
            f"{filename}: {error}",
            QSystemTrayIcon.MessageIcon.Warning,
            5000,
        )

    def notify_added(self, filename: str) -> None:
        """Show a notification when a download is added from clipboard."""
        if not self._config.get("general", {}).get("show_notifications", True):
            return

        self.showMessage(
            "Download Added",
            f"Added: {filename}",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )
