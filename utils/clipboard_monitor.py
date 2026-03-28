"""
IDM Utilities — Clipboard Monitor
===================================
Monitors the system clipboard for URLs and automatically triggers
download prompts.

Features:
    • Detects HTTP/HTTPS, FTP, and magnet URLs
    • Configurable URL pattern filtering
    • Duplicate detection (won't re-trigger for the same URL)
    • File extension filtering (only trigger for downloadable files)
    • Async interface compatible with the engine loop

Usage::

    monitor = ClipboardMonitor(config, on_url_detected=callback)
    await monitor.start()
    await monitor.stop()
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Optional, Set

log = logging.getLogger("idm.utils.clipboard")

# ── URL Detection Patterns ─────────────────────────────────────────────────────

# Matches HTTP/HTTPS/FTP URLs
_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"'`\]\)]+|"
    r"ftp://[^\s<>\"'`\]\)]+|"
    r"magnet:\?[^\s<>\"'`\]\)]+",
    re.IGNORECASE,
)

# Common downloadable file extensions
_DOWNLOADABLE_EXTENSIONS = frozenset({
    # Video
    ".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".3gp", ".ts",
    # Audio
    ".mp3", ".flac", ".wav", ".aac", ".ogg", ".wma", ".m4a", ".opus",
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp",
    # Documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".epub",
    # Software
    ".exe", ".msi", ".dmg", ".deb", ".rpm", ".apk", ".appimage",
    # Archives
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
    # Other
    ".bin", ".img", ".torrent",
})

# URL parts to exclude (login pages, API endpoints, etc.)
_EXCLUDE_PATTERNS = [
    re.compile(r"(login|signin|auth|oauth|api/v\d)", re.IGNORECASE),
]


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CLIPBOARD MONITOR                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class ClipboardMonitor:
    """
    Monitors the system clipboard for downloadable URLs.

    Uses polling (checks clipboard every ``poll_interval`` seconds)
    since there's no reliable cross-platform clipboard change event.

    Args:
        config: Application configuration dictionary.
        on_url_detected: Async callback when a downloadable URL is found.
        poll_interval: Seconds between clipboard checks.
    """

    def __init__(
        self,
        config: dict[str, Any],
        on_url_detected: Optional[Callable[[str], Any]] = None,
        poll_interval: float = 1.0,
    ) -> None:
        self._config = config
        self._on_url_detected = on_url_detected
        self._poll_interval = poll_interval
        self._enabled: bool = config.get("clipboard", {}).get("monitor_enabled", True)
        self._auto_download: bool = config.get("clipboard", {}).get("auto_download", False)

        # Extension filter from config
        custom_exts = config.get("clipboard", {}).get("monitored_extensions", [])
        if custom_exts:
            self._extensions = frozenset(
                ext if ext.startswith(".") else f".{ext}"
                for ext in custom_exts
            )
        else:
            self._extensions = _DOWNLOADABLE_EXTENSIONS

        # State
        self._seen_urls: Set[str] = set()
        self._last_text: str = ""
        self._task: Optional[asyncio.Task[None]] = None
        self._max_seen = 1000  # max URLs to remember

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        log.info("Clipboard monitor %s", "enabled" if value else "disabled")

    @property
    def seen_count(self) -> int:
        """Number of unique URLs detected so far."""
        return len(self._seen_urls)

    def clear_history(self) -> None:
        """Clear the set of seen URLs."""
        self._seen_urls.clear()
        self._last_text = ""

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the clipboard polling loop without replaying current clipboard contents."""
        if self._task and not self._task.done():
            return

        # Prime the monitor with the current clipboard contents so startup does
        # not immediately replay a previously copied URL as a new download.
        self._last_text = await self._read_clipboard()

        self._task = asyncio.create_task(
            self._poll_loop(), name="clipboard-monitor"
        )
        log.info("Clipboard monitor started (interval=%.1fs)", self._poll_interval)

    async def stop(self) -> None:
        """Stop the clipboard polling loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("Clipboard monitor stopped")

    # ── Polling Loop ───────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main polling loop — reads clipboard and checks for URLs."""
        while True:
            try:
                if self._enabled:
                    text = await self._read_clipboard()
                    if text and text != self._last_text:
                        self._last_text = text
                        await self._check_text(text)
            except asyncio.CancelledError:
                return
            except Exception:
                log.debug("Clipboard read error", exc_info=True)

            await asyncio.sleep(self._poll_interval)

    async def _read_clipboard(self) -> str:
        """
        Read the current clipboard text content.

        Uses platform-specific methods via asyncio.to_thread to avoid
        blocking the event loop.
        """
        try:
            return await asyncio.to_thread(self._read_clipboard_sync)
        except Exception:
            return ""

    @staticmethod
    def _read_clipboard_sync() -> str:
        """Synchronous clipboard read using platform-native methods."""
        try:
            # Try tkinter first (cross-platform, usually available)
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            try:
                text = root.clipboard_get()
            except tk.TclError:
                text = ""
            finally:
                root.destroy()
            return text
        except ImportError:
            pass

        try:
            # Windows fallback
            import ctypes
            CF_UNICODETEXT = 13
            user32 = ctypes.windll.user32
            if not user32.OpenClipboard(0):
                return ""
            try:
                data = user32.GetClipboardData(CF_UNICODETEXT)
                if data:
                    text = ctypes.wstring_at(data)
                    return text if text else ""
            finally:
                user32.CloseClipboard()
        except (AttributeError, OSError):
            pass

        return ""

    # ── URL Detection ──────────────────────────────────────────────────────

    async def _check_text(self, text: str) -> None:
        """Extract and process URLs from clipboard text."""
        urls = extract_urls(text)

        for url in urls:
            if url in self._seen_urls:
                continue

            if not is_downloadable_url(url, self._extensions):
                continue

            if _is_excluded(url):
                continue

            # Add to seen set (with size cap)
            if len(self._seen_urls) >= self._max_seen:
                # Remove oldest entries (approximate — sets aren't ordered)
                self._seen_urls.clear()
            self._seen_urls.add(url)

            log.info("Downloadable URL detected: %s", url[:100])

            if self._on_url_detected:
                try:
                    result = self._on_url_detected(url)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("URL callback error")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  UTILITY FUNCTIONS                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def extract_urls(text: str) -> list[str]:
    """
    Extract all URLs from a text string.

    Args:
        text: The text to search for URLs.

    Returns:
        List of unique URLs found.
    """
    if not text:
        return []

    matches = _URL_PATTERN.findall(text)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for url in matches:
        # Clean trailing punctuation
        url = url.rstrip(".,;:!?)>]}")
        if url not in seen:
            seen.add(url)
            unique.append(url)

    return unique


def is_downloadable_url(
    url: str,
    extensions: frozenset[str] = _DOWNLOADABLE_EXTENSIONS,
) -> bool:
    """
    Check if a URL points to a downloadable file.

    Checks the URL path for known file extensions. Also matches
    magnet links and common download URL patterns.

    Args:
        url: The URL to check.
        extensions: Set of downloadable extensions.

    Returns:
        True if the URL is likely a downloadable file.
    """
    lower = url.lower()

    # Magnet links are always downloadable
    if lower.startswith("magnet:"):
        return True

    # FTP links are always downloadable
    if lower.startswith("ftp://"):
        return True

    # Check for file extension in URL path
    # Strip query string and fragment
    path = lower.split("?")[0].split("#")[0]

    for ext in extensions:
        if path.endswith(ext):
            return True

    # Check for common download URL patterns
    download_patterns = [
        "/download/", "/dl/", "/get/", "/file/",
        "download=", "file=", "attachment",
    ]
    return any(p in lower for p in download_patterns)


def _is_excluded(url: str) -> bool:
    """Check if a URL matches any exclusion pattern."""
    return any(pat.search(url) for pat in _EXCLUDE_PATTERNS)
