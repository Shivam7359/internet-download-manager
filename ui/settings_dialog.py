"""
IDM UI — Settings Dialog
==========================
Tabbed settings dialog covering all configuration sections.

Tabs:
    1. General  — download directory, concurrency, theme, behaviour
    2. Network  — bandwidth limit, proxy, SSL, user-agent
    3. Scheduler  — enable/disable, time window, days
    4. Advanced  — chunk sizes, hash verify, speed history
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QWidget, QLabel, QLineEdit, QPushButton, QComboBox,
    QSpinBox, QCheckBox, QGroupBox, QFileDialog, QMessageBox, QScrollArea,
)

from utils.speed_tuning import compute_stable_limits

log = logging.getLogger("idm.ui.settings")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SETTINGS DIALOG                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class SettingsDialog(QDialog):
    """
    Modal tabbed settings dialog.

    Emits ``settings_saved(dict)`` with the full updated config when OK is pressed.
    """

    settings_saved = pyqtSignal(dict)

    def __init__(
        self,
        config: dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config

        self.setWindowTitle("Settings")
        self.setMinimumSize(620, 520)
        self.setModal(True)

        self._build_ui()
        self._load_values()

    # ── UI Construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 16)

        # Title
        title = QLabel("Settings")
        title.setStyleSheet(
            "font-size: 20px; font-weight: 700; color: #E5E7EB; "
            "background: transparent; margin-bottom: 4px;"
        )
        layout.addWidget(title)

        # Tab widget
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #21262D;
                border-radius: 8px;
                background: #0D1117;
                padding: 16px;
            }
            QTabBar::tab {
                background: #161B22;
                color: #8B949E;
                padding: 10px 20px;
                border: none;
                border-bottom: 2px solid transparent;
                font-weight: 600;
                font-size: 12px;
            }
            QTabBar::tab:selected {
                color: #E5E7EB;
                border-bottom-color: #58A6FF;
            }
            QTabBar::tab:hover:!selected {
                color: #C9D1D9;
                background: #21262D;
            }
        """)

        self._tabs.addTab(self._build_general_tab(), "General")
        self._tabs.addTab(self._build_network_tab(), "Network")
        self._tabs.addTab(self._build_scheduler_tab(), "Scheduler")
        self._tabs.addTab(self._build_advanced_tab(), "Advanced")

        layout.addWidget(self._tabs)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(90)
        cancel_btn.setStyleSheet(self._button_style())
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setFixedWidth(100)
        save_btn.setStyleSheet("""
            QPushButton {
                padding: 10px 20px; border-radius: 8px;
                background: #238636; color: #FFFFFF;
                font-weight: 700; font-size: 13px; border: none;
            }
            QPushButton:hover { background: #2EA043; }
        """)
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _wrap_scroll_tab(self, content: QWidget) -> QWidget:
        """Wrap a tab content widget in a scroll area to avoid layout compression."""
        tab = QWidget()
        outer_layout = QVBoxLayout(tab)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)

        outer_layout.addWidget(scroll)
        return tab

    # ── General Tab ────────────────────────────────────────────────────────

    def _build_general_tab(self) -> QWidget:
        content = QWidget()
        form = QFormLayout(content)
        form.setSpacing(12)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Download directory
        dir_layout = QHBoxLayout()
        self._download_dir = QLineEdit()
        self._download_dir.setStyleSheet(self._input_style())
        dir_layout.addWidget(self._download_dir)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.setStyleSheet(self._button_style())
        browse_btn.clicked.connect(self._on_browse_dir)
        dir_layout.addWidget(browse_btn)
        form.addRow("Download Directory:", dir_layout)

        # Max concurrent
        self._max_concurrent = QSpinBox()
        self._max_concurrent.setRange(1, 20)
        self._max_concurrent.setStyleSheet(self._spin_style())
        form.addRow("Max Concurrent Downloads:", self._max_concurrent)

        # Default chunks
        self._default_chunks = QSpinBox()
        self._default_chunks.setRange(3, 5)
        self._default_chunks.setStyleSheet(self._spin_style())
        form.addRow("Default Chunks per Download:", self._default_chunks)

        # Theme
        self._theme_combo = QComboBox()
        self._theme_combo.addItems(["dark", "light"])
        self._theme_combo.setStyleSheet(self._combo_style())
        form.addRow("Theme:", self._theme_combo)

        # Checkboxes
        self._auto_start = QCheckBox("Auto-start downloads when added")
        self._auto_start.setStyleSheet(self._check_style())
        form.addRow("", self._auto_start)

        self._minimize_tray = QCheckBox("Minimize to tray instead of closing")
        self._minimize_tray.setStyleSheet(self._check_style())
        form.addRow("", self._minimize_tray)

        self._close_behavior = QComboBox()
        self._close_behavior.addItem("Minimize to tray", "minimize_to_tray")
        self._close_behavior.addItem("Ask every time", "ask")
        self._close_behavior.addItem("Quit", "quit")
        self._close_behavior.setStyleSheet(self._combo_style())
        form.addRow("Close Button Behavior:", self._close_behavior)

        self._start_with_system = QCheckBox("Start IDM bridge with Windows")
        self._start_with_system.setStyleSheet(self._check_style())
        form.addRow("", self._start_with_system)

        self._show_notifications = QCheckBox("Show desktop notifications")
        self._show_notifications.setStyleSheet(self._check_style())
        form.addRow("", self._show_notifications)

        self._confirm_exit = QCheckBox("Confirm before exiting")
        self._confirm_exit.setStyleSheet(self._check_style())
        form.addRow("", self._confirm_exit)

        self._sound_complete = QCheckBox("Play sound on download complete")
        self._sound_complete.setStyleSheet(self._check_style())
        form.addRow("", self._sound_complete)

        return self._wrap_scroll_tab(content)

    # ── Network Tab ────────────────────────────────────────────────────────

    def _build_network_tab(self) -> QWidget:
        tab = QWidget()
        outer_layout = QVBoxLayout(tab)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # Bandwidth
        bw_group = QGroupBox("Bandwidth")
        bw_group.setStyleSheet(self._group_style())
        bw_form = QFormLayout(bw_group)

        # Global Speed Limit
        bw_global_layout = QHBoxLayout()
        bw_global_layout.setContentsMargins(0, 0, 0, 0)
        self._bandwidth_limit = QSpinBox()
        self._bandwidth_limit.setRange(0, 1_000_000)
        self._bandwidth_limit.setSuffix(" KB/s")
        self._bandwidth_limit.setSpecialValueText("Unlimited")
        self._bandwidth_limit.setStyleSheet(self._spin_style())
        bw_global_layout.addWidget(self._bandwidth_limit, 1)

        btn_global_unlimited = QPushButton("Unlimited")
        btn_global_unlimited.setStyleSheet(self._button_style())
        btn_global_unlimited.clicked.connect(lambda: self._bandwidth_limit.setValue(0))
        bw_global_layout.addWidget(btn_global_unlimited)
        bw_form.addRow("Speed Limit:", bw_global_layout)

        # Per-Download Limit
        bw_per_layout = QHBoxLayout()
        bw_per_layout.setContentsMargins(0, 0, 0, 0)
        self._per_download_bandwidth_limit = QSpinBox()
        self._per_download_bandwidth_limit.setRange(0, 1_000_000)
        self._per_download_bandwidth_limit.setSuffix(" KB/s")
        self._per_download_bandwidth_limit.setSpecialValueText("Unlimited")
        self._per_download_bandwidth_limit.setStyleSheet(self._spin_style())
        bw_per_layout.addWidget(self._per_download_bandwidth_limit, 1)

        btn_per_unlimited = QPushButton("Unlimited")
        btn_per_unlimited.setStyleSheet(self._button_style())
        btn_per_unlimited.clicked.connect(lambda: self._per_download_bandwidth_limit.setValue(0))
        bw_per_layout.addWidget(btn_per_unlimited)
        bw_form.addRow("Per-Download Limit:", bw_per_layout)

        self._auto_stable_limits = QCheckBox("Auto-apply stable speed limits on startup")
        self._auto_stable_limits.setStyleSheet(self._check_style())
        bw_form.addRow("", self._auto_stable_limits)

        suggest_row = QHBoxLayout()
        suggest_btn = QPushButton("Suggest Stable Limits")
        suggest_btn.setStyleSheet(self._button_style())
        suggest_btn.clicked.connect(self._on_suggest_stable_limits)
        suggest_row.addWidget(suggest_btn)

        suggest_hint = QLabel("Uses current concurrency and limits to suggest smoother caps")
        suggest_hint.setStyleSheet(
            "color: #8B949E; font-size: 11px; background: transparent;"
        )
        suggest_row.addWidget(suggest_hint, 1)
        bw_form.addRow("", suggest_row)

        layout.addWidget(bw_group)

        # Timeouts
        timeout_group = QGroupBox("Timeouts && Retries")
        timeout_group.setStyleSheet(self._group_style())
        t_form = QFormLayout(timeout_group)

        self._conn_timeout = QSpinBox()
        self._conn_timeout.setRange(5, 300)
        self._conn_timeout.setSuffix(" sec")
        self._conn_timeout.setStyleSheet(self._spin_style())
        t_form.addRow("Connection Timeout:", self._conn_timeout)

        self._read_timeout = QSpinBox()
        self._read_timeout.setRange(5, 600)
        self._read_timeout.setSuffix(" sec")
        self._read_timeout.setStyleSheet(self._spin_style())
        t_form.addRow("Read Timeout:", self._read_timeout)

        self._first_byte_timeout = QSpinBox()
        self._first_byte_timeout.setRange(1, 120)
        self._first_byte_timeout.setSuffix(" sec")
        self._first_byte_timeout.setStyleSheet(self._spin_style())
        t_form.addRow("First Byte Timeout:", self._first_byte_timeout)

        self._max_retries = QSpinBox()
        self._max_retries.setRange(0, 20)
        self._max_retries.setStyleSheet(self._spin_style())
        t_form.addRow("Max Retries:", self._max_retries)

        layout.addWidget(timeout_group)

        # Proxy
        proxy_group = QGroupBox("Proxy")
        proxy_group.setStyleSheet(self._group_style())
        p_form = QFormLayout(proxy_group)

        self._proxy_enabled = QCheckBox("Enable proxy")
        self._proxy_enabled.setStyleSheet(self._check_style())
        p_form.addRow("", self._proxy_enabled)

        self._proxy_type = QComboBox()
        self._proxy_type.addItems(["http", "socks4", "socks5"])
        self._proxy_type.setStyleSheet(self._combo_style())
        p_form.addRow("Type:", self._proxy_type)

        self._proxy_host = QLineEdit()
        self._proxy_host.setPlaceholderText("127.0.0.1")
        self._proxy_host.setStyleSheet(self._input_style())
        p_form.addRow("Host:", self._proxy_host)

        self._proxy_port = QSpinBox()
        self._proxy_port.setRange(0, 65535)
        self._proxy_port.setStyleSheet(self._spin_style())
        p_form.addRow("Port:", self._proxy_port)

        self._proxy_user = QLineEdit()
        self._proxy_user.setPlaceholderText("(optional)")
        self._proxy_user.setStyleSheet(self._input_style())
        p_form.addRow("Username:", self._proxy_user)

        self._proxy_pass = QLineEdit()
        self._proxy_pass.setPlaceholderText("(optional)")
        self._proxy_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._proxy_pass.setStyleSheet(self._input_style())
        p_form.addRow("Password:", self._proxy_pass)

        layout.addWidget(proxy_group)

        # SSL
        self._verify_ssl = QCheckBox("Verify SSL certificates")
        self._verify_ssl.setStyleSheet(self._check_style())
        layout.addWidget(self._verify_ssl)

        # Bridge server
        server_group = QGroupBox("Extension Bridge Server")
        server_group.setStyleSheet(self._group_style())
        s_form = QFormLayout(server_group)

        self._server_enabled = QCheckBox("Enable local bridge server")
        self._server_enabled.setStyleSheet(self._check_style())
        s_form.addRow("", self._server_enabled)

        self._server_host = QLineEdit()
        self._server_host.setPlaceholderText("127.0.0.1")
        self._server_host.setStyleSheet(self._input_style())
        s_form.addRow("Host:", self._server_host)

        self._server_port = QSpinBox()
        self._server_port.setRange(1, 65535)
        self._server_port.setStyleSheet(self._spin_style())
        s_form.addRow("Port:", self._server_port)

        self._server_token = QLineEdit()
        self._server_token.setPlaceholderText("(optional)")
        self._server_token.setStyleSheet(self._input_style())
        s_form.addRow("Auth Token:", self._server_token)

        layout.addWidget(server_group)

        layout.addStretch(1)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        return tab

    # ── Scheduler Tab ──────────────────────────────────────────────────────

    def _build_scheduler_tab(self) -> QWidget:
        content = QWidget()
        form = QFormLayout(content)
        form.setSpacing(12)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._scheduler_enabled = QCheckBox("Enable download scheduler")
        self._scheduler_enabled.setStyleSheet(self._check_style())
        form.addRow("", self._scheduler_enabled)

        info = QLabel(
            "When enabled, downloads will only run during the configured\n"
            "time window. Outside of this window, downloads will be paused."
        )
        info.setStyleSheet(
            "color: #8B949E; font-size: 11px; background: transparent;"
        )
        form.addRow("", info)

        self._sched_start = QLineEdit()
        self._sched_start.setPlaceholderText("HH:MM (e.g. 02:00)")
        self._sched_start.setStyleSheet(self._input_style())
        form.addRow("Start Time:", self._sched_start)

        self._sched_end = QLineEdit()
        self._sched_end.setPlaceholderText("HH:MM (e.g. 06:00)")
        self._sched_end.setStyleSheet(self._input_style())
        form.addRow("End Time:", self._sched_end)

        # Days checkboxes
        days_widget = QWidget()
        days_widget.setStyleSheet("background: transparent;")
        days_layout = QHBoxLayout(days_widget)
        days_layout.setContentsMargins(0, 0, 0, 0)
        days_layout.setSpacing(8)

        self._day_checks: dict[str, QCheckBox] = {}
        for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
            cb = QCheckBox(day)
            cb.setStyleSheet(self._check_style())
            days_layout.addWidget(cb)
            self._day_checks[day.lower()] = cb

        form.addRow("Active Days:", days_widget)

        # Action after all downloads complete
        self._action_after = QComboBox()
        self._action_after.addItems([
            "none", "shutdown", "hibernate", "sleep", "exit",
        ])
        self._action_after.setStyleSheet(self._combo_style())
        form.addRow("After All Complete:", self._action_after)

        return self._wrap_scroll_tab(content)

    # ── Advanced Tab ───────────────────────────────────────────────────────

    def _build_advanced_tab(self) -> QWidget:
        content = QWidget()
        form = QFormLayout(content)
        form.setSpacing(12)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._dynamic_chunks = QCheckBox("Enable dynamic chunk adjustment")
        self._dynamic_chunks.setStyleSheet(self._check_style())
        form.addRow("", self._dynamic_chunks)

        self._min_chunk_size = QSpinBox()
        self._min_chunk_size.setRange(64, 10240)
        self._min_chunk_size.setSuffix(" KB")
        self._min_chunk_size.setStyleSheet(self._spin_style())
        form.addRow("Min Chunk Size:", self._min_chunk_size)

        self._max_chunk_size = QSpinBox()
        self._max_chunk_size.setRange(1, 200)
        self._max_chunk_size.setSuffix(" MB")
        self._max_chunk_size.setStyleSheet(self._spin_style())
        form.addRow("Max Chunk Size:", self._max_chunk_size)

        self._hash_verify = QCheckBox("Verify file hash after download")
        self._hash_verify.setStyleSheet(self._check_style())
        form.addRow("", self._hash_verify)

        self._speed_interval = QSpinBox()
        self._speed_interval.setRange(100, 5000)
        self._speed_interval.setSuffix(" ms")
        self._speed_interval.setStyleSheet(self._spin_style())
        form.addRow("Speed Sample Interval:", self._speed_interval)

        self._history_days = QSpinBox()
        self._history_days.setRange(1, 365)
        self._history_days.setSuffix(" days")
        self._history_days.setStyleSheet(self._spin_style())
        form.addRow("History Retention:", self._history_days)

        self._chunk_buffer = QSpinBox()
        self._chunk_buffer.setRange(4, 512)
        self._chunk_buffer.setSuffix(" KB")
        self._chunk_buffer.setStyleSheet(self._spin_style())
        form.addRow("Chunk Buffer Size:", self._chunk_buffer)

        # Temp directory
        tmp_layout = QHBoxLayout()
        self._temp_dir = QLineEdit()
        self._temp_dir.setPlaceholderText("(default: system temp)")
        self._temp_dir.setStyleSheet(self._input_style())
        tmp_layout.addWidget(self._temp_dir)
        tmp_browse = QPushButton("Browse…")
        tmp_browse.setFixedWidth(80)
        tmp_browse.setStyleSheet(self._button_style())
        tmp_browse.clicked.connect(self._on_browse_temp)
        tmp_layout.addWidget(tmp_browse)
        form.addRow("Temp Directory:", tmp_layout)
        
        # Antivirus
        av_group = QGroupBox("Antivirus / Security")
        av_group.setStyleSheet(self._group_style())
        av_form = QFormLayout(av_group)

        self._av_enabled = QCheckBox("Scan downloaded files automatically")
        self._av_enabled.setStyleSheet(self._check_style())
        av_form.addRow("", self._av_enabled)

        self._av_path = QLineEdit()
        self._av_path.setPlaceholderText("C:\\Program Files\\Windows Defender\\MpCmdRun.exe")
        self._av_path.setStyleSheet(self._input_style())
        av_form.addRow("Scanner Path:", self._av_path)

        form.addRow(av_group)

        return self._wrap_scroll_tab(content)

    # ── Load / Save ────────────────────────────────────────────────────────

    def _load_values(self) -> None:
        g = self._config.get("general", {})
        n = self._config.get("network", {})
        s = self._config.get("scheduler", {})
        a = self._config.get("advanced", {})
        p = n.get("proxy", {})

        # General
        self._download_dir.setText(g.get("download_directory", ""))
        self._max_concurrent.setValue(g.get("max_concurrent_downloads", 4))
        self._default_chunks.setValue(g.get("default_chunks", 5))
        idx = self._theme_combo.findText(g.get("theme", "dark"))
        if idx >= 0:
            self._theme_combo.setCurrentIndex(idx)
        self._auto_start.setChecked(g.get("auto_start_downloads", True))
        self._minimize_tray.setChecked(g.get("minimize_to_tray", True))
        close_behavior = str(
            g.get(
                "close_button_behavior",
                "minimize_to_tray" if g.get("minimize_to_tray", True) else "quit",
            )
        )
        index = self._close_behavior.findData(close_behavior)
        self._close_behavior.setCurrentIndex(index if index >= 0 else 0)
        self._start_with_system.setChecked(g.get("start_with_system", False))
        self._show_notifications.setChecked(g.get("show_notifications", True))
        self._confirm_exit.setChecked(g.get("confirm_on_exit", True))
        self._sound_complete.setChecked(g.get("sound_on_complete", True))
        # Network
        self._bandwidth_limit.setValue(n.get("bandwidth_limit_kbps", 0))
        self._per_download_bandwidth_limit.setValue(
            n.get("per_download_bandwidth_kbps", 0)
        )
        self._auto_stable_limits.setChecked(
            n.get("auto_apply_stable_limits_on_startup", False)
        )
        self._conn_timeout.setValue(n.get("connection_timeout_seconds", 30))
        self._read_timeout.setValue(n.get("read_timeout_seconds", 60))
        self._first_byte_timeout.setValue(a.get("first_byte_timeout_seconds", 15))
        self._max_retries.setValue(n.get("max_retries", 5))
        self._proxy_enabled.setChecked(p.get("enabled", False))
        idx = self._proxy_type.findText(p.get("type", "http"))
        if idx >= 0:
            self._proxy_type.setCurrentIndex(idx)
        self._proxy_host.setText(p.get("host", ""))
        self._proxy_port.setValue(p.get("port", 0))
        self._proxy_user.setText(p.get("username", ""))
        self._proxy_pass.setText(p.get("password", ""))
        self._verify_ssl.setChecked(n.get("verify_ssl", True))

        # Server
        server = self._config.get("server", {})
        self._server_enabled.setChecked(server.get("enabled", True))
        self._server_host.setText(server.get("host", "127.0.0.1"))
        self._server_port.setValue(server.get("port", 6800))
        self._server_token.setText(server.get("auth_token", ""))

        # Scheduler
        self._scheduler_enabled.setChecked(s.get("enabled", False))
        self._sched_start.setText(s.get("start_time", "02:00"))
        self._sched_end.setText(s.get("end_time", "06:00"))
        days = s.get("days", [])
        for day_key, cb in self._day_checks.items():
            cb.setChecked(day_key in days)
        idx = self._action_after.findText(
            s.get("action_after_complete", "none")
        )
        if idx >= 0:
            self._action_after.setCurrentIndex(idx)

        # Advanced
        self._dynamic_chunks.setChecked(a.get("dynamic_chunk_adjustment", True))
        self._min_chunk_size.setValue(
            a.get("min_chunk_size_bytes", 262144) // 1024
        )
        self._max_chunk_size.setValue(
            a.get("max_chunk_size_bytes", 52428800) // (1024 * 1024)
        )
        self._hash_verify.setChecked(a.get("hash_verify_on_complete", True))
        self._speed_interval.setValue(a.get("speed_sample_interval_ms", 500))
        self._history_days.setValue(a.get("history_retention_days", 90))
        self._chunk_buffer.setValue(
            a.get("chunk_buffer_size_bytes", 16384) // 1024
        )
        self._temp_dir.setText(a.get("temp_directory", ""))
        self._av_enabled.setChecked(a.get("antivirus_enabled", False))
        self._av_path.setText(a.get("antivirus_path", ""))

    def _collect_values(self) -> dict[str, Any]:
        active_days = [
            day for day, cb in self._day_checks.items() if cb.isChecked()
        ]

        return {
            "general": {
                "download_directory": self._download_dir.text(),
                "max_concurrent_downloads": self._max_concurrent.value(),
                "default_chunks": self._default_chunks.value(),
                "theme": self._theme_combo.currentText(),
                "auto_start_downloads": self._auto_start.isChecked(),
                "minimize_to_tray": self._minimize_tray.isChecked(),
                "close_button_behavior": str(self._close_behavior.currentData() or "minimize_to_tray"),
                "start_with_system": self._start_with_system.isChecked(),
                "show_notifications": self._show_notifications.isChecked(),
                "confirm_on_exit": self._confirm_exit.isChecked(),
                "sound_on_complete": self._sound_complete.isChecked(),
            },
            "network": {
                "bandwidth_limit_kbps": self._bandwidth_limit.value(),
                "per_download_bandwidth_kbps": self._per_download_bandwidth_limit.value(),
                "auto_apply_stable_limits_on_startup": self._auto_stable_limits.isChecked(),
                "connection_timeout_seconds": self._conn_timeout.value(),
                "read_timeout_seconds": self._read_timeout.value(),
                "max_retries": self._max_retries.value(),
                "proxy": {
                    "enabled": self._proxy_enabled.isChecked(),
                    "type": self._proxy_type.currentText(),
                    "host": self._proxy_host.text(),
                    "port": self._proxy_port.value(),
                    "username": self._proxy_user.text(),
                    "password": self._proxy_pass.text(),
                },
                "verify_ssl": self._verify_ssl.isChecked(),
            },
            "server": {
                "enabled": self._server_enabled.isChecked(),
                "host": self._server_host.text().strip() or "127.0.0.1",
                "port": self._server_port.value(),
                "auth_token": self._server_token.text().strip(),
            },
            "scheduler": {
                "enabled": self._scheduler_enabled.isChecked(),
                "start_time": self._sched_start.text(),
                "end_time": self._sched_end.text(),
                "days": active_days,
                "action_after_complete": self._action_after.currentText(),
            },
            "advanced": {
                "dynamic_chunk_adjustment": self._dynamic_chunks.isChecked(),
                "min_chunk_size_bytes": self._min_chunk_size.value() * 1024,
                "max_chunk_size_bytes": (
                    self._max_chunk_size.value() * 1024 * 1024
                ),
                "hash_verify_on_complete": self._hash_verify.isChecked(),
                "speed_sample_interval_ms": self._speed_interval.value(),
                "history_retention_days": self._history_days.value(),
                "chunk_buffer_size_bytes": self._chunk_buffer.value() * 1024,
                "first_byte_timeout_seconds": self._first_byte_timeout.value(),
                "temp_directory": self._temp_dir.text(),
                "antivirus_enabled": self._av_enabled.isChecked(),
                "antivirus_path": self._av_path.text(),
            },
        }

    @pyqtSlot()
    def _on_save(self) -> None:
        values = self._collect_values()
        self.settings_saved.emit(values)
        self.accept()

    @pyqtSlot()
    def _on_browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select Download Directory", self._download_dir.text(),
        )
        if d:
            self._download_dir.setText(d)

    @pyqtSlot()
    def _on_browse_temp(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select Temp Directory", self._temp_dir.text(),
        )
        if d:
            self._temp_dir.setText(d)

    @pyqtSlot()
    def _on_suggest_stable_limits(self) -> None:
        """Suggest conservative caps that reduce speed oscillation."""
        suggested_global, suggested_per_download, tip = compute_stable_limits(
            max_concurrent_downloads=self._max_concurrent.value(),
            default_chunks=self._default_chunks.value(),
            bandwidth_limit_kbps=self._bandwidth_limit.value(),
            per_download_bandwidth_kbps=self._per_download_bandwidth_limit.value(),
        )

        self._bandwidth_limit.setValue(suggested_global)
        self._per_download_bandwidth_limit.setValue(suggested_per_download)

        chunk_tip = f"\n{tip}" if tip else ""

        QMessageBox.information(
            self,
            "Stable Speed Suggestion Applied",
            (
                f"Global limit: {suggested_global} KB/s\n"
                f"Per-download limit: {suggested_per_download} KB/s\n\n"
                "These values keep around 10-15% headroom to reduce speed spikes."
                f"{chunk_tip}"
            ),
        )

    # ── Style helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _input_style() -> str:
        return """
            QLineEdit {
                min-height: 36px;
                padding: 7px 12px; border: 1px solid #30363D;
                border-radius: 6px; background: #161B22;
                color: #E5E7EB; font-size: 13px;
            }
            QLineEdit:focus { border-color: #58A6FF; }
        """

    @staticmethod
    def _button_style() -> str:
        return """
            QPushButton {
                min-height: 34px;
                padding: 7px 16px; border: 1px solid #30363D;
                border-radius: 6px; background: #21262D;
                color: #C9D1D9; font-size: 12px; font-weight: 600;
            }
            QPushButton:hover { background: #30363D; border-color: #58A6FF; }
        """

    @staticmethod
    def _spin_style() -> str:
        return """
            QSpinBox, QDoubleSpinBox {
                min-height: 36px;
                padding: 6px 10px; border: 1px solid #30363D;
                border-radius: 6px; background: #161B22;
                color: #E5E7EB; font-size: 13px;
            }
            QSpinBox:focus, QDoubleSpinBox:focus { border-color: #58A6FF; }
        """

    @staticmethod
    def _combo_style() -> str:
        return """
            QComboBox {
                min-height: 36px;
                padding: 7px 12px; border: 1px solid #30363D;
                border-radius: 6px; background: #161B22;
                color: #E5E7EB; font-size: 13px;
            }
            QComboBox:hover { border-color: #58A6FF; }
            QComboBox QAbstractItemView {
                background: #161B22; border: 1px solid #30363D;
                selection-background-color: #1F6FEB;
            }
        """

    @staticmethod
    def _check_style() -> str:
        return """
            QCheckBox {
                color: #C9D1D9; font-size: 13px; spacing: 8px;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 18px; height: 18px;
                border: 2px solid #30363D; border-radius: 4px;
                background: #161B22;
            }
            QCheckBox::indicator:checked {
                background: #58A6FF; border-color: #58A6FF;
            }
            QCheckBox::indicator:hover { border-color: #58A6FF; }
        """

    @staticmethod
    def _group_style() -> str:
        return """
            QGroupBox {
                font-weight: 600; font-size: 13px; color: #58A6FF;
                border: 1px solid #21262D; border-radius: 8px;
                margin-top: 12px; padding: 20px 16px 16px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 8px;
            }
        """
