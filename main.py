# IDM v2.0 — main.py — audited 2026-03-28
"""
IDM — Internet Download Manager
================================
Main entry point for the desktop application.

Responsibilities:
    1. Parse command-line arguments
    2. Configure application-wide logging (console + rotating file)
    3. Load / create the JSON configuration file
    4. Ensure single-instance execution (mutex / lock file)
    5. Bootstrap the PyQt6 QApplication with a dark theme
    6. Spin up the async download-engine thread (asyncio event loop)
    7. Spin up the FastAPI bridge server thread (uvicorn)
    8. Instantiate the main window and system-tray icon
    9. Enter the Qt event loop
   10. Perform clean shutdown of all subsystems on exit
"""

from __future__ import annotations

import sys
import os
import json
import logging
import asyncio
import argparse
import threading
import tempfile
import time
import secrets
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Optional
import traceback
if sys.platform.startswith("win"):
    import winreg

from utils.speed_tuning import apply_stable_limits_to_config

# ── Ensure the project root is importable ──────────────────────────────────────
BASE_DIR: Path = Path(__file__).parent.resolve()
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ── Constants ──────────────────────────────────────────────────────────────────
APP_NAME: str = "IDM — Internet Download Manager"
APP_VERSION: str = "1.1.0"
APP_ORG: str = "IDM"
CONFIG_FILE: str = "config.json"
DEFAULT_DOWNLOAD_ROOT: str = r"D:\idm down"
LOG_FILE: str = "idm.log"
DB_FILE: str = "downloads.db"
LOCK_FILE: str = ".idm.lock"

# Derived paths
if getattr(sys, "frozen", False):
    # In a packaged build, write mutable runtime data next to the executable.
    RUNTIME_DIR: Path = Path(sys.executable).resolve().parent
else:
    RUNTIME_DIR = BASE_DIR

DATA_DIR: Path = RUNTIME_DIR / "data"
LOG_DIR: Path = RUNTIME_DIR / "logs"
CHUNKS_DIR: Path = RUNTIME_DIR / "chunks"
I18N_DIR: Path = BASE_DIR / "i18n"


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  LOGGING                                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """
    Configure application-wide logging.

    Two handlers are attached to the root 'idm' logger:
        • Console  – coloured, concise format (INFO+)
        • File     – rotating 5 MB × 3 backups, verbose format (DEBUG+)

    Args:
        log_level: One of DEBUG, INFO, WARNING, ERROR, CRITICAL.

    Returns:
        The configured root logger for the 'idm' namespace.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("idm")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Prevent adding duplicate handlers on re-init
    if logger.handlers:
        return logger

    # ── Console handler ────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        fmt="%(asctime)s │ %(levelname)-8s │ %(name)-25s │ %(message)s",
        datefmt="%H:%M:%S",
    ))

    # ── File handler (rotating) ────────────────────────────────────────────
    file_handler = RotatingFileHandler(
        filename=LOG_DIR / LOG_FILE,
        maxBytes=5 * 1024 * 1024,      # 5 MB per file
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s │ %(levelname)-8s │ %(name)-25s │ %(funcName)-20s │ L%(lineno)4d │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    logger.addHandler(console)
    logger.addHandler(file_handler)

    return logger


def apply_logging_config(config: dict[str, Any]) -> None:
    """Apply configured console/file logging levels to existing handlers."""
    log_cfg = config.get("logging", {})
    console_level_name = str(log_cfg.get("console_level", "INFO")).upper()
    file_level_name = str(log_cfg.get("file_level", "DEBUG")).upper()

    console_level = getattr(logging, console_level_name, logging.INFO)
    file_level = getattr(logging, file_level_name, logging.DEBUG)

    logger = logging.getLogger("idm")
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, RotatingFileHandler):
            handler.setLevel(console_level)
        elif isinstance(handler, RotatingFileHandler):
            handler.setLevel(file_level)

    # Root level should allow most detailed handler level to pass through.
    logger.setLevel(min(console_level, file_level))
    logging.getLogger("idm.config").info(
        "Applied logging config (console=%s, file=%s)",
        console_level_name,
        file_level_name,
    )


def show_startup_error_dialog(exc: Exception) -> None:
    """Display a native startup error dialog for packaged GUI builds."""
    if not getattr(sys, "frozen", False):
        return

    try:
        import ctypes

        log_path = LOG_DIR / LOG_FILE
        message = (
            "IDM failed to start.\n\n"
            f"Error: {exc}\n\n"
            f"Logs: {log_path}\n\n"
            "Please share the log file when reporting this issue."
        )
        ctypes.windll.user32.MessageBoxW(0, message, "IDM Startup Error", 0x10)
    except Exception:
        # Never raise from crash handler paths.
        pass


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CONFIGURATION                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

DEFAULT_CONFIG_TEMPLATE: dict[str, Any] = {
    "general": {
        "download_directory": DEFAULT_DOWNLOAD_ROOT,
        "max_concurrent_downloads": 4,
        "default_chunks": 5,
        "auto_start_downloads": True,
        "minimize_to_tray": True,
        "close_button_behavior": "minimize_to_tray",
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
        "auto_apply_stable_limits_on_startup": False,
        "connection_timeout_seconds": 30,
        "read_timeout_seconds": 60,
        "max_retries": 5,
        "retry_base_delay_seconds": 3,
        "retry_max_delay_seconds": 120,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
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
        "verify_ssl": True,
    },
    "scheduler": {
        "enabled": False,
        "start_time": "02:00",
        "end_time": "06:00",
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "action_after_complete": "none",
    },
    "categories": {
        "Video": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
                  ".m4v", ".3gp", ".ts", ".m2ts"],
        "Audio": [".mp3", ".flac", ".wav", ".aac", ".ogg", ".wma", ".m4a",
                  ".opus", ".aiff"],
        "Image": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp",
                  ".ico", ".tiff", ".heic", ".avif"],
        "Document": [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt",
                     ".pptx", ".txt", ".csv", ".rtf", ".odt", ".epub"],
        "Software": [".exe", ".msi", ".dmg", ".deb", ".rpm", ".apk",
                     ".AppImage", ".snap"],
        "Archive": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
                    ".zst"],
        "Other": [],
    },
    "server": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 6800,
        "auth_token": "",
        "require_auth_token": True,
        "rate_limit": {
            "requests_per_second": 10.0,
            "burst_size": 20,
        },
        "cors_origins": [
            "http://localhost",
            "chrome-extension://*",
            "moz-extension://*",
        ],
    },
    "logging": {
        "console_level": "INFO",
        "file_level": "DEBUG",
    },
    "clipboard": {
        "monitor_enabled": False,
        "auto_capture_threshold_mb": 5,
        "supported_protocols": ["http", "https", "ftp", "magnet"],
        "exclude_patterns": [],
    },
    "file_conflicts": {
        "default_action": "rename",
        "rename_pattern": "{name} ({n}){ext}",
    },
    "advanced": {
        "dynamic_chunk_adjustment": True,
        "min_chunk_size_bytes": 262_144,
        "max_chunk_size_bytes": 52_428_800,
        "parallel_chunk_min_file_size_mb": 100,
        "speed_sample_interval_ms": 500,
        "history_retention_days": 90,
        "max_speed_history_points": 120,
        "chunk_buffer_size_bytes": 65_536,
        "chunk_prefetch_buffers": 2,
        "first_byte_timeout_seconds": 15,
        "hash_verify_on_complete": True,
        "hash_algorithm": "sha256",
        "temp_directory": "",
    },
}


def _copy_config_value(value: Any) -> Any:
    """Recursively copy config-compatible container values."""
    if isinstance(value, dict):
        return {key: _copy_config_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_config_value(item) for item in value]
    return value


def get_default_config() -> dict[str, Any]:
    """
    Return the full default configuration dictionary.

    This is used as a fallback when config.json is missing keys (forward
    compatibility) or when the file does not exist at all.
    """
    return _copy_config_value(DEFAULT_CONFIG_TEMPLATE)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively merge *override* into *base*.

    • Dict values are merged recursively.
    • All other types in *override* take precedence.
    • Keys in *base* not present in *override* are preserved.

    Returns:
        A new merged dictionary (inputs are not mutated).
    """
    result: dict[str, Any] = {}
    for key, value in base.items():
        if key in override:
            override_value = override[key]
            if isinstance(value, dict) and isinstance(override_value, dict):
                result[key] = deep_merge(value, override_value)
            else:
                result[key] = _copy_config_value(override_value)
        else:
            result[key] = _copy_config_value(value)

    for key, value in override.items():
        if key not in base:
            result[key] = _copy_config_value(value)
    return result


def _load_user_config(
    config_path: Path,
    defaults: dict[str, Any],
    log: logging.Logger,
) -> dict[str, Any]:
    """Load raw user config from disk or materialize defaults when absent."""
    if not config_path.exists():
        log.warning("Config not found at %s — writing defaults", config_path)
        save_config(config_path, defaults)
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to parse config file: %s — using defaults", exc)
        return {}

    if isinstance(loaded, dict):
        return loaded

    log.error("Config root is not an object — using defaults")
    return {}


def _write_config_if_possible(
    config_path: Path,
    config: dict[str, Any],
    log: logging.Logger,
) -> None:
    """Persist config changes without failing startup on save errors."""
    try:
        save_config(config_path, config)
    except Exception as exc:
        log.warning("Failed to persist token to config: %s", exc)


def _load_or_generate_auth_token(
    config_path: Path,
    config: dict[str, Any],
    log: logging.Logger,
) -> None:
    """Resolve the bridge auth token from secure storage or config."""
    require_auth = bool(config.get("server", {}).get("require_auth_token", True))
    if not require_auth:
        return

    try:
        from utils.credentials import get_credential_store
    except ImportError:
        get_credential_store = None

    runtime_token = str(config.get("server", {}).get("auth_token", "")).strip()

    if get_credential_store is None:
        if not runtime_token:
            config["server"]["auth_token"] = secrets.token_urlsafe(24)
            log.warning(
                "Generated bridge auth token in plaintext config (keyring unavailable)"
            )
            _write_config_if_possible(config_path, config, log)
        return

    try:
        store = get_credential_store()

        if runtime_token and store.store("api_auth_token", runtime_token):
            config["server"]["auth_token"] = ""
            runtime_token = ""
            log.info("Migrated auth token to secure storage")

        stored_token = store.retrieve("api_auth_token")
        if stored_token:
            config["server"]["auth_token"] = stored_token
            return

        generated_token = secrets.token_urlsafe(24)
        config["server"]["auth_token"] = generated_token
        if store.store("api_auth_token", generated_token):
            log.info("Generated secure bridge auth token in credential store")
        else:
            log.warning(
                "Generated bridge auth token but secure store unavailable; "
                "token will remain in runtime config"
            )
        _write_config_if_possible(config_path, config, log)
    except Exception as exc:
        log.warning("Failed to load token from secure storage: %s", exc)


def _normalize_loaded_config(
    config_path: Path,
    config: dict[str, Any],
    log: logging.Logger,
) -> dict[str, Any]:
    """Apply runtime-safe normalization and security defaults."""
    _load_or_generate_auth_token(config_path, config, log)

    dl_dir = str(config.get("general", {}).get("download_directory", "")).strip()
    config.setdefault("general", {})["download_directory"] = (
        dl_dir or DEFAULT_DOWNLOAD_ROOT
    )

    try:
        chunks_val = int(config.get("general", {}).get("default_chunks", 5))
    except (TypeError, ValueError):
        chunks_val = 5
    config["general"]["default_chunks"] = max(3, min(chunks_val, 5))

    if not config.get("network", {}).get("verify_ssl", True):
        log.error(
            "⚠ SECURITY WARNING ⚠ SSL certificate verification is DISABLED. "
            "This makes you vulnerable to Man-in-the-Middle (MITM) attacks! "
            "Only disable this for testing with self-signed certificates. "
            "Enable it in network.verify_ssl config for production use."
        )

    return config


def load_config(config_path: Path) -> dict[str, Any]:
    """
    Load configuration from *config_path*.

    If the file does not exist, a default config.json is written.
    The loaded config is always merged with defaults so that new keys
    introduced in future versions are automatically populated.

    Args:
        config_path: Absolute path to the JSON configuration file.

    Returns:
        The fully-merged configuration dictionary.
    """
    log = logging.getLogger("idm.config")
    defaults = get_default_config()

    user_config = _load_user_config(config_path, defaults, log)
    merged = deep_merge(defaults, user_config)
    merged = _normalize_loaded_config(config_path, merged, log)

    log.info("Configuration loaded from %s", config_path)
    return merged

def save_config(config_path: Path, config: dict[str, Any]) -> None:
    """
    Atomically write *config* to *config_path*.

    Uses write-to-temp-then-rename to avoid corrupt files on crash.
    """
    log = logging.getLogger("idm.config")
    config_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=config_path.parent, suffix=".tmp", prefix=".config_"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=4, ensure_ascii=False)
        # On Windows, os.replace is atomic if same volume
        os.replace(tmp_path, config_path)
        log.debug("Configuration saved to %s", config_path)
    except OSError:
        log.exception("Failed to save config")
        # Clean up temp file if rename failed
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


class BatchedConfigSaver:
    """Debounce repeated config writes and persist only the latest snapshot."""

    def __init__(
        self,
        config_path: Path,
        save_fn: Callable[[Path, dict[str, Any]], None] = save_config,
        debounce_seconds: float = 0.8,
    ) -> None:
        self._config_path = config_path
        self._save_fn = save_fn
        self._debounce_seconds = max(0.0, float(debounce_seconds))
        self._lock = threading.Lock()
        self._pending_snapshot: Optional[dict[str, Any]] = None
        self._timer: Optional[threading.Timer] = None
        self._log = logging.getLogger("idm.config")

    def schedule(self, config: dict[str, Any]) -> None:
        """Queue a debounced save using a safe snapshot of current config."""
        snapshot = _copy_config_value(config)
        with self._lock:
            self._pending_snapshot = snapshot
            if self._timer is not None:
                self._timer.cancel()

            self._timer = threading.Timer(self._debounce_seconds, self._flush_from_timer)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        """Immediately persist any pending snapshot."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            snapshot = self._pending_snapshot
            self._pending_snapshot = None

        if snapshot is not None:
            self._save_fn(self._config_path, snapshot)

    def _flush_from_timer(self) -> None:
        with self._lock:
            snapshot = self._pending_snapshot
            self._pending_snapshot = None
            self._timer = None

        if snapshot is None:
            return

        try:
            self._save_fn(self._config_path, snapshot)
        except Exception:
            self._log.exception("Failed to persist debounced config snapshot")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SINGLE-INSTANCE GUARD                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class SingleInstanceGuard:
    """
    Prevent multiple IDM instances from running simultaneously.

    Uses a lock file in the data directory.  On Windows, holding an open
    file handle prevents other processes from opening the same file with
    exclusive access.  On POSIX, ``fcntl.flock`` is used.
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._lock_file: Optional[Any] = None
        self._locked: bool = False

    def acquire(self) -> bool:
        """Attempt to acquire the lock. Returns True on success."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                # On Windows, open with exclusive access
                import msvcrt
                self._lock_file = open(self._lock_path, "w", encoding="utf-8")
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                self._lock_file = open(self._lock_path, "w", encoding="utf-8")
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Write PID so we can identify the owning process
            self._lock_file.write(str(os.getpid()))
            self._lock_file.flush()
            self._locked = True
            return True

        except (OSError, PermissionError, BlockingIOError):
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None
            return False

    def release(self) -> None:
        """Release the lock and remove the lock file."""
        if self._lock_file:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
            except OSError:
                pass
            finally:
                self._lock_file = None

            try:
                self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

            self._locked = False


def _read_lock_owner_pid(lock_path: Path) -> Optional[int]:
    """Read owning PID from the lock file, if available."""
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return int(raw)
    except Exception:
        return None


def try_activate_existing_window(owner_pid: Optional[int] = None) -> bool:
    """
    On Windows, try to restore and focus an already-running IDM window.

    Returns:
        True if a matching top-level window was found and activation was attempted.
    """
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        # Constants from WinUser.h
        SW_RESTORE = 9

        # Owner process may still be starting; poll briefly for its main window.
        for _ in range(25):
            found_hwnd = ctypes.c_void_p(0)

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def enum_windows_proc(hwnd: wintypes.HWND, _lparam: wintypes.LPARAM) -> bool:
                if not user32.IsWindow(hwnd):
                    return True

                if owner_pid:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value == owner_pid:
                        found_hwnd.value = int(hwnd)
                        return False

                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True

                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = (buffer.value or "").strip()
                if not title:
                    return True

                lower = title.lower()
                # Match both full and shorter titles used by this app.
                if (
                    "internet download manager" in lower
                    or ("idm" in lower and "download" in lower)
                    or lower == "idm"
                ):
                    found_hwnd.value = int(hwnd)
                    return False

                return True

            user32.EnumWindows(enum_windows_proc, 0)

            if found_hwnd.value:
                hwnd = found_hwnd.value
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
                return True

            time.sleep(0.2)

        return False

    except Exception:
        logging.getLogger("idm").warning(
            "Failed to activate existing IDM window", exc_info=True
        )
        return False


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ENGINE THREAD  — runs the asyncio download engine                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class EngineThread(threading.Thread):
    """
    Dedicated thread that hosts the asyncio event loop for the download engine.

    The Qt UI schedules work on this loop via ``asyncio.run_coroutine_threadsafe``.

    Usage::

        engine = EngineThread()
        engine.start()
        engine.wait_ready()

        future = asyncio.run_coroutine_threadsafe(some_coro(), engine.loop)

        engine.stop()       # graceful shutdown
    """

    def __init__(self) -> None:
        super().__init__(name="IDM-EngineThread", daemon=True)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready_event = threading.Event()
        self._log = logging.getLogger("idm.engine_thread")

    def run(self) -> None:
        """Thread entry — create and run the asyncio event loop."""
        self._log.info("Engine thread starting…")
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # Signal that the loop is ready for work
        self._ready_event.set()
        self._log.info("Engine event loop running")

        try:
            self.loop.run_forever()
        finally:
            # Drain remaining tasks before closing the loop
            pending = asyncio.all_tasks(self.loop)
            if pending:
                self._log.info("Cancelling %d pending tasks…", len(pending))
                for task in pending:
                    task.cancel()
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()
            self._log.info("Engine event loop closed")

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """
        Block until the event loop is running.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            True if the loop is ready, False on timeout.
        """
        return self._ready_event.wait(timeout)

    def stop(self) -> None:
        """Gracefully stop the event loop and join the thread."""
        if self.loop and self.loop.is_running():
            self._log.info("Stopping engine event loop…")
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.join(timeout=10)

    def run_coroutine(self, coro: Any) -> Any:
        """
        Schedule a coroutine on the engine loop from any thread.

        Returns:
            A ``concurrent.futures.Future`` that resolves when the coroutine completes.
        """
        if not self.loop or not self.loop.is_running():
            raise RuntimeError("Engine loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SERVER THREAD  — runs the FastAPI / uvicorn bridge server                  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class ServerThread(threading.Thread):
    """
    Dedicated thread for the FastAPI bridge server (uvicorn).

    The browser extension communicates with IDM via REST + WebSocket
    on ``localhost:6800`` (configurable).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 6800) -> None:
        super().__init__(name="IDM-ServerThread", daemon=True)
        self.host = host
        self.port = port
        self._server: Optional[Any] = None  # uvicorn.Server
        self._log = logging.getLogger("idm.server_thread")
        self._ready_event = threading.Event()

    def run(self) -> None:
        """Thread entry — start the uvicorn ASGI server."""
        self._log.info("Server thread starting on %s:%d …", self.host, self.port)
        try:
            import uvicorn

            # Import the FastAPI app — either patched by _setup_server_app()
            # or created fresh as a fallback.
            try:
                import server.api as api_module
                if hasattr(api_module, "app"):
                    app = api_module.app
                    self._log.info("Loaded patched FastAPI app from server.api")
                else:
                    app = api_module.create_app()
                    self._log.info("Created default FastAPI app from server.api")
            except ImportError:
                self._log.warning(
                    "server.api not found — starting placeholder app. "
                    "Build server/api.py to enable the bridge."
                )
                from fastapi import FastAPI
                app = FastAPI(title="IDM Bridge (placeholder)")

                @app.get("/status")
                async def _placeholder_status() -> dict[str, Any]:
                    return {"status": "ok", "placeholder": True, "downloads": []}

            config = uvicorn.Config(
                app=app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False,
                log_config=None,
            )
            self._server = uvicorn.Server(config)

            # Signal readiness once the server socket is bound
            self._ready_event.set()
            self._server.run()

        except Exception:
            self._log.exception("Server thread failed")
            self._ready_event.set()  # unblock waiters even on failure

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Block until the server is ready (or has failed)."""
        return self._ready_event.wait(timeout)

    def stop(self) -> None:
        """Signal uvicorn to shut down gracefully."""
        if self._server:
            self._log.info("Stopping bridge server…")
            self._server.should_exit = True
        self.join(timeout=10)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DARK THEME STYLESHEET                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

DARK_THEME_QSS: str = """
/* ═══════════════════════════════════════════════════════════════════════════
   IDM Dark Theme — Modern Flat Design
   Color palette:
       bg-deep      #1a1a2e     (main background)
       bg-card      #16213e     (cards, panels)
       bg-input     #1f2a4d     (input fields)
       bg-hover     #22325f     (hover states)
       border       #304574     (borders, separators)
       text-primary #f5f7ff     (main text)
       text-muted   #aab3d6     (secondary text)
       accent       #0f3460     (primary accent)
       highlight    #e94560     (critical highlight)
       green        #2ecc71     (success / complete)
       yellow       #f6c445     (warning / paused)
       red          #ef4444     (error / stopped)
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Global ──────────────────────────────────────────────────────────────── */
QWidget {
    background-color: #1a1a2e;
    color: #f5f7ff;
    font-family: "Segoe UI", "Ubuntu", "SF Pro Text", sans-serif;
    font-size: 13px;
    selection-background-color: #0f3460;
    selection-color: #ffffff;
}

/* ── Main Window ─────────────────────────────────────────────────────────── */
QMainWindow {
    background-color: #1a1a2e;
}

QMainWindow::separator {
    background-color: #304574;
    width: 1px;
    height: 1px;
}

/* ── Menu Bar ────────────────────────────────────────────────────────────── */
QMenuBar {
    background-color: #16213e;
    border-bottom: 1px solid #304574;
    padding: 2px 0;
}

QMenuBar::item {
    padding: 6px 12px;
    border-radius: 4px;
    margin: 2px 1px;
}

QMenuBar::item:selected {
    background-color: #22325f;
}

QMenu {
    background-color: #16213e;
    border: 1px solid #304574;
    border-radius: 8px;
    padding: 4px;
}

QMenu::item {
    padding: 8px 32px 8px 16px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #22325f;
    color: #e94560;
}

QMenu::separator {
    height: 1px;
    background-color: #304574;
    margin: 4px 8px;
}

/* ── Tool Bar ────────────────────────────────────────────────────────────── */
QToolBar {
    background-color: #16213e;
    border-bottom: 1px solid #304574;
    spacing: 4px;
    padding: 4px 8px;
}

QToolButton {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 12px;
    color: #e6edf3;
    font-weight: 500;
}

QToolButton:hover {
    background-color: #21262d;
    border-color: #30363d;
}

QToolButton:pressed {
    background-color: #30363d;
}

/* ── Push Buttons ────────────────────────────────────────────────────────── */
QPushButton {
    background-color: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 20px;
    color: #e6edf3;
    font-weight: 500;
    min-height: 20px;
}

QPushButton:hover {
    background-color: #30363d;
    border-color: #58a6ff;
}

QPushButton:pressed {
    background-color: #0d1117;
}

QPushButton:disabled {
    background-color: #161b22;
    color: #484f58;
    border-color: #21262d;
}

QPushButton#primaryButton, QPushButton[primary="true"] {
    background-color: #238636;
    border-color: #2ea043;
    color: #ffffff;
}

QPushButton#primaryButton:hover, QPushButton[primary="true"]:hover {
    background-color: #2ea043;
    border-color: #3fb950;
}

QPushButton#dangerButton, QPushButton[danger="true"] {
    background-color: #da3633;
    border-color: #f85149;
    color: #ffffff;
}

QPushButton#dangerButton:hover, QPushButton[danger="true"]:hover {
    background-color: #f85149;
}

/* ── Input Fields ────────────────────────────────────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {
    background-color: #1c2128;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 12px;
    color: #e6edf3;
}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #58a6ff;
    outline: none;
}

QLineEdit:disabled, QTextEdit:disabled {
    background-color: #161b22;
    color: #484f58;
}

/* ── Combo Box ───────────────────────────────────────────────────────────── */
QComboBox {
    background-color: #1c2128;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 12px;
    color: #e6edf3;
    min-height: 20px;
}

QComboBox:hover {
    border-color: #58a6ff;
}

QComboBox::drop-down {
    border: none;
    width: 28px;
}

QComboBox QAbstractItemView {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    selection-background-color: #21262d;
    selection-color: #58a6ff;
    padding: 4px;
}

/* ── Tables ──────────────────────────────────────────────────────────────── */
QTableWidget, QTableView, QTreeView, QListView {
    background-color: #0d1117;
    alternate-background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    gridline-color: #21262d;
    outline: none;
}

QTableWidget::item, QTableView::item, QTreeView::item, QListView::item {
    padding: 8px 12px;
    border: none;
}

QTableWidget::item:selected, QTableView::item:selected,
QTreeView::item:selected, QListView::item:selected {
    background-color: #1c3a5f;
    color: #e6edf3;
}

QTableWidget::item:hover, QTableView::item:hover {
    background-color: #21262d;
}

QHeaderView::section {
    background-color: #161b22;
    color: #8b949e;
    border: none;
    border-bottom: 2px solid #30363d;
    border-right: 1px solid #21262d;
    padding: 10px 12px;
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
}

QHeaderView::section:hover {
    background-color: #21262d;
    color: #e6edf3;
}

/* ── Scroll Bars ─────────────────────────────────────────────────────────── */
QScrollBar:vertical {
    background-color: transparent;
    width: 10px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background-color: #30363d;
    border-radius: 5px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background-color: #484f58;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar:horizontal {
    background-color: transparent;
    height: 10px;
    margin: 0;
}

QScrollBar::handle:horizontal {
    background-color: #30363d;
    border-radius: 5px;
    min-width: 30px;
}

QScrollBar::handle:horizontal:hover {
    background-color: #484f58;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ── Progress Bars ───────────────────────────────────────────────────────── */
QProgressBar {
    background-color: #21262d;
    border: none;
    border-radius: 4px;
    text-align: center;
    color: #e6edf3;
    font-weight: 600;
    font-size: 11px;
    min-height: 8px;
    max-height: 22px;
}

QProgressBar::chunk {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 #1f6feb,
        stop:1 #58a6ff
    );
    border-radius: 4px;
}

/* ── Tab Widget ──────────────────────────────────────────────────────────── */
QTabWidget::pane {
    background-color: #0d1117;
    border: 1px solid #30363d;
    border-top: none;
    border-radius: 0 0 8px 8px;
}

QTabBar::tab {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-bottom: none;
    border-radius: 8px 8px 0 0;
    padding: 10px 20px;
    color: #8b949e;
    font-weight: 500;
    margin-right: 2px;
}

QTabBar::tab:selected {
    background-color: #0d1117;
    color: #e6edf3;
    border-bottom: 2px solid #58a6ff;
}

QTabBar::tab:hover:!selected {
    background-color: #21262d;
    color: #e6edf3;
}

/* ── Group Box ───────────────────────────────────────────────────────────── */
QGroupBox {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    margin-top: 16px;
    padding: 16px 12px 12px 12px;
    font-weight: 600;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 4px 12px;
    color: #58a6ff;
    font-size: 13px;
}

/* ── Check Box & Radio Button ────────────────────────────────────────────── */
QCheckBox, QRadioButton {
    spacing: 8px;
    color: #e6edf3;
}

QCheckBox::indicator, QRadioButton::indicator {
    width: 18px;
    height: 18px;
    border: 2px solid #30363d;
    border-radius: 4px;
    background-color: #1c2128;
}

QRadioButton::indicator {
    border-radius: 10px;
}

QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background-color: #58a6ff;
    border-color: #58a6ff;
}

QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: #58a6ff;
}

/* ── Tooltips ────────────────────────────────────────────────────────────── */
QToolTip {
    background-color: #1c2128;
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}

/* ── Status Bar ──────────────────────────────────────────────────────────── */
QStatusBar {
    background-color: #161b22;
    border-top: 1px solid #30363d;
    color: #8b949e;
    font-size: 12px;
    padding: 2px 8px;
}

QStatusBar::item {
    border: none;
}

/* ── Splitter ────────────────────────────────────────────────────────────── */
QSplitter::handle {
    background-color: #30363d;
}

QSplitter::handle:horizontal {
    width: 2px;
}

QSplitter::handle:vertical {
    height: 2px;
}

/* ── Dock Widget ─────────────────────────────────────────────────────────── */
QDockWidget {
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}

QDockWidget::title {
    background-color: #161b22;
    border: 1px solid #30363d;
    padding: 8px;
    font-weight: 600;
    text-align: left;
}

/* ── Dialog ──────────────────────────────────────────────────────────────── */
QDialog {
    background-color: #0d1117;
}

/* ── Label ───────────────────────────────────────────────────────────────── */
QLabel {
    color: #e6edf3;
    background-color: transparent;
}

QLabel[heading="true"] {
    font-size: 18px;
    font-weight: 700;
    color: #ffffff;
}

QLabel[subheading="true"] {
    font-size: 14px;
    color: #8b949e;
}

QLabel[status="complete"] { color: #3fb950; }
QLabel[status="error"]    { color: #f85149; }
QLabel[status="warning"]  { color: #d29922; }
QLabel[status="info"]     { color: #58a6ff; }
"""


LIGHT_THEME_QSS: str = """
/* ═══════════════════════════════════════════════════════════════════════════
   IDM Light Theme — Clean, Minimal
   ═══════════════════════════════════════════════════════════════════════════ */

QWidget {
    background-color: #ffffff;
    color: #1f2328;
    font-family: "Segoe UI", "Inter", "Roboto", sans-serif;
    font-size: 13px;
    selection-background-color: #0969da;
    selection-color: #ffffff;
}

QMainWindow { background-color: #f6f8fa; }

QMenuBar {
    background-color: #f6f8fa;
    border-bottom: 1px solid #d0d7de;
}

QMenuBar::item:selected { background-color: #eaeef2; }

QMenu {
    background-color: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 8px;
}

QMenu::item:selected { background-color: #eaeef2; color: #0969da; }

QToolBar {
    background-color: #f6f8fa;
    border-bottom: 1px solid #d0d7de;
}

QToolButton:hover { background-color: #eaeef2; border-color: #d0d7de; }

QPushButton {
    background-color: #f6f8fa;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 8px 20px;
    color: #1f2328;
    font-weight: 500;
}

QPushButton:hover { background-color: #eaeef2; border-color: #0969da; }

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {
    background-color: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 8px 12px;
}

QLineEdit:focus { border-color: #0969da; }

QTableWidget, QTableView, QTreeView, QListView {
    background-color: #ffffff;
    alternate-background-color: #f6f8fa;
    border: 1px solid #d0d7de;
    gridline-color: #eaeef2;
}

QHeaderView::section {
    background-color: #f6f8fa;
    color: #656d76;
    border-bottom: 2px solid #d0d7de;
}

QProgressBar { background-color: #eaeef2; border-radius: 4px; }
QProgressBar::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #0969da, stop:1 #54aeff);
    border-radius: 4px;
}

QTabBar::tab { background-color: #f6f8fa; border: 1px solid #d0d7de; }
QTabBar::tab:selected { background-color: #ffffff; border-bottom: 2px solid #0969da; }

QGroupBox { background-color: #f6f8fa; border: 1px solid #d0d7de; border-radius: 8px; }
QGroupBox::title { color: #0969da; }

QStatusBar { background-color: #f6f8fa; border-top: 1px solid #d0d7de; color: #656d76; }

QScrollBar:vertical { width: 10px; }
QScrollBar::handle:vertical { background-color: #d0d7de; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background-color: #afb8c1; }
QScrollBar:horizontal { height: 10px; }
QScrollBar::handle:horizontal { background-color: #d0d7de; border-radius: 5px; }

QToolTip { background-color: #1f2328; color: #ffffff; border-radius: 6px; padding: 6px 10px; }
"""


def get_theme_stylesheet(theme: str) -> str:
    """
    Return the Qt stylesheet string for the given theme name.

    Args:
        theme: Either 'dark' or 'light'.

    Returns:
        A complete QSS stylesheet string.
    """
    if theme == "light":
        return LIGHT_THEME_QSS
    return DARK_THEME_QSS


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ARGUMENT PARSER                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="idm",
        description="IDM — Internet Download Manager",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {APP_VERSION}",
    )
    parser.add_argument(
        "--config", type=Path, default=RUNTIME_DIR / CONFIG_FILE,
        help="Path to config.json (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    parser.add_argument(
        "--no-server", action="store_true",
        help="Do not start the browser-bridge API server",
    )
    parser.add_argument(
        "--no-tray", action="store_true",
        help="Do not show the system-tray icon",
    )
    parser.add_argument(
        "--minimized", action="store_true",
        help="Start minimized to system tray",
    )
    parser.add_argument(
        "--portable", action="store_true",
        help="Store all data relative to the executable (portable mode)",
    )
    return parser.parse_args()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DIRECTORY BOOTSTRAP                                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def ensure_directories(config: dict[str, Any]) -> None:
    """
    Create all required directories if they do not exist.

    Directories created:
        • Download directory (from config)
        • Data directory (SQLite DB)
        • Logs directory
        • Chunks directory (temporary chunk storage)
        • i18n directory (translations)
    """
    log = logging.getLogger("idm.bootstrap")

    dirs = [
        Path(config["general"]["download_directory"]),
        DATA_DIR,
        LOG_DIR,
        CHUNKS_DIR,
        I18N_DIR,
    ]

    # Optional temp directory override
    temp_dir = config.get("advanced", {}).get("temp_directory", "")
    if temp_dir:
        dirs.append(Path(temp_dir))

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        log.debug("Directory ensured: %s", d)

    # Create category sub-directories inside the download directory
    download_dir = Path(config["general"]["download_directory"])
    for category in config.get("categories", {}):
        cat_dir = download_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        log.debug("Category directory ensured: %s", cat_dir)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ASYNC HELPERS                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

async def _init_subsystems(storage: Any, network: Any) -> None:
    """Initialize storage and network on the engine event loop."""
    await storage.initialize()
    await network.initialize()


async def _shutdown_subsystems(storage: Any, network: Any) -> None:
    """Gracefully close storage and network."""
    try:
        await network.close()
    except Exception:
        logging.getLogger("idm").warning("Network shutdown error", exc_info=True)
    try:
        await storage.close()
    except Exception:
        logging.getLogger("idm").warning("Storage shutdown error", exc_info=True)


def _install_shutdown_diagnostics(
    app: Any,
    window: Any,
    log: logging.Logger,
) -> Callable[[], str]:
    """
    Install runtime diagnostics that record why the app is shutting down.

    This does not alter shutdown behavior; it only logs and tracks likely
    triggers so sporadic exits can be diagnosed from logs.
    """
    state: dict[str, str] = {"reason": "unknown"}

    def mark(reason: str) -> None:
        if state["reason"] == "unknown":
            state["reason"] = reason
            log.warning("Shutdown reason marked: %s", reason)

    # Qt lifecycle diagnostics
    try:
        app.lastWindowClosed.connect(
            lambda: mark("lastWindowClosed signal emitted")
        )
        app.aboutToQuit.connect(
            lambda: log.warning(
                "QApplication aboutToQuit fired "
                "(reason=%s, window_visible=%s, window_minimized=%s)",
                state["reason"],
                bool(window.isVisible()),
                bool(window.isMinimized()),
            )
        )
    except Exception:
        log.warning("Failed to attach Qt shutdown diagnostics", exc_info=True)

    # Python-level uncaught exceptions
    original_excepthook = sys.excepthook

    def _exception_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: Any,
    ) -> None:
        mark("uncaught_exception_main_thread")
        log.critical(
            "Uncaught exception in main thread", exc_info=(exc_type, exc_value, exc_tb)
        )
        original_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _exception_hook

    if hasattr(threading, "excepthook"):
        original_thread_hook = threading.excepthook

        def _thread_exception_hook(args: threading.ExceptHookArgs) -> None:
            mark(f"uncaught_exception_thread:{args.thread.name if args.thread else 'unknown'}")
            exc_type = args.exc_type or Exception
            exc_value = args.exc_value or Exception("Unknown thread exception")
            log.critical(
                "Uncaught exception in thread",
                exc_info=(exc_type, exc_value, args.exc_traceback),
            )
            original_thread_hook(args)

        threading.excepthook = _thread_exception_hook

    return lambda: state["reason"]


def _setup_server_app(
    engine: Any, storage: Any, config: dict[str, Any]
) -> Any:
    """
    Patch the server.api module so ServerThread picks up a fully-configured
    FastAPI app instead of the placeholder.
    """
    import server.api as api_module

    app = api_module.create_app(engine=engine, storage=storage, config=config)

    # Register WebSocket endpoint
    from server.websocket import ConnectionManager, register_websocket
    ws_manager = ConnectionManager()
    register_websocket(app, ws_manager)

    # Monkey-patch so ServerThread's import picks up the real app
    api_module.app = app  # type: ignore[attr-defined]
    return ws_manager


def _set_startup_registration(enabled: bool, log: logging.Logger) -> None:
    """Register/unregister IDM startup command under HKCU Run on Windows."""
    if not sys.platform.startswith("win"):
        return

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    value_name = "IDMBridge"

    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}"'
    else:
        command = f'"{sys.executable}" "{BASE_DIR / "main.py"}"'

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
                log.info("Enabled start-with-Windows registration")
            else:
                try:
                    winreg.DeleteValue(key, value_name)
                except FileNotFoundError:
                    pass
                log.info("Disabled start-with-Windows registration")
    except Exception:
        log.warning("Failed to update start-with-Windows registration", exc_info=True)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  MAIN APPLICATION                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def main() -> int:
    """
    Application entry point.

    Returns:
        Exit code (0 = success, 1 = error, 2 = already running).
    """
    # ── 1. Parse arguments ─────────────────────────────────────────────────
    args = parse_args()

    # Bootstrap context boundaries: startup/shutdown + infra adapters.
    from bootstrap import (
        EngineThread as BootstrapEngineThread,
        ServerThread as BootstrapServerThread,
        init_subsystems as bootstrap_init_subsystems,
        setup_server_app as bootstrap_setup_server_app,
        install_shutdown_diagnostics as bootstrap_install_shutdown_diagnostics,
    )

    # ── 2. Setup logging ───────────────────────────────────────────────────
    log = setup_logging(args.log_level)
    log.info("═" * 60)
    log.info("  %s  v%s", APP_NAME, APP_VERSION)
    log.info("  Python %s on %s", sys.version.split()[0], sys.platform)
    log.info("═" * 60)

    # ── 3. Load configuration ──────────────────────────────────────────────
    config = load_config(args.config)
    _set_startup_registration(bool(config.get("general", {}).get("start_with_system", False)), log)
    if config.get("network", {}).get("auto_apply_stable_limits_on_startup", False):
        changed, global_kbps, per_download_kbps, _ = apply_stable_limits_to_config(config)
        if changed:
            save_config(args.config, config)
            logging.getLogger("idm.config").info(
                "Auto-applied stable limits on startup: global=%d KB/s, per-download=%d KB/s",
                global_kbps,
                per_download_kbps,
            )
    apply_logging_config(config)
    log.info("Download directory: %s", config["general"]["download_directory"])

    # ── 4. Single-instance guard ───────────────────────────────────────────
    lock = SingleInstanceGuard(DATA_DIR / LOCK_FILE)
    if not lock.acquire():
        owner_pid = _read_lock_owner_pid(DATA_DIR / LOCK_FILE)
        activated = try_activate_existing_window(owner_pid=owner_pid)
        log.error(
            "Another instance of IDM is already running. "
            "Close it first or delete %s", DATA_DIR / LOCK_FILE
        )
        if activated:
            log.info("Activated existing IDM window")
        else:
            log.info("Existing instance detected; activation was not possible yet")
        return 2

    try:
        # ── 5. Ensure directories exist ────────────────────────────────────
        ensure_directories(config)

        # ── 6. Create QApplication ─────────────────────────────────────────
        from PyQt6.QtGui import QFont
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QTranslator, QTimer, QObject, pyqtSignal, Qt

        # High-DPI scaling (enabled by default in Qt6, but let's be explicit)
        app = QApplication(sys.argv)
        app.setApplicationName(APP_NAME)
        app.setApplicationVersion(APP_VERSION)
        app.setOrganizationName(APP_ORG)

        # Use platform-specific sans-serif defaults for cleaner native typography.
        if sys.platform.startswith("win"):
            app.setFont(QFont("Segoe UI", 10))
        elif sys.platform == "darwin":
            app.setFont(QFont("SF Pro Text", 10))
        else:
            app.setFont(QFont("Ubuntu", 10))

        # ── 7. Apply theme ─────────────────────────────────────────────────
        theme = config["general"].get("theme", "dark")
        app.setStyleSheet(get_theme_stylesheet(theme))
        log.info("Theme applied: %s", theme)

        # ── 8. Load translations (i18n) ───────────────────────────────────
        language = config["general"].get("language", "en")
        if language != "en":
            translator = QTranslator()
            qm_file = I18N_DIR / f"idm_{language}.qm"
            if qm_file.exists() and translator.load(str(qm_file)):
                app.installTranslator(translator)
                log.info("Translation loaded: %s", language)
            else:
                log.warning("Translation file not found: %s", qm_file)

        # ── 9. Start the async engine thread ──────────────────────────────
        engine_thread = BootstrapEngineThread()
        engine_thread.start()
        if not engine_thread.wait_ready(timeout=10):
            log.error("Engine thread failed to start within 10 seconds")
            return 1
        log.info("✓ Engine thread ready")

        # ── 10. Initialize core subsystems on the engine loop ─────────────
        from core.storage import StorageManager
        from core.network import NetworkManager
        from core.downloader import DownloadEngine

        storage = StorageManager(DATA_DIR / DB_FILE)
        network = NetworkManager(config)

        # Initialize storage and network asynchronously
        init_future = engine_thread.run_coroutine(bootstrap_init_subsystems(storage, network))
        init_future.result(timeout=15)
        log.info("✓ Storage and network initialized")

        # Create the download engine (with null callbacks initially)
        download_engine = DownloadEngine(
            storage=storage,
            network=network,
            config=config,
            chunks_dir=CHUNKS_DIR,
        )
        engine_thread.run_coroutine(download_engine.start()).result(timeout=5)
        log.info("✓ Download engine started")

        # ── 11. Start clipboard monitor & scheduler ───────────────────────
        from utils.clipboard_monitor import ClipboardMonitor
        from utils.scheduler import DownloadScheduler

        clipboard_monitor: Optional[ClipboardMonitor] = None
        if config.get("clipboard", {}).get("monitor_enabled", True):
            async def _on_clipboard_url(url: str) -> None:
                result = await _request_add_with_file_info({
                    "url": url,
                    "priority": "normal",
                    "category": "Other",
                    "source": "clipboard",
                })
                if not result.get("download_id"):
                    log.info(
                        "Clipboard capture skipped: %s",
                        result.get("error", "unknown reason"),
                    )

            clipboard_monitor = ClipboardMonitor(
                config, on_url_detected=_on_clipboard_url
            )
            log.info("Clipboard monitor initialized (start deferred until UI bridge is ready)")

        scheduler = DownloadScheduler(config)
        scheduler_running = False
        if scheduler.enabled:
            engine_thread.run_coroutine(scheduler.start(download_engine))
            log.info("✓ Scheduler started")
            scheduler_running = True

        # ── 12. Start the bridge server thread ────────────────────────────
        server_thread: Optional[BootstrapServerThread] = None
        ws_manager: Optional[Any] = None
        bridge_pairing_code: str = ""
        api_dialog_ready = threading.Event()
        api_dialog_handler: dict[str, Any] = {"handler": None}

        async def _deferred_api_add_interceptor(req: Any) -> dict[str, str]:
            """Block API add requests until UI dialog handling is ready."""
            loop = asyncio.get_running_loop()
            ready = await loop.run_in_executor(None, api_dialog_ready.wait, 20.0)
            if not ready:
                return {
                    "error": (
                        "Download info dialog is still initializing. "
                        "Please retry in a moment."
                    )
                }

            handler = api_dialog_handler.get("handler")
            if handler is None:
                return {"error": "Download info dialog handler is unavailable"}

            return await handler(req)

        if config["server"]["enabled"] and not args.no_server:
            # Inject real engine/storage into the FastAPI app
            ws_manager = bootstrap_setup_server_app(download_engine, storage, config)

            try:
                import server.api as api_module

                app_obj = getattr(api_module, "app", None)
                if app_obj is not None:
                    app_obj.state.add_download_interceptor = _deferred_api_add_interceptor
                    bridge_pairing_code = str(getattr(app_obj.state, "pairing_code", "") or "")
                    if not bridge_pairing_code:
                        app_obj.state.pairing_code = api_module._generate_pairing_code()
                        app_obj.state.pairing_code_expires_at = time.time() + 5 * 60
                        bridge_pairing_code = str(app_obj.state.pairing_code or "")
            except Exception:
                log.exception("Failed to install deferred API add interceptor")

            server_thread = BootstrapServerThread(
                host=config["server"]["host"],
                port=config["server"]["port"],
            )
            server_thread.start()
            log.info(
                "✓ Bridge server starting on %s:%d",
                config["server"]["host"],
                config["server"]["port"],
            )
        else:
            log.info("Bridge server disabled")

        # ── 13. Create main window ────────────────────────────────────────
        from ui.main_window import MainWindow
        from ui.tray import SystemTray

        window = MainWindow(config=config)
        log.info("✓ MainWindow created")
        window.set_bridge_status(
            enabled=bool(config.get("server", {}).get("enabled", True) and not args.no_server),
            host=str(config.get("server", {}).get("host", "127.0.0.1")),
            port=int(config.get("server", {}).get("port", 6800)),
            pairing_code=bridge_pairing_code,
        )

        tray: Optional[SystemTray] = None
        if SystemTray.isSystemTrayAvailable():
            tray = SystemTray(config=config, parent=window)

            def _show_window_from_tray() -> None:
                window.show()
                window.raise_()
                window.activateWindow()

            tray.show_window_requested.connect(_show_window_from_tray)
            tray.add_url_requested.connect(window._on_add_url)
            tray.pause_all_requested.connect(
                lambda: engine_thread.run_coroutine(download_engine.pause_all())
            )
            tray.resume_all_requested.connect(
                lambda: engine_thread.run_coroutine(download_engine.resume_all())
            )

            def _show_pairing_code_from_tray() -> None:
                def _display_code(value: str) -> str:
                    raw = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
                    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else raw

                try:
                    import server.api as api_module

                    app_obj = getattr(api_module, "app", None)
                    code = str(getattr(app_obj.state, "pairing_code", "") or "") if app_obj else ""
                    if not code:
                        # No active code (paired or expired): generate a fresh one on demand.
                        if app_obj is not None:
                            app_obj.state.pairing_code = api_module._generate_pairing_code()
                            app_obj.state.pairing_code_expires_at = time.time() + 5 * 60
                            code = str(app_obj.state.pairing_code or "")
                            tray.set_pairing_code(code)
                            window.set_bridge_status(
                                enabled=bool(config.get("server", {}).get("enabled", True) and not args.no_server),
                                host=str(config.get("server", {}).get("host", "127.0.0.1")),
                                port=int(config.get("server", {}).get("port", 6800)),
                                pairing_code=code,
                            )
                        else:
                            code = "Unavailable"
                    tray.showMessage(
                        "IDM Pairing Code",
                        f"Current code: {_display_code(code)}",
                        tray.MessageIcon.Information,
                        5000,
                    )
                except Exception:
                    log.warning("Failed to show pairing code from tray", exc_info=True)

            def _reset_pairing_from_tray() -> None:
                def _display_code(value: str) -> str:
                    raw = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
                    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else raw

                try:
                    import server.api as api_module

                    app_obj = getattr(api_module, "app", None)
                    if app_obj is None:
                        return

                    app_obj.state.session_tokens.clear()
                    app_obj.state.pairing_code = api_module._generate_pairing_code()
                    app_obj.state.pairing_code_expires_at = time.time() + 5 * 60
                    new_code = str(app_obj.state.pairing_code or "")
                    tray.set_pairing_code(new_code)
                    window.set_bridge_status(
                        enabled=bool(config.get("server", {}).get("enabled", True) and not args.no_server),
                        host=str(config.get("server", {}).get("host", "127.0.0.1")),
                        port=int(config.get("server", {}).get("port", 6800)),
                        pairing_code=new_code,
                    )
                    tray.showMessage(
                        "Pairing Reset",
                        f"New pairing code: {_display_code(new_code)}",
                        tray.MessageIcon.Information,
                        6000,
                    )
                except Exception:
                    log.warning("Failed to reset pairing from tray", exc_info=True)

            def _confirm_quit_from_tray() -> None:
                from PyQt6.QtWidgets import QMessageBox

                reply = QMessageBox.question(
                    window,
                    "Quit IDM",
                    "Are you sure you want to quit IDM?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    app.quit()

            tray.show_pairing_code_requested.connect(_show_pairing_code_from_tray)
            tray.reset_pairing_requested.connect(_reset_pairing_from_tray)
            tray.quit_requested.connect(_confirm_quit_from_tray)
            tray.show()
            tray.set_pairing_code(bridge_pairing_code)

            # Allow MainWindow to honor minimize-to-tray behavior.
            setattr(window, "_tray", tray)
            log.info("✓ System tray initialized")
        else:
            log.warning("System tray is not available on this platform/session")

        class ApiAddDialogBridge(QObject):
            """Bridge server-thread add requests to the Qt UI thread."""

            request = pyqtSignal(dict, object)

        api_add_bridge = ApiAddDialogBridge()

        def _show_file_info_for_api_add(
            request_data: dict[str, Any],
            completion: concurrent.futures.Future,
        ) -> None:
            """Open File Info dialog before creating API-triggered downloads."""
            try:
                from ui.file_info_dialog import FileInfoDialog

                url = str(request_data.get("url", "")).strip()
                if not url:
                    if not completion.done():
                        completion.set_result({"error": "Invalid URL"})
                    return

                initial_filename = str(request_data.get("filename") or "").strip()
                raw_save_path = str(request_data.get("save_path") or "").strip()

                if raw_save_path:
                    save_dir = str(Path(raw_save_path).parent)
                else:
                    save_dir = str(
                        config.get("general", {}).get(
                            "download_directory", DEFAULT_DOWNLOAD_ROOT
                        )
                    )

                handled = {"accepted": False}

                dialog = FileInfoDialog(
                    url=url,
                    filename=initial_filename,
                    save_dir=save_dir,
                    config=config,
                    parent=window,
                )

                def _on_dialog_accepted(file_info_data: dict[str, Any]) -> None:
                    handled["accepted"] = True

                    final_filename = str(
                        file_info_data.get("filename") or initial_filename
                    ).strip()
                    final_save_dir = str(file_info_data.get("save_dir") or save_dir).strip()

                    final_save_path = ""
                    if final_filename and final_save_dir:
                        final_save_path = str(Path(final_save_dir) / final_filename)
                    elif raw_save_path:
                        final_save_path = raw_save_path

                    add_future = engine_thread.run_coroutine(
                        download_engine.add_download(
                            url=url,
                            filename=final_filename,
                            category=str(
                                file_info_data.get("category")
                                or request_data.get("category")
                                or "Other"
                            ),
                            save_path=final_save_path,
                            priority=str(request_data.get("priority") or "normal"),
                            referer=request_data.get("referer"),
                            cookies=request_data.get("cookies"),
                            hash_expected=request_data.get("hash_expected"),
                            start_immediately=bool(file_info_data.get("immediate", True)),
                            # UI dialog selection is explicit user confirmation for custom paths.
                            allow_out_of_root=True,
                        )
                    )

                    def _on_added(done_future: Any) -> None:
                        try:
                            dl_id = str(done_future.result())
                        except Exception as exc:
                            if not completion.done():
                                completion.set_result({"error": str(exc)})
                            log.exception("API add failed after dialog acceptance")
                            return

                        if not completion.done():
                            completion.set_result({"download_id": dl_id})

                    add_future.add_done_callback(_on_added)

                dialog.download_accepted.connect(_on_dialog_accepted)

                window.show()
                window.raise_()
                window.activateWindow()

                dialog.exec()

                if not handled["accepted"] and not completion.done():
                    completion.set_result({"error": "Download canceled from file info dialog"})
            except Exception as exc:
                if not completion.done():
                    completion.set_result({"error": str(exc)})
                log.exception("Failed to process API add request in UI")

        api_add_bridge.request.connect(
            _show_file_info_for_api_add,
            Qt.ConnectionType.QueuedConnection,
        )

        async def _request_add_with_file_info(request_data: dict[str, Any]) -> dict[str, str]:
            completion: concurrent.futures.Future = concurrent.futures.Future()
            api_add_bridge.request.emit(request_data, completion)

            # Do not block the API response while waiting for user interaction.
            # Returning immediately prevents the extension's 7-second timeout,
            # while the UI asynchronously handles the user's choice.
            import time
            return {"download_id": f"ui_pending_{int(time.time()*1000)}"}

        async def _api_add_interceptor(req: Any) -> dict[str, str]:
            return await _request_add_with_file_info(
                {
                    "url": req.url,
                    "filename": req.filename,
                    "save_path": req.save_path,
                    "priority": req.priority,
                    "category": req.category,
                    "referer": req.referer,
                    "cookies": req.cookies,
                    "hash_expected": req.hash_expected,
                    "source": "api",
                }
            )

        api_dialog_handler["handler"] = _api_add_interceptor
        api_dialog_ready.set()

        def _attach_api_add_interceptor() -> None:
            if args.no_server or not config.get("server", {}).get("enabled", True):
                return

            try:
                import server.api as api_module

                app_obj = getattr(api_module, "app", None)
                if app_obj is not None:
                    app_obj.state.add_download_interceptor = _api_add_interceptor
            except Exception:
                log.exception("Failed to attach API add-download interceptor")

        _attach_api_add_interceptor()

        if clipboard_monitor is not None:
            engine_thread.run_coroutine(clipboard_monitor.start())
            log.info("✓ Clipboard monitor started")

        # Wire UI signals → engine coroutines (cross-thread via run_coroutine)
        def _on_add(data: dict) -> None:
            engine_thread.run_coroutine(
                download_engine.add_download(
                    url=data.get("url", ""),
                    filename=data.get("filename", ""),
                    category=data.get("category", "Other"),
                    save_path=data.get("save_path", ""),
                    priority=data.get("priority", "normal"),
                    referer=data.get("referer"),
                    cookies=data.get("cookies"),
                    hash_expected=data.get("hash_expected"),
                    start_immediately=bool(data.get("start_immediately", True)),
                    # Manual UI actions are considered explicit confirmation.
                    allow_out_of_root=True,
                )
            )

        def _on_pause(dl_id: str) -> None:
            engine_thread.run_coroutine(download_engine.pause(dl_id))

        def _on_resume(dl_id: str) -> None:
            engine_thread.run_coroutine(download_engine.resume(dl_id))

        def _on_cancel(dl_id: str) -> None:
            engine_thread.run_coroutine(download_engine.cancel(dl_id))

        def _on_delete(dl_id: str, delete_file: bool) -> None:
            future = engine_thread.run_coroutine(
                download_engine.remove(dl_id, delete_file=delete_file)
            )

            def _on_done(fut: Any) -> None:
                ok = True
                try:
                    fut.result()
                except Exception as exc:
                    ok = False
                    log.warning("Delete failed for %s: %s", dl_id, exc)
                emitter.download_deleted.emit(dl_id, ok)

            future.add_done_callback(_on_done)

        window.add_download_requested.connect(_on_add)
        window.pause_requested.connect(_on_pause)
        window.resume_requested.connect(_on_resume)
        window.cancel_requested.connect(_on_cancel)
        window.delete_requested.connect(_on_delete)

        # ── Callback bridge: engine → UI ───────────────────────────────────
        # Create a thread-safe handler using Qt signals to bridge engine callbacks.
        from core.downloader import DownloadCallbacks
        
        class SignalEmitter(QObject):
            """Qt signal emitter for cross-thread engine→UI communication."""
            progress_updated = pyqtSignal(str, int, int, float, float)
            chunk_progress = pyqtSignal(str, int, int)
            status_changed = pyqtSignal(str, str, str)
            download_added = pyqtSignal(str, dict)
            download_deleted = pyqtSignal(str, bool)
            downloads_reconciled = pyqtSignal(list)
        
        class UICallbackBridge(DownloadCallbacks):
            """Bridge engine callbacks to Qt signals (thread-safe)."""
            def __init__(self, emitter: SignalEmitter) -> None:
                self.emitter = emitter
            
            def on_progress(
                self, download_id: str, downloaded: int, total: int,
                speed: float, eta_seconds: float,
            ) -> None:
                """Called when download progress updates."""
                try:
                    self.emitter.progress_updated.emit(download_id, downloaded, total, speed, eta_seconds)
                except Exception as e:
                    log.warning("Progress callback error: %s", e)

            def on_chunk_progress(self, download_id: str, completed: int, total: int) -> None:
                """Called when chunk completion/activity updates."""
                try:
                    self.emitter.chunk_progress.emit(download_id, completed, total)
                except Exception as e:
                    log.warning("Chunk callback error: %s", e)
            
            def on_status_changed(
                self, download_id: str, status: str, error: Optional[str] = None,
            ) -> None:
                """Called when download status changes."""
                try:
                    self.emitter.status_changed.emit(download_id, status, error or "")
                except Exception as e:
                    log.warning("Status callback error: %s", e)
            
            def on_download_added(
                self, download_id: str, record: Any,
            ) -> None:
                """Called when a new download is added."""
                try:
                    data = {
                        "id": record.id,
                        "url": record.url,
                        "filename": record.filename,
                        "file_size": record.file_size,
                        "downloaded_bytes": 0,
                        "progress_percent": 0.0,
                        "status": record.status,
                        "priority": record.priority,
                        "category": record.category,
                        "date_added": record.date_added,
                        "save_path": record.save_path,
                        "error_message": record.error_message,
                        "chunks_count": record.chunks_count,
                    }
                    self.emitter.download_added.emit(download_id, data)
                except Exception as e:
                    log.warning("Download added callback error: %s", e)
            
            def on_download_complete(self, download_id: str) -> None:
                """Called when a download completes."""
                pass
        
        # Create emitter and bridge, then wire signals to window slots
        emitter = SignalEmitter()
        emitter.progress_updated.connect(window.on_engine_progress)
        emitter.chunk_progress.connect(window.on_engine_chunks)
        emitter.status_changed.connect(window.on_engine_status)
        emitter.download_added.connect(window.on_engine_download_added)
        emitter.download_deleted.connect(window.on_engine_download_deleted)
        emitter.downloads_reconciled.connect(window.apply_reconciled_downloads)

        if tray is not None:
            def _notify_added(_dl_id: str, data: dict[str, Any]) -> None:
                filename = str(data.get("filename") or "download")
                tray.notify_added(filename)

            def _notify_status(dl_id: str, status: str, error: str) -> None:
                row = window.model.get_download(dl_id)
                filename = str((row or {}).get("filename") or "download")
                if status == "completed":
                    file_size = int((row or {}).get("file_size") or 0)
                    size_text = ""
                    if file_size > 0:
                        from core.network import format_size
                        size_text = format_size(file_size)
                    tray.notify_complete(filename, size_text)
                elif status == "failed":
                    tray.notify_error(filename, error or "Unknown error")

            def _sync_tray_status() -> None:
                active = 0
                total_speed = 0.0
                for dl in window.model._downloads:
                    dl_id = str(dl.get("id", ""))
                    if dl.get("status") in ("downloading", "merging", "verifying"):
                        active += 1
                        total_speed += float(window.model._speeds.get(dl_id, 0.0))
                tray.update_status(active, total_speed)

            emitter.download_added.connect(_notify_added)
            emitter.status_changed.connect(_notify_status)
            emitter.progress_updated.connect(
                lambda _a, _b, _c, _d, _e: _sync_tray_status()
            )
            emitter.status_changed.connect(
                lambda _a, _b, _c: _sync_tray_status()
            )
        
        ui_callbacks = UICallbackBridge(emitter)

        callbacks: list[DownloadCallbacks] = [ui_callbacks]
        if ws_manager is not None:
            from server.websocket import WebSocketCallbacks

            callbacks.append(WebSocketCallbacks(ws_manager))

        class FanoutCallbacks(DownloadCallbacks):
            """Dispatch engine callback events to all registered callback targets."""

            def __init__(self, targets: list[DownloadCallbacks]) -> None:
                self._targets = targets

            def set_targets(self, targets: list[DownloadCallbacks]) -> None:
                self._targets = targets

            def on_progress(
                self,
                download_id: str,
                downloaded: int,
                total: int,
                speed: float,
                eta_seconds: float,
            ) -> None:
                for target in self._targets:
                    target.on_progress(download_id, downloaded, total, speed, eta_seconds)

            def on_status_changed(
                self,
                download_id: str,
                status: str,
                error: Optional[str] = None,
            ) -> None:
                for target in self._targets:
                    target.on_status_changed(download_id, status, error)

            def on_download_added(self, download_id: str, record: Any) -> None:
                for target in self._targets:
                    target.on_download_added(download_id, record)

            def on_chunk_progress(self, download_id: str, completed: int, total: int) -> None:
                for target in self._targets:
                    target.on_chunk_progress(download_id, completed, total)

            def on_download_complete(self, download_id: str) -> None:
                for target in self._targets:
                    target.on_download_complete(download_id)

        fanout_callbacks = FanoutCallbacks(callbacks)
        download_engine.set_callbacks(fanout_callbacks)
        
        # Store emitter as window attribute to prevent garbage collection
        setattr(window, "_ui_callback_emitter", emitter)
        log.info("✓ UI/WebSocket callback bridge attached to engine")

        def _refresh_engine_callbacks() -> None:
            targets: list[DownloadCallbacks] = [ui_callbacks]
            if ws_manager is not None:
                from server.websocket import WebSocketCallbacks
                targets.append(WebSocketCallbacks(ws_manager))
            fanout_callbacks.set_targets(targets)

        # Load existing downloads into the table
        async def _load_existing() -> list:
            await storage.normalize_legacy_filenames()
            now_utc = datetime.now(timezone.utc)

            def _looks_like_generated_id(value: str) -> bool:
                if not value:
                    return False
                compact = value.replace("-", "")
                if len(compact) in (32, 36) and all(
                    c in "0123456789abcdefABCDEF-" for c in value
                ):
                    return True
                return False

            def _is_stale_placeholder(record: Any) -> bool:
                if getattr(record, "status", "") != "queued":
                    return False
                if int(getattr(record, "file_size", -1)) > 0:
                    return False
                if int(getattr(record, "downloaded_bytes", 0)) > 0:
                    return False
                if not _looks_like_generated_id(str(getattr(record, "filename", "")).strip()):
                    return False

                # Only prune obviously-broken placeholders: no valid URL and no save path.
                url_value = str(getattr(record, "url", "")).strip().lower()
                save_path_value = str(getattr(record, "save_path", "")).strip()
                valid_url = url_value.startswith(("http://", "https://", "ftp://", "magnet:"))
                if valid_url or save_path_value:
                    return False

                added = str(getattr(record, "date_added", "")).strip()
                if not added:
                    return True
                try:
                    added_dt = datetime.fromisoformat(added.replace("Z", "+00:00"))
                    if added_dt.tzinfo is None:
                        added_dt = added_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    return True

                age_seconds = (now_utc - added_dt).total_seconds()
                return age_seconds > 1800  # 30 minutes

            records = await storage.get_all_downloads(limit=500)

            # Remove stale placeholder rows created by failed/legacy queue imports.
            stale_ids = [r.id for r in records if _is_stale_placeholder(r)]
            for stale_id in stale_ids:
                await storage.delete_download(stale_id)

            if stale_ids:
                records = await storage.get_all_downloads(limit=500)

            visible_records = records
            return [
                {
                    "id": r.id, "url": r.url, "filename": r.filename,
                    "file_size": r.file_size,
                    "downloaded_bytes": r.downloaded_bytes,
                    "progress_percent": r.progress_percent,
                    "status": r.status, "priority": r.priority,
                    "category": r.category, "date_added": r.date_added,
                    "chunks_count": r.chunks_count,
                    "save_path": r.save_path,
                    "error_message": r.error_message,
                }
                for r in visible_records
            ]

        existing = engine_thread.run_coroutine(_load_existing()).result(timeout=10)
        window.model.set_downloads(existing)
        log.info("✓ Loaded %d existing downloads", len(existing))

        # Fallback reconciliation: if any bridge/UI callback is missed,
        # keep the UI table in sync with persisted downloads.
        reconcile_in_flight = False

        def _run_reconcile() -> None:
            nonlocal reconcile_in_flight
            if reconcile_in_flight:
                return

            reconcile_in_flight = True
            future = engine_thread.run_coroutine(_load_existing())

            def _on_done(fut: Any) -> None:
                nonlocal reconcile_in_flight
                try:
                    snapshot = fut.result()
                    emitter.downloads_reconciled.emit(snapshot)
                except Exception:
                    log.warning("UI reconcile refresh failed", exc_info=True)
                finally:
                    reconcile_in_flight = False

            future.add_done_callback(_on_done)

        reconcile_timer = QTimer(window)
        reconcile_timer.setInterval(3000)
        reconcile_timer.timeout.connect(_run_reconcile)
        reconcile_timer.start()

        config_saver = BatchedConfigSaver(args.config)

        def _on_settings_changed(updated_config: dict[str, Any]) -> None:
            nonlocal server_thread, ws_manager, scheduler_running, bridge_pairing_code

            previous_config = deepcopy(config)
            config.clear()
            config.update(updated_config)

            if config.get("network", {}).get("auto_apply_stable_limits_on_startup", False):
                changed, global_kbps, per_download_kbps, _ = apply_stable_limits_to_config(config)
                if changed:
                    log.info(
                        "Auto-applied stable limits from settings: global=%d KB/s, per-download=%d KB/s",
                        global_kbps,
                        per_download_kbps,
                    )

            # Apply theme instantly.
            theme = config.get("general", {}).get("theme", "dark")
            app.setStyleSheet(get_theme_stylesheet(theme))

            # Persist immediately so settings survive crashes/forced closes.
            # Debounce rapid setting changes to avoid repeated disk writes.
            config_saver.schedule(config)

            # Apply engine settings in-place.
            try:
                download_engine.apply_runtime_config(config)
            except Exception:
                log.exception("Failed to apply engine runtime settings")

            # Apply network settings. Recreate session only if no active downloads.
            prev_net = previous_config.get("network", {})
            new_net = config.get("network", {})
            disruptive_network_change = any([
                prev_net.get("proxy") != new_net.get("proxy"),
                prev_net.get("verify_ssl") != new_net.get("verify_ssl"),
                prev_net.get("connection_timeout_seconds") != new_net.get("connection_timeout_seconds"),
                prev_net.get("read_timeout_seconds") != new_net.get("read_timeout_seconds"),
                prev_net.get("ipv6_enabled") != new_net.get("ipv6_enabled"),
            ])
            reinitialize_session = disruptive_network_change and download_engine.active_count == 0

            try:
                engine_thread.run_coroutine(
                    network.apply_runtime_config(
                        config,
                        reinitialize_session=reinitialize_session,
                    )
                ).result(timeout=10)
            except Exception:
                log.exception("Failed to apply network runtime settings")

            # Reload scheduler and start/stop monitor loop as needed.
            try:
                scheduler.reload_config(config)
                if scheduler.enabled and not scheduler_running:
                    engine_thread.run_coroutine(scheduler.start(download_engine)).result(timeout=5)
                    scheduler_running = True
                    log.info("Scheduler enabled from settings")
                elif not scheduler.enabled and scheduler_running:
                    engine_thread.run_coroutine(scheduler.stop()).result(timeout=5)
                    scheduler_running = False
                    log.info("Scheduler disabled from settings")
            except Exception:
                log.exception("Failed to apply scheduler runtime settings")

            # Restart bridge server if key server settings changed.
            prev_server = previous_config.get("server", {})
            new_server = config.get("server", {})
            server_changed = any([
                prev_server.get("enabled") != new_server.get("enabled"),
                prev_server.get("host") != new_server.get("host"),
                prev_server.get("port") != new_server.get("port"),
                prev_server.get("auth_token") != new_server.get("auth_token"),
            ])

            if server_changed and not args.no_server:
                try:
                    if server_thread is not None:
                        server_thread.stop()
                        server_thread = None

                    if new_server.get("enabled", True):
                        ws_manager = bootstrap_setup_server_app(download_engine, storage, config)
                        _attach_api_add_interceptor()
                        try:
                            import server.api as api_module
                            app_obj = getattr(api_module, "app", None)
                            bridge_pairing_code = str(getattr(app_obj.state, "pairing_code", "") or "") if app_obj else ""
                        except Exception:
                            bridge_pairing_code = ""
                        server_thread = BootstrapServerThread(
                            host=new_server.get("host", "127.0.0.1"),
                            port=int(new_server.get("port", 6800)),
                        )
                        server_thread.start()
                        log.info(
                            "Bridge server restarted on %s:%s",
                            new_server.get("host", "127.0.0.1"),
                            new_server.get("port", 6800),
                        )

                    _refresh_engine_callbacks()
                except Exception:
                    log.exception("Failed to restart bridge server from settings")

            window.set_bridge_status(
                enabled=bool(config.get("server", {}).get("enabled", True) and not args.no_server),
                host=str(config.get("server", {}).get("host", "127.0.0.1")),
                port=int(config.get("server", {}).get("port", 6800)),
                pairing_code=bridge_pairing_code,
            )

            if tray is not None:
                tray._config = config
                tray.set_pairing_code(bridge_pairing_code)

            _set_startup_registration(
                bool(config.get("general", {}).get("start_with_system", False)),
                log,
            )

            log.info("Runtime settings applied")

        window.settings_changed.connect(_on_settings_changed)

        # ── 14. Show window ────────────────────────────────────────────────
        start_minimized = args.minimized or config["general"].get("start_minimized", False)
        if start_minimized:
            log.info("Starting minimized to tray")
            if tray is None:
                window.showMinimized()
        else:
            window.show()

        log.info("✓ Application window shown — entering event loop")

        get_shutdown_reason = bootstrap_install_shutdown_diagnostics(app, window, log)

        # ── 15. Enter Qt event loop ────────────────────────────────────────
        exit_code = app.exec()
        log.warning(
            "Qt event loop exited (exit_code=%s, shutdown_reason=%s)",
            exit_code,
            get_shutdown_reason(),
        )

        # ── 16. Clean shutdown ─────────────────────────────────────────────
        log.info("Shutting down…")

        if clipboard_monitor:
            engine_thread.run_coroutine(clipboard_monitor.stop())
            log.info("✓ Clipboard monitor stopped")

        if scheduler.enabled:
            engine_thread.run_coroutine(scheduler.stop())
            log.info("✓ Scheduler stopped")

        engine_thread.run_coroutine(download_engine.stop())
        log.info("✓ Download engine stopped")

        if server_thread:
            server_thread.stop()
            log.info("✓ Bridge server stopped")

        if tray is not None:
            tray.hide()

        engine_thread.run_coroutine(_shutdown_subsystems(storage, network))
        log.info("✓ Storage and network closed")

        engine_thread.stop()
        log.info("✓ Engine thread stopped")

        try:
            config_saver.flush()
        except Exception:
            log.exception("Failed to flush pending config snapshot")

        save_config(args.config, config)
        log.info("✓ Configuration saved")

        log.info("Goodbye!")
        return exit_code

    except Exception as exc:
        log.exception("Fatal error during startup")
        show_startup_error_dialog(exc)
        log.debug("Startup traceback:\n%s", traceback.format_exc())
        return 1

    finally:
        lock.release()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ENTRY POINT                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

if __name__ == "__main__":
    sys.exit(main())
