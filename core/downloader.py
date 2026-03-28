# IDM v2.0 — downloader.py — audited 2026-03-28
"""
IDM Core — Chunk Download Engine
==================================
Multi-threaded parallel chunk downloading with resume support.

This module provides:

    • **SpeedTracker** — Rolling-average speed calculation.
    • **DownloadTask** — Manages lifecycle of a single file download:
      preflight, chunking, parallel chunk download, progress tracking.
    • **DownloadEngine** — Top-level orchestrator: queue processing,
      concurrency control, pause/resume/cancel operations.

All async operations run on the EngineThread's asyncio event loop.

Usage::

    engine = DownloadEngine(storage, network, config)
    await engine.start()

    download_id = await engine.add_download("https://example.com/file.zip")
    await engine.pause(download_id)
    await engine.resume(download_id)
    await engine.cancel(download_id)

    await engine.stop()
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import time
from collections import deque
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Optional, Protocol
from urllib.parse import urlparse, unquote

import aiohttp
import aiofiles

from core.storage import (
    StorageManager,
    DownloadRecord,
    DownloadStatus,
    DownloadPriority,
    ChunkRecord,
    ChunkStatus,
)
from core.network import (
    NetworkManager,
    classify_error,
    extract_filename_from_url,
    format_speed,
    format_size,
    calculate_eta,
)
from core.assembler import assemble_and_verify

log = logging.getLogger("idm.core.downloader")

# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_CHUNKS: int = 5
MIN_PARALLEL_CHUNKS: int = 3
MAX_PARALLEL_CHUNKS: int = 5
MIN_CHUNK_SIZE: int = 262_144          # 256 KB
MAX_CHUNK_SIZE: int = 52_428_800       # 50 MB
PARALLEL_CHUNK_MIN_FILE_SIZE: int = 104_857_600  # 100 MB
BUFFER_SIZE: int = 65_536              # 64 KB read buffer
PROGRESS_FLUSH_INTERVAL: float = 1.5   # seconds between DB flushes
SPEED_SAMPLE_WINDOW: int = 20          # rolling window size
SPEED_STALE_AFTER_SECONDS: float = 2.0 # no new bytes -> speed considered zero
QUEUE_POLL_IDLE_SECONDS: float = 1.0   # queue check interval when idle
QUEUE_POLL_BUSY_SECONDS: float = 0.2   # queue check interval when work was scheduled
WINDOWS_SAFE_MAX_PATH: int = 240       # conservative path budget for Windows APIs
_HLS_QUERY_HINT_RE = re.compile(
    r"[?&](format|type|ext|output|container)=m3u8(?:&|$)",
    re.IGNORECASE,
)

_RESOLVE_NEGATIVE_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "application/json",
    "text/plain",
    "text/javascript",
    "application/javascript",
    "text/css",
    "text/xml",
    "application/xml",
}

_RESOLVE_BINARY_EXTENSIONS = {
    "zip", "rar", "7z", "tar", "gz", "bz2", "xz",
    "exe", "msi", "dmg", "pkg", "deb", "rpm", "apk", "iso",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "mp4", "mkv", "avi", "mov", "mp3", "flac", "wav", "webm",
    "jpg", "jpeg", "png", "gif", "tiff", "bmp", "webp",
}


@dataclass(frozen=True)
class ResolvedUrlMetadata:
    """Resolved URL metadata used for smart preflight and extension verification."""

    requested_url: str
    final_url: str
    filename: str = ""
    content_type: str = ""
    content_disposition: str = ""
    content_length: int = -1
    resume_supported: bool = False
    redirected: bool = False
    is_html_page: bool = False
    is_binary: bool = False
    verified: bool = False
    verification_method: str = ""
    warning: str = ""
    error: str = ""


def _resolve_filename(content_disposition: str, final_url: str, filename_hint: str | None = None) -> str:
    """Resolve filename from response headers or final URL fallback."""
    parsed = ""
    if content_disposition:
        # RFC 5987 filename*= and fallback filename=
        m_star = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
        if m_star:
            try:
                parsed = unquote(m_star.group(1))
            except Exception:
                parsed = m_star.group(1)
        if not parsed:
            m_plain = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
            if m_plain:
                parsed = m_plain.group(1).strip()
    if parsed:
        return parsed
    
    # Use filename_hint as priority 2 fallback (from page context)
    if filename_hint:
        hint_str = str(filename_hint or "").strip()
        if hint_str and re.search(r'\.(mkv|mp4|avi|zip|rar|exe|pdf|mp3|mov|flv|wmv|webm|m4a)', hint_str, re.I):
            return hint_str
    
    # Priority 3: URL path filename
    url_filename = extract_filename_from_url(final_url)
    return url_filename


def _looks_binary_content_type(content_type: str) -> bool:
    """Return True for likely binary/media payload content-types."""
    ctype = str(content_type or "").lower().split(";", 1)[0].strip()
    if not ctype:
        return False
    if ctype in _RESOLVE_NEGATIVE_CONTENT_TYPES:
        return False
    return ctype.startswith("application/") or ctype.startswith("video/") or ctype.startswith("audio/") or ctype.startswith("image/")


def _looks_binary_extension(url: str, filename: str) -> bool:
    """Return True if filename or URL path has a known downloadable extension."""
    candidates = [str(filename or ""), extract_filename_from_url(url)]
    for candidate in candidates:
        if not candidate or "." not in candidate:
            continue
        ext = candidate.rsplit(".", 1)[-1].strip().lower()
        if ext in _RESOLVE_BINARY_EXTENSIONS:
            return True
    return False


def _parse_resolve_response(
    requested_url: str,
    final_url: str,
    headers: dict[str, str],
    status: int,
    verification_method: str = "head",
    warning: str = "",
) -> ResolvedUrlMetadata:
    """Parse HTTP response metadata into ResolvedUrlMetadata."""
    content_type = str(headers.get("Content-Type", "") or "")
    disposition = str(headers.get("Content-Disposition", "") or "")
    content_length_raw = str(headers.get("Content-Length", "") or "").strip()
    content_length = -1
    if content_length_raw.isdigit():
        content_length = int(content_length_raw)
    else:
        content_range = str(headers.get("Content-Range", "") or "")
        m = re.search(r"/(\d+)$", content_range)
        if m:
            content_length = int(m.group(1))

    accept_ranges = str(headers.get("Accept-Ranges", "") or "").lower()
    resume_supported = "bytes" in accept_ranges or status == 206
    filename = _resolve_filename(disposition, final_url)
    is_html = str(content_type).lower().split(";", 1)[0].strip() in {"text/html", "application/xhtml+xml"}
    has_attachment = "attachment" in disposition.lower()
    is_binary = has_attachment or _looks_binary_content_type(content_type) or _looks_binary_extension(final_url, filename)

    return ResolvedUrlMetadata(
        requested_url=requested_url,
        final_url=final_url,
        filename=filename,
        content_type=content_type,
        content_disposition=disposition,
        content_length=content_length,
        resume_supported=resume_supported,
        redirected=(requested_url != final_url),
        is_html_page=is_html,
        is_binary=is_binary,
        verified=True,
        verification_method=verification_method,
        warning=warning,
    )


async def resolve_url_metadata(
    url: str,
    *,
    referer: str | None = None,
    cookies: str | None = None,
    headers: Optional[dict[str, str]] = None,
    timeout_seconds: float = 3.0,
    max_redirects: int = 5,
) -> ResolvedUrlMetadata:
    """
    Resolve final URL and response metadata with redirect-following HEAD/GET probes.

    This helper is used by API endpoints and extension bridge checks to decide
    whether a link resolves to a downloadable binary file or an HTML page.
    """
    requested = str(url or "").strip()
    if not requested:
        return ResolvedUrlMetadata(
            requested_url="",
            final_url="",
            verified=False,
            error="URL is required",
        )

    parsed = urlparse(requested)
    if parsed.scheme not in {"http", "https"}:
        return ResolvedUrlMetadata(
            requested_url=requested,
            final_url=requested,
            filename=extract_filename_from_url(requested),
            is_binary=_looks_binary_extension(requested, ""),
            verified=False,
            warning="Only HTTP/HTTPS links support metadata resolve",
        )

    request_headers: dict[str, str] = {}
    if referer:
        request_headers["Referer"] = str(referer)
    if cookies:
        request_headers["Cookie"] = str(cookies)
    if headers:
        for key, value in headers.items():
            k = str(key or "").strip()
            v = str(value or "").strip()
            if k and v:
                request_headers[k] = v

    total_timeout = max(1.0, float(timeout_seconds or 3.0))
    timeout = aiohttp.ClientTimeout(
        total=total_timeout,
        connect=min(1.5, total_timeout),
        sock_connect=min(1.5, total_timeout),
        sock_read=min(2.5, total_timeout),
    )

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.head(
                    requested,
                    headers=request_headers,
                    allow_redirects=True,
                    max_redirects=max(1, int(max_redirects)),
                ) as resp:
                    head_headers = dict(resp.headers)
                    final_url = str(resp.url)
                    if resp.status < 400:
                        return _parse_resolve_response(
                            requested,
                            final_url,
                            head_headers,
                            int(resp.status),
                            verification_method="head",
                        )
            except asyncio.TimeoutError:
                return ResolvedUrlMetadata(
                    requested_url=requested,
                    final_url=requested,
                    filename=extract_filename_from_url(requested),
                    verified=False,
                    warning="HEAD verification timed out",
                )
            except Exception:
                # Fall back to lightweight GET probe when HEAD is blocked.
                pass

            probe_headers = dict(request_headers)
            probe_headers["Range"] = "bytes=0-0"
            probe_headers.setdefault("Accept-Encoding", "identity")

            try:
                async with session.get(
                    requested,
                    headers=probe_headers,
                    allow_redirects=True,
                    max_redirects=max(1, int(max_redirects)),
                ) as probe:
                    probe_headers_resp = dict(probe.headers)
                    final_url = str(probe.url)
                    if probe.status < 400:
                        warning = "Verified via GET probe (HEAD blocked)"
                        return _parse_resolve_response(
                            requested,
                            final_url,
                            probe_headers_resp,
                            int(probe.status),
                            verification_method="get-probe",
                            warning=warning,
                        )
                    return ResolvedUrlMetadata(
                        requested_url=requested,
                        final_url=final_url,
                        filename=extract_filename_from_url(final_url),
                        verified=False,
                        error=f"HTTP {int(probe.status)}",
                    )
            except asyncio.TimeoutError:
                return ResolvedUrlMetadata(
                    requested_url=requested,
                    final_url=requested,
                    filename=extract_filename_from_url(requested),
                    verified=False,
                    warning="Resolve probe timed out",
                )
            except Exception as exc:
                return ResolvedUrlMetadata(
                    requested_url=requested,
                    final_url=requested,
                    filename=extract_filename_from_url(requested),
                    verified=False,
                    warning="Resolve probe unavailable",
                    error=str(exc),
                )
    except Exception as exc:
        return ResolvedUrlMetadata(
            requested_url=requested,
            final_url=requested,
            filename=extract_filename_from_url(requested),
            verified=False,
            warning="Resolver initialization failed",
            error=str(exc),
        )


def _sanitize_output_filename(filename: str) -> str:
    """Sanitize a filename for local filesystem use."""
    name = str(filename or "").strip()
    if not name:
        return "download"

    # Remove invalid Windows characters and control bytes.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Windows rejects trailing spaces/dots in path components.
    name = name.rstrip(" .")
    return name or "download"


def _truncate_filename_for_parent(parent: Path, filename: str) -> str:
    """
    Truncate filename so full path stays inside conservative Windows limits.

    Keeps extension when possible and appends a short hash suffix for uniqueness.
    """
    safe_name = _sanitize_output_filename(filename)
    if not safe_name:
        safe_name = "download"

    parent_len = len(str(parent))
    budget = WINDOWS_SAFE_MAX_PATH - parent_len - 1
    # Keep a practical lower bound so we don't create empty/invalid names.
    budget = max(40, budget)

    if len(safe_name) <= budget:
        return safe_name

    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    digest = hashlib.sha1(safe_name.encode("utf-8", errors="ignore")).hexdigest()[:8]
    tag = f"_{digest}"
    keep = max(1, budget - len(suffix) - len(tag))
    return f"{stem[:keep]}{tag}{suffix}"


def _normalize_save_path_for_platform(save_path: str | Path) -> str:
    """Normalize a save path and shorten overly long filenames on Windows."""
    path = Path(str(save_path))
    if path.drive:
        filename = _truncate_filename_for_parent(path.parent, path.name)
        return str(path.parent / filename)
    return str(path)


def _allowed_download_roots(config: dict[str, Any]) -> list[Path]:
    """Build allowlisted download roots from current configuration."""
    general = config.get("general", {}) if isinstance(config, dict) else {}
    categories = config.get("categories", {}) if isinstance(config, dict) else {}

    download_root = Path(str(general.get("download_directory", "") or "").strip() or r"D:\idm down")
    roots = {download_root.resolve(strict=False)}
    if isinstance(categories, dict):
        for category in categories.keys():
            roots.add((download_root / str(category)).resolve(strict=False))
    return sorted(roots, key=lambda p: len(str(p)))


def _path_within_roots(candidate: Path, roots: list[Path]) -> bool:
    """Check whether candidate path stays within configured allowlisted roots."""
    resolved = candidate.resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


def _canonicalize_save_path(save_path: str, config: dict[str, Any]) -> tuple[str, bool]:
    """Resolve caller save path and flag whether it is inside allowlisted roots."""
    raw = str(save_path or "").strip()
    roots = _allowed_download_roots(config)
    base = roots[0] if roots else Path(r"D:\idm down").resolve(strict=False)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve(strict=False)
    return str(resolved), _path_within_roots(resolved, roots)


@dataclass(frozen=True)
class TaskRuntimeConfig:
    """Immutable runtime configuration contract applied to active tasks."""
    default_chunks: int
    min_chunk_size: int
    max_chunk_size: int
    buffer_size: int
    prefetch_buffers: int
    dynamic_chunks: bool
    parallel_chunk_min_size: int


def _build_task_runtime_config(config: dict[str, Any]) -> TaskRuntimeConfig:
    """Construct validated task runtime config from global config dict."""
    adv = config.get("advanced", {})
    default_chunks = int(config.get("general", {}).get("default_chunks", DEFAULT_CHUNKS))
    default_chunks = max(MIN_PARALLEL_CHUNKS, min(default_chunks, MAX_PARALLEL_CHUNKS))
    min_chunk = int(adv.get("min_chunk_size_bytes", MIN_CHUNK_SIZE))
    max_chunk = int(adv.get("max_chunk_size_bytes", MAX_CHUNK_SIZE))
    buffer_size = int(adv.get("chunk_buffer_size_bytes", BUFFER_SIZE))
    prefetch_buffers = max(1, int(adv.get("chunk_prefetch_buffers", 2) or 1))
    dynamic_chunks = bool(adv.get("dynamic_chunk_adjustment", True))
    min_mb = int(adv.get("parallel_chunk_min_file_size_mb", 100))
    parallel_chunk_min_size = max(1, min_mb) * 1024 * 1024

    return TaskRuntimeConfig(
        default_chunks=default_chunks,
        min_chunk_size=min_chunk,
        max_chunk_size=max_chunk,
        buffer_size=buffer_size,
        prefetch_buffers=prefetch_buffers,
        dynamic_chunks=dynamic_chunks,
        parallel_chunk_min_size=parallel_chunk_min_size,
    )


def _looks_like_hls_url(url: str) -> bool:
    """Return True when URL likely points to an HLS playlist."""
    value = str(url or "")
    return ".m3u8" in value.lower() or bool(_HLS_QUERY_HINT_RE.search(value))


def _load_request_headers_from_metadata(metadata_json: str | None) -> dict[str, str]:
    """Extract persisted request headers from metadata JSON payload."""
    if not metadata_json:
        return {}

    try:
        payload = json.loads(metadata_json)
    except Exception:
        return {}

    source = payload.get("request_headers") if isinstance(payload, dict) else None
    if not isinstance(source, dict):
        return {}

    blocked = {"host", "content-length"}
    cleaned: dict[str, str] = {}
    for key, value in source.items():
        k = str(key).strip()
        v = str(value).strip()
        if not k or not v:
            continue
        if k.lower() in blocked:
            continue
        cleaned[k] = v
    return cleaned


def _cookie_header_to_dict(cookie_header: str | None) -> dict[str, str]:
    """Convert Cookie header string into a dictionary for aiohttp session cookies."""
    if not cookie_header:
        return {}

    parsed = SimpleCookie()
    try:
        parsed.load(cookie_header)
    except Exception:
        return {}

    return {name: morsel.value for name, morsel in parsed.items()}


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CALLBACKS PROTOCOL                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class DownloadCallbacks(Protocol):
    """Protocol for download progress callbacks (UI updates)."""

    def on_progress(
        self, download_id: str, downloaded: int, total: int, speed: float,
        eta_seconds: float,
    ) -> None: ...

    def on_status_changed(
        self, download_id: str, status: str, error: Optional[str] = None,
    ) -> None: ...

    def on_download_added(self, download_id: str, record: DownloadRecord) -> None: ...
    def on_chunk_progress(self, download_id: str, completed: int, total: int) -> None: ...
    def on_download_complete(self, download_id: str) -> None: ...


class NullCallbacks:
    """No-op callbacks for when no UI is attached."""
    def on_progress(self, *a: Any, **kw: Any) -> None: pass
    def on_status_changed(self, *a: Any, **kw: Any) -> None: pass
    def on_download_added(self, *a: Any, **kw: Any) -> None: pass
    def on_chunk_progress(self, *a: Any, **kw: Any) -> None: pass
    def on_download_complete(self, *a: Any, **kw: Any) -> None: pass


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SPEED TRACKER                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class SpeedTracker:
    """
    Track download speed using a rolling window of byte-count samples.

    Samples are recorded periodically. Speed is calculated as the
    average rate of change over the window.
    """

    def __init__(
        self,
        window_size: int = SPEED_SAMPLE_WINDOW,
        stale_after_seconds: float = SPEED_STALE_AFTER_SECONDS,
    ) -> None:
        self._samples: deque[tuple[float, int]] = deque(maxlen=window_size)
        self._total_bytes: int = 0
        self._start_time: float = time.monotonic()
        self._stale_after_seconds: float = max(0.1, float(stale_after_seconds))

    def record(self, bytes_so_far: int) -> None:
        """Record the current cumulative byte count."""
        self._samples.append((time.monotonic(), bytes_so_far))
        self._total_bytes = bytes_so_far

    @property
    def speed(self) -> float:
        """Current speed in bytes/sec (rolling average)."""
        if len(self._samples) < 2:
            return 0.0
        oldest_time, oldest_bytes = self._samples[0]
        newest_time, newest_bytes = self._samples[-1]
        if time.monotonic() - newest_time > self._stale_after_seconds:
            return 0.0
        elapsed = newest_time - oldest_time
        if elapsed <= 0:
            return 0.0
        return (newest_bytes - oldest_bytes) / elapsed

    @property
    def average_speed(self) -> float:
        """Overall average speed since start."""
        elapsed = time.monotonic() - self._start_time
        if elapsed <= 0:
            return 0.0
        return self._total_bytes / elapsed

    def reset(self) -> None:
        """Reset all samples."""
        self._samples.clear()
        self._total_bytes = 0
        self._start_time = time.monotonic()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CHUNK CALCULATOR                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def calculate_chunks(
    file_size: int,
    num_chunks: int = DEFAULT_CHUNKS,
    min_chunk_size: int = MIN_CHUNK_SIZE,
    max_chunk_size: int = MAX_CHUNK_SIZE,
) -> list[tuple[int, int]]:
    """
    Split a file into byte-range chunks for parallel downloading.

    Rules:
        • Each chunk is at least ``min_chunk_size`` bytes.
        • Each chunk is at most ``max_chunk_size`` bytes.
        • The last chunk absorbs any remainder bytes.
        • If the file is smaller than ``min_chunk_size``, a single chunk
          is returned.

    Args:
        file_size: Total file size in bytes.
        num_chunks: Desired number of chunks.
        min_chunk_size: Minimum bytes per chunk.
        max_chunk_size: Maximum bytes per chunk.

    Returns:
        List of (start_byte, end_byte) tuples (inclusive).
    """
    if file_size <= 0:
        return [(0, 0)]

    # Adjust chunk count based on constraints
    if file_size < min_chunk_size:
        return [(0, file_size - 1)]

    # Don't create chunks smaller than min_chunk_size
    max_possible = max(1, file_size // min_chunk_size)
    num_chunks = min(num_chunks, max_possible)

    # Don't create chunks larger than max_chunk_size
    min_needed = max(1, file_size // max_chunk_size)
    if min_needed > num_chunks:
        num_chunks = min_needed

    num_chunks = max(1, num_chunks)
    chunk_size = file_size // num_chunks

    chunks: list[tuple[int, int]] = []
    for i in range(num_chunks):
        start = i * chunk_size
        if i == num_chunks - 1:
            end = file_size - 1  # last chunk gets remainder
        else:
            end = start + chunk_size - 1
        chunks.append((start, end))

    return chunks


def dynamic_chunk_count(
    file_size: int,
    default_chunks: int = DEFAULT_CHUNKS,
) -> int:
    """
    Calculate an IDM-like chunk count based on file size.

    Heuristic tuned to keep parallel downloads in the 3–5 chunk range:
        • < 10 MB     → 3 chunks
        • 10–100 MB   → user default (clamped 3–5)
        • 100 MB–2 GB → 4 chunks
        • > 2 GB      → 5 chunks

    Args:
        file_size: File size in bytes.
        default_chunks: Fallback value.

    Returns:
        Recommended number of chunks.
    """
    if file_size <= 0:
        return MIN_PARALLEL_CHUNKS
    if file_size < 10_485_760:         # < 10 MB
        return MIN_PARALLEL_CHUNKS
    if file_size < 104_857_600:        # < 100 MB
        return max(MIN_PARALLEL_CHUNKS, min(int(default_chunks), MAX_PARALLEL_CHUNKS))
    if file_size < 2_147_483_648:      # < 2 GB
        return 4
    return MAX_PARALLEL_CHUNKS


def should_use_parallel_chunks(
    file_size: int,
    resume_supported: bool,
    min_parallel_size: int = PARALLEL_CHUNK_MIN_FILE_SIZE,
) -> bool:
    """Return True when a download should use multi-chunk parallel mode."""
    if file_size <= 0:
        return False
    if not resume_supported:
        return False
    return file_size >= max(1, int(min_parallel_size))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CUSTOM EXCEPTIONS                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class RangeNotSupportedError(Exception):
    """
    Raised when a server doesn't support HTTP Range requests.
    
    Signals that the download should fall back from multi-chunk
    to single-chunk mode.
    """
    pass


class FirstByteTimeoutError(TimeoutError):
    """Raised when a response connection opens but no payload bytes arrive in time."""
    pass


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DOWNLOAD TASK                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class DownloadTask:
    """
    Manages the full lifecycle of a single file download.

    Lifecycle:
        QUEUED → DOWNLOADING → (MERGING → VERIFYING →) COMPLETED
                  ↓
                PAUSED / FAILED / CANCELLED

    The task:
        1. Performs a preflight HEAD request.
        2. Calculates and stores chunks.
        3. Downloads all chunks in parallel.
        4. Tracks aggregated progress and speed.
        5. Handles pause / resume / cancel.
    """

    def __init__(
        self,
        download_id: str,
        storage: StorageManager,
        network: NetworkManager,
        config: dict[str, Any],
        callbacks: DownloadCallbacks,
        chunks_dir: Path,
    ) -> None:
        self._download_id = download_id
        self._storage = storage
        self._network = network
        self._config = config
        self._callbacks = callbacks
        self._chunks_dir = chunks_dir

        # State
        self._record: Optional[DownloadRecord] = None
        self._chunk_records: list[ChunkRecord] = []
        self._speed_tracker = SpeedTracker()
        self._paused = asyncio.Event()
        self._paused.set()  # not paused initially
        self._cancelled = False
        self._task: Optional[asyncio.Task[None]] = None
        self._request_headers: dict[str, str] = {}
        self._first_byte_seen = False
        self._first_byte_event = asyncio.Event()

        # Progress
        self._downloaded_bytes: int = 0
        self._last_flush: float = 0

        # Config
        self._runtime_config = _build_task_runtime_config(config)
        self._buffer_size = self._runtime_config.buffer_size
        self._dynamic_chunks = self._runtime_config.dynamic_chunks
        self._default_chunks = self._runtime_config.default_chunks
        self._min_chunk = self._runtime_config.min_chunk_size
        self._max_chunk = self._runtime_config.max_chunk_size
        self._prefetch_buffers = self._runtime_config.prefetch_buffers
        self._first_byte_timeout_seconds = max(
            1.0,
            float(config.get("advanced", {}).get("first_byte_timeout_seconds", 15.0) or 15.0),
        )
        self._parallel_chunk_min_size = self._runtime_config.parallel_chunk_min_size

    def apply_runtime_config(
        self,
        new_config: TaskRuntimeConfig,
        full_config: Optional[dict[str, Any]] = None,
    ) -> None:
        """Apply immutable runtime task config without mutating private attrs ad-hoc."""
        self._runtime_config = new_config
        if full_config is not None:
            self._config = full_config

        self._default_chunks = new_config.default_chunks
        self._min_chunk = new_config.min_chunk_size
        self._max_chunk = new_config.max_chunk_size
        self._buffer_size = new_config.buffer_size
        self._prefetch_buffers = new_config.prefetch_buffers
        self._dynamic_chunks = new_config.dynamic_chunks
        self._parallel_chunk_min_size = new_config.parallel_chunk_min_size

    def _log_phase(self, phase: str) -> None:
        """Emit structured phase transitions with explicit timestamps."""
        log.info(
            "Download %s phase=%s at %s",
            self._download_id[:8],
            phase,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def _get_per_download_rate_bps(self) -> int:
        """Resolve per-download cap from config in bytes per second."""
        net = self._config.get("network", {})
        rate_kbps = int(net.get("per_download_bandwidth_kbps", 0) or 0)
        return max(0, rate_kbps * 1024)

    def _build_chunk_request_headers(
        self,
        start: int,
        end: int,
    ) -> dict[str, str]:
        """Build request headers for a chunk download attempt."""
        assert self._record is not None

        extra_headers: dict[str, str] = dict(self._request_headers)
        if self._record.user_agent:
            extra_headers["User-Agent"] = self._record.user_agent

        if self._record.referer:
            try:
                parsed_ref = urlparse(self._record.referer)
                if parsed_ref.scheme and parsed_ref.netloc:
                    extra_headers.setdefault(
                        "Origin",
                        f"{parsed_ref.scheme}://{parsed_ref.netloc}",
                    )
            except Exception:
                pass

        if self._record.resume_supported and end > 0:
            return self._network.build_chunk_headers(
                start,
                end,
                referer=self._record.referer,
                cookies=self._record.cookies,
                extra_headers=extra_headers,
            )

        headers: dict[str, str] = {}
        if self._record.referer:
            headers["Referer"] = self._record.referer
        if self._record.cookies:
            headers["Cookie"] = self._record.cookies
        headers.update(extra_headers)
        return headers

    @property
    def download_id(self) -> str:
        return self._download_id

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def speed(self) -> float:
        return self._speed_tracker.speed

    @property
    def downloaded_bytes(self) -> int:
        return self._downloaded_bytes

    # ── Lifecycle Controls ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin or resume the download."""
        self._record = await self._storage.get_download(self._download_id)
        if not self._record:
            raise ValueError(f"Download {self._download_id} not found")

        self._cancelled = False
        self._paused.set()
        self._speed_tracker.reset()
        self._first_byte_seen = False
        self._first_byte_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name=f"download-{self._download_id[:8]}"
        )

    async def pause(self) -> None:
        """Pause the download (chunks will finish their current read)."""
        self._paused.clear()
        await self._storage.update_download_status(
            self._download_id, DownloadStatus.PAUSED
        )
        self._callbacks.on_status_changed(
            self._download_id, DownloadStatus.PAUSED.value
        )
        log.info("Download paused: %s", self._download_id[:8])

    async def resume(self) -> None:
        """Resume a paused download."""
        if self._task and not self._task.done():
            # Task is still alive but waiting on the pause event
            self._paused.set()
            await self._storage.update_download_status(
                self._download_id, DownloadStatus.DOWNLOADING
            )
            self._callbacks.on_status_changed(
                self._download_id, DownloadStatus.DOWNLOADING.value
            )
            log.info("Download resumed: %s", self._download_id[:8])
        else:
            # Task ended — restart it
            await self.start()

    async def cancel(self, *, mark_cancelled: bool = True) -> None:
        """
        Cancel the task and optionally persist a CANCELLED status.

        When ``mark_cancelled`` is False, the download is persisted as PAUSED so
        it can be resumed after application shutdown.
        """
        self._cancelled = True
        self._paused.set()  # unblock if paused
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

        if mark_cancelled:
            await self._storage.update_download_status(
                self._download_id, DownloadStatus.CANCELLED
            )
            self._callbacks.on_status_changed(
                self._download_id, DownloadStatus.CANCELLED.value
            )
            log.info("Download cancelled: %s", self._download_id[:8])
        else:
            await self._storage.update_download_status(
                self._download_id, DownloadStatus.PAUSED
            )
            self._callbacks.on_status_changed(
                self._download_id, DownloadStatus.PAUSED.value
            )
            log.info("Download paused for shutdown: %s", self._download_id[:8])

        self._network.remove_download_limiter(self._download_id)

    async def wait(self) -> None:
        """Wait for the download task to complete."""
        if self._task:
            await asyncio.shield(self._task)

    # ── Main Download Flow ─────────────────────────────────────────────────

    async def _run(self) -> None:
        """Main download coroutine."""
        try:
            record = self._record
            assert record is not None

            self._network.create_download_limiter(
                self._download_id,
                rate_bps=self._get_per_download_rate_bps(),
            )
            self._log_phase("connecting")
            self._request_headers = _load_request_headers_from_metadata(
                record.metadata_json
            )

            # Update status
            await self._storage.update_download_status(
                self._download_id, DownloadStatus.DOWNLOADING
            )
            self._callbacks.on_status_changed(
                self._download_id, DownloadStatus.DOWNLOADING.value
            )

            # ── Step 1: Preflight ──────────────────────────────────────
            if record.file_size <= 0 or not record.chunks_count:
                preflight = await self._network.preflight(
                    record.url,
                    referer=record.referer,
                    cookies=record.cookies,
                    headers=self._request_headers,
                )
                if preflight.error:
                    raise ConnectionError(f"Preflight failed: {preflight.error}")

                # Update record with preflight data
                updates: dict[str, Any] = {}
                if preflight.file_size > 0:
                    updates["file_size"] = preflight.file_size
                    record.file_size = preflight.file_size
                if preflight.resume_supported:
                    updates["resume_supported"] = 1
                    record.resume_supported = True
                if preflight.filename and not record.filename:
                    updates["filename"] = preflight.filename
                    record.filename = preflight.filename
                # Update final URL after redirects
                if preflight.redirected:
                    updates["url"] = preflight.url
                    record.url = preflight.url

                if updates:
                    await self._storage.update_download_field(
                        self._download_id, **updates
                    )
                    log.info(
                        "Preflight: %s — %s, resume=%s",
                        record.filename,
                        format_size(record.file_size),
                        record.resume_supported,
                    )

            # ── Step 2: Calculate chunks ───────────────────────────────
            existing_chunks = await self._storage.get_chunks(self._download_id)

            if not existing_chunks and record.file_size > 0:
                dl_chunks_dir = self._chunks_dir / self._download_id[:8]
                dl_chunks_dir.mkdir(parents=True, exist_ok=True)

                use_parallel = should_use_parallel_chunks(
                    file_size=record.file_size,
                    resume_supported=bool(record.resume_supported),
                    min_parallel_size=self._parallel_chunk_min_size,
                )

                if use_parallel:
                    # Fresh download — calculate chunks
                    num_chunks = self._default_chunks
                    if self._dynamic_chunks:
                        num_chunks = dynamic_chunk_count(
                            record.file_size, self._default_chunks
                        )
                    num_chunks = max(MIN_PARALLEL_CHUNKS, min(int(num_chunks), MAX_PARALLEL_CHUNKS))

                    # Respect the 3–5 chunk policy even when max_chunk_size is small.
                    effective_max_chunk = max(
                        int(self._max_chunk),
                        (record.file_size + MAX_PARALLEL_CHUNKS - 1) // MAX_PARALLEL_CHUNKS,
                    )

                    ranges = calculate_chunks(
                        record.file_size, num_chunks,
                        self._min_chunk, effective_max_chunk,
                    )

                    chunk_records = [
                        ChunkRecord(
                            download_id=self._download_id,
                            chunk_index=i,
                            start_byte=start,
                            end_byte=end,
                            temp_file=str(dl_chunks_dir / f"chunk_{i}.part"),
                        )
                        for i, (start, end) in enumerate(ranges)
                    ]
                    await self._storage.add_chunks(self._download_id, chunk_records)
                    self._chunk_records = chunk_records
                    log.info(
                        "Created %d chunks for %s", len(chunk_records), record.filename
                    )
                else:
                    # Small/short files use a single stream to avoid chunk overhead.
                    single = ChunkRecord(
                        download_id=self._download_id,
                        chunk_index=0,
                        start_byte=0,
                        end_byte=max(0, record.file_size - 1),
                        temp_file=str(dl_chunks_dir / "chunk_0.part"),
                    )
                    await self._storage.add_chunks(self._download_id, [single])
                    self._chunk_records = [single]
                    log.info(
                        "Using single-chunk mode for %s (%s < %s)",
                        record.filename,
                        format_size(record.file_size),
                        format_size(self._parallel_chunk_min_size),
                    )
            elif existing_chunks:
                self._chunk_records = existing_chunks
                # Calculate already-downloaded bytes from existing chunks
                self._downloaded_bytes = sum(
                    c.downloaded_bytes for c in existing_chunks
                )

                # Verify integrity of existing chunk files to detect corruption
                # before resuming download
                await self._verify_chunk_files(existing_chunks)
            else:
                # Single-chunk download (no Range support or unknown size)
                dl_chunks_dir = self._chunks_dir / self._download_id[:8]
                dl_chunks_dir.mkdir(parents=True, exist_ok=True)

                single = ChunkRecord(
                    download_id=self._download_id,
                    chunk_index=0,
                    start_byte=0,
                    end_byte=max(0, record.file_size - 1),
                    temp_file=str(dl_chunks_dir / "chunk_0.part"),
                )
                await self._storage.add_chunks(self._download_id, [single])
                self._chunk_records = [single]

            # ── Step 3: Download chunks in parallel ────────────────────
            incomplete = [
                c for c in self._chunk_records
                if c.status != ChunkStatus.COMPLETED.value
            ]

            if not incomplete:
                log.info("All chunks already complete for %s", record.filename)
            else:
                tasks = [
                    asyncio.create_task(
                        self._download_chunk(chunk),
                        name=f"chunk-{self._download_id[:8]}-{chunk.chunk_index}",
                    )
                    for chunk in incomplete
                ]

                self._log_phase("downloading")

                # Start periodic progress reporter
                progress_task = asyncio.create_task(self._progress_reporter())
                first_byte_guard_task: Optional[asyncio.Task[None]] = None

                try:
                    if self._downloaded_bytes > 0:
                        self._first_byte_event.set()

                    if (record.file_size != 0) and (not self._first_byte_event.is_set()):
                        first_byte_guard_task = asyncio.create_task(
                            self._await_first_byte_or_timeout(),
                            name=f"first-byte-guard-{self._download_id[:8]}",
                        )

                    results = await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    if first_byte_guard_task and not first_byte_guard_task.done():
                        first_byte_guard_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await first_byte_guard_task

                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass

                if (
                    first_byte_guard_task
                    and first_byte_guard_task.done()
                    and not first_byte_guard_task.cancelled()
                ):
                    guard_error = first_byte_guard_task.exception()
                    if guard_error is not None:
                        # Ensure chunk workers don't remain running after guard timeout.
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise guard_error

                # Check for errors
                errors = [r for r in results if isinstance(r, Exception)]
                if errors:
                    # Check for RangeNotSupportedError — needs fallback to single-chunk
                    range_errors = [
                        e for e in errors
                        if isinstance(e, RangeNotSupportedError)
                    ]
                    if range_errors:
                        log.info(
                            "Download %s: Falling back from multi-chunk to single-chunk",
                            self._download_id[:8],
                        )
                        # Delete all existing chunks and create a single chunk
                        await self._storage.delete_chunks(self._download_id)
                        
                        dl_chunks_dir = self._chunks_dir / self._download_id[:8]
                        if dl_chunks_dir.exists():
                            # Remove stale multi-chunk temp files before fallback.
                            for old_part in dl_chunks_dir.glob("*.part"):
                                old_part.unlink(missing_ok=True)
                        dl_chunks_dir.mkdir(parents=True, exist_ok=True)
                        
                        single = ChunkRecord(
                            download_id=self._download_id,
                            chunk_index=0,
                            start_byte=0,
                            end_byte=max(0, record.file_size - 1),
                            temp_file=str(dl_chunks_dir / "chunk_0.part"),
                        )
                        await self._storage.add_chunks(self._download_id, [single])
                        self._chunk_records = [single]
                        
                        # Retry with single chunk
                        tasks = [
                            asyncio.create_task(
                                self._download_chunk(single),
                                name=f"chunk-{self._download_id[:8]}-fallback",
                            )
                        ]
                        
                        progress_task = asyncio.create_task(self._progress_reporter())
                        try:
                            results = await asyncio.gather(*tasks, return_exceptions=True)
                        finally:
                            progress_task.cancel()
                            try:
                                await progress_task
                            except asyncio.CancelledError:
                                pass
                        
                        errors = [r for r in results if isinstance(r, Exception)]
                    
                    # Filter out CancelledError (expected on cancel/pause)
                    if errors:
                        real_errors = [
                            e for e in errors
                            if not isinstance(e, asyncio.CancelledError)
                        ]
                        if real_errors:
                            error_msg = str(real_errors[0])
                            await self._storage.update_download_status(
                                self._download_id, DownloadStatus.FAILED,
                                error_message=error_msg,
                            )
                            self._callbacks.on_status_changed(
                                self._download_id, DownloadStatus.FAILED.value,
                                error=error_msg,
                            )
                            log.error("Download failed: %s — %s",
                                      record.filename, error_msg)
                            return

            if self._cancelled:
                return

            # ── Step 4: Assemble / verify / finalize ───────────────────
            # Final progress flush
            await self._flush_progress()
            await self._storage.update_download_progress(
                self._download_id,
                downloaded_bytes=record.file_size if record.file_size > 0
                else self._downloaded_bytes,
                average_speed=self._speed_tracker.average_speed,
            )

            self._callbacks.on_status_changed(
                self._download_id, DownloadStatus.MERGING.value
            )

            assembly = await assemble_and_verify(
                self._download_id,
                self._storage,
                chunks_dir=self._chunks_dir,
                verify_hash=bool(
                    self._config.get("advanced", {}).get("hash_verify_on_complete", True)
                ),
                hash_algorithm=str(
                    self._config.get("advanced", {}).get("hash_algorithm", "sha256")
                ),
                cleanup=True,
            )

            if not assembly.success:
                error_msg = assembly.error or "Assembly/verification failed"
                self._callbacks.on_status_changed(
                    self._download_id,
                    DownloadStatus.FAILED.value,
                    error=error_msg,
                )
                log.error("Post-download pipeline failed: %s — %s", record.filename, error_msg)
                return

            # Keep persisted path metadata aligned with conflict-resolved output paths.
            resolved_output = Path(assembly.output_path) if assembly.output_path else None
            if resolved_output:
                updates: dict[str, Any] = {
                    "save_path": str(resolved_output),
                    "filename": resolved_output.name,
                }
                if assembly.file_size > 0:
                    updates["file_size"] = assembly.file_size
                await self._storage.update_download_field(self._download_id, **updates)

            # Antivirus post-download execution has been disabled because it
            # introduced reliability issues in production.
            av_enabled = bool(self._config.get("advanced", {}).get("antivirus_enabled", False))
            if av_enabled:
                log.warning(
                    "Antivirus post-download scan is currently disabled for reliability"
                )

            # Defensive persistence: ensure DB status is completed after successful assembly & AV.
            await self._storage.update_download_status(
                self._download_id, DownloadStatus.COMPLETED
            )
            if assembly.file_size > 0:
                await self._storage.update_download_progress(
                    self._download_id,
                    downloaded_bytes=assembly.file_size,
                    average_speed=self._speed_tracker.average_speed,
                )

            self._callbacks.on_status_changed(
                self._download_id, DownloadStatus.COMPLETED.value
            )

            # Update daily stats
            await self._storage.update_daily_stats(
                bytes_downloaded=record.file_size if record.file_size > 0
                else self._downloaded_bytes,
                downloads_completed=1,
                average_speed=self._speed_tracker.average_speed,
            )

            self._callbacks.on_download_complete(self._download_id)
            log.info(
                "Download chunks complete: %s (avg: %s)",
                record.filename,
                format_speed(self._speed_tracker.average_speed),
            )

        except asyncio.CancelledError:
            if self._cancelled:
                log.debug("Download task cancelled: %s", self._download_id[:8])
            else:
                error_msg = "Download worker interrupted unexpectedly"
                log.warning("%s: %s", self._download_id[:8], error_msg)
                await self._storage.update_download_status(
                    self._download_id,
                    DownloadStatus.FAILED,
                    error_message=error_msg,
                )
                self._callbacks.on_status_changed(
                    self._download_id,
                    DownloadStatus.FAILED.value,
                    error=error_msg,
                )
        except Exception as exc:
            log.exception("Download failed: %s", self._download_id[:8])
            await self._storage.update_download_status(
                self._download_id, DownloadStatus.FAILED,
                error_message=str(exc),
            )
            self._callbacks.on_status_changed(
                self._download_id, DownloadStatus.FAILED.value,
                error=str(exc),
            )
        finally:
            self._network.remove_download_limiter(self._download_id)

    # ── Chunk Downloader ───────────────────────────────────────────────────

    async def _download_chunk(self, chunk: ChunkRecord) -> None:
        """
        Download a single byte-range chunk with retry logic.

        Writes data to the chunk's temp file in append mode so that
        interrupted downloads can resume from the last written byte.
        
        If a 416 Range Not Satisfiable error is encountered, raises a
        special RangeNotSupportedError to signal fallback to single-chunk.
        """
        policy = self._network.retry_policy
        session = self._network.session

        for attempt in range(policy.max_retries + 1):
            try:
                # Wait if paused
                await self._paused.wait()
                if self._cancelled:
                    return

                await self._storage.update_chunk_status(
                    self._download_id, chunk.chunk_index,
                    ChunkStatus.DOWNLOADING,
                )
                chunk.status = ChunkStatus.DOWNLOADING.value

                # Build request headers
                # Resume from where the chunk left off
                start = chunk.resume_offset
                end = chunk.end_byte

                if start > end:
                    # Chunk is already fully downloaded
                    await self._storage.update_chunk_status(
                        self._download_id, chunk.chunk_index,
                        ChunkStatus.COMPLETED,
                    )
                    chunk.status = ChunkStatus.COMPLETED.value
                    return

                # Only use Range header if server supports it and we have
                # meaningful byte ranges
                headers = self._build_chunk_request_headers(start, end)

                async with session.get(
                    self._record.url if self._record else "",
                    headers=headers,
                    proxy=self._network.proxy_url,
                ) as resp:
                    # Handle Range Not Satisfiable — trigger fallback
                    if resp.status == 416:
                        log.warning(
                            "Chunk %d got 416 Range Not Satisfiable — fallback to single-chunk",
                            chunk.chunk_index,
                        )
                        # Mark this download for fallback conversion
                        await self._mark_range_unsupported()
                        raise RangeNotSupportedError(
                            f"Server returned 416 for chunk {chunk.chunk_index}"
                        )
                    
                    if resp.status not in (200, 206):
                        error_type = policy.classify_http_status(resp.status)
                        if policy.is_retryable_status(resp.status):
                            log.debug(
                                "Chunk %d got retryable HTTP %d (%s)",
                                chunk.chunk_index, resp.status, error_type.value,
                            )
                            raise aiohttp.ClientResponseError(
                                resp.request_info, resp.history,
                                status=resp.status,
                                message=f"HTTP {resp.status}",
                            )
                        raise ConnectionError(
                            f"HTTP {resp.status} ({error_type.value}) for chunk {chunk.chunk_index}"
                        )

                    # Open temp file — use literal modes for type-safe overloads.
                    if chunk.downloaded_bytes > 0:
                        file_ctx = aiofiles.open(chunk.temp_file, "ab")
                    else:
                        file_ctx = aiofiles.open(chunk.temp_file, "wb")

                    async with file_ctx as f:
                        await self._stream_chunk_data(resp, f, chunk)

                # Chunk completed successfully
                await self._storage.update_chunk_status(
                    self._download_id, chunk.chunk_index,
                    ChunkStatus.COMPLETED,
                )
                chunk.status = ChunkStatus.COMPLETED.value
                await self._storage.update_chunk_progress(
                    self._download_id, chunk.chunk_index,
                    chunk.downloaded_bytes,
                )
                log.debug(
                    "Chunk %d complete (%s)",
                    chunk.chunk_index,
                    format_size(chunk.total_bytes),
                )
                return  # success — exit retry loop

            except asyncio.CancelledError:
                # Save progress before exiting
                await self._storage.update_chunk_progress(
                    self._download_id, chunk.chunk_index,
                    chunk.downloaded_bytes,
                )
                raise

            except RangeNotSupportedError:
                # Signal fallback — don't retry, just raise
                raise

            except FirstByteTimeoutError:
                # First-byte stalls should fail fast instead of looping retries.
                await self._storage.update_chunk_status(
                    self._download_id,
                    chunk.chunk_index,
                    ChunkStatus.FAILED,
                    error_message=(
                        f"No first byte received within "
                        f"{self._first_byte_timeout_seconds:.0f}s"
                    ),
                )
                chunk.status = ChunkStatus.FAILED.value
                raise

            except Exception as exc:
                # Save progress
                await self._storage.update_chunk_progress(
                    self._download_id, chunk.chunk_index,
                    chunk.downloaded_bytes,
                )

                error_type = classify_error(exc)

                # Additional logging for proxy errors to help diagnose configuration issues
                if error_type.value == "proxy_error":
                    log.debug(
                        "Chunk %d proxy error on attempt %d (may indicate proxy config issue): %s",
                        chunk.chunk_index, attempt + 1, exc,
                    )

                if not policy.should_retry(attempt, exc):
                    log.error(
                        "Chunk %d failed (%s): %s",
                        chunk.chunk_index, error_type.value, exc,
                    )
                    await self._storage.update_chunk_status(
                        self._download_id, chunk.chunk_index,
                        ChunkStatus.FAILED, error_message=str(exc),
                    )
                    chunk.status = ChunkStatus.FAILED.value
                    raise

                delay = policy.get_retry_delay(attempt, exc)
                if delay is None:
                    # Should not retry (e.g., permanent failure)
                    log.error(
                        "Chunk %d failed (non-retryable, %s): %s",
                        chunk.chunk_index, error_type.value, exc,
                    )
                    await self._storage.update_chunk_status(
                        self._download_id, chunk.chunk_index,
                        ChunkStatus.FAILED, error_message=str(exc),
                    )
                    chunk.status = ChunkStatus.FAILED.value
                    raise
                
                log.warning(
                    "Chunk %d attempt %d/%d failed (%s): %s — retry in %.1fs",
                    chunk.chunk_index, attempt + 1,
                    policy.max_retries + 1, error_type.value, exc, delay,
                )
                await asyncio.sleep(delay)

    async def _await_first_byte_or_timeout(self) -> None:
        """Wait for first payload byte while ignoring paused intervals."""
        deadline = time.monotonic() + self._first_byte_timeout_seconds
        while not self._first_byte_event.is_set():
            await asyncio.sleep(0.2)

            if self._cancelled:
                raise asyncio.CancelledError()

            # Paused periods should not consume the first-byte timeout budget.
            if not self._paused.is_set():
                deadline = time.monotonic() + self._first_byte_timeout_seconds
                continue

            if time.monotonic() >= deadline:
                raise FirstByteTimeoutError(
                    f"No first byte received within {self._first_byte_timeout_seconds:.0f}s"
                )

    async def _iter_response_chunks(
        self,
        resp: aiohttp.ClientResponse,
        chunk: ChunkRecord,
    ):
        """Yield response chunks while enforcing a first-byte timeout."""
        iterator = resp.content.iter_chunked(self._buffer_size).__aiter__()
        try:
            first = await asyncio.wait_for(
                iterator.__anext__(),
                timeout=self._first_byte_timeout_seconds,
            )
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            raise FirstByteTimeoutError(
                f"No first byte received within {self._first_byte_timeout_seconds:.0f}s "
                f"for chunk {chunk.chunk_index}"
            ) from exc

        if not self._first_byte_seen:
            self._first_byte_seen = True
            self._log_phase("first_byte")

        yield first

        async for data in iterator:
            yield data

    async def _stream_chunk_data(
        self,
        resp: aiohttp.ClientResponse,
        f: Any,
        chunk: ChunkRecord,
    ) -> None:
        """Stream response bytes to disk with optional bounded prefetching."""
        if self._prefetch_buffers <= 1:
            async for data in self._iter_response_chunks(resp, chunk):
                await self._apply_chunk_data(data, chunk, f)
            return

        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._prefetch_buffers)
        producer_error: Optional[Exception] = None

        async def _producer() -> None:
            nonlocal producer_error
            try:
                async for data in self._iter_response_chunks(resp, chunk):
                    await self._paused.wait()
                    if self._cancelled:
                        break
                    await queue.put(data)
            except Exception as exc:
                producer_error = exc
            finally:
                await queue.put(None)

        producer_task = asyncio.create_task(_producer())
        try:
            while True:
                data = await queue.get()
                if data is None:
                    break
                await self._apply_chunk_data(data, chunk, f)

            await producer_task
            if producer_error is not None:
                raise producer_error
        finally:
            if not producer_task.done():
                producer_task.cancel()
                with suppress(asyncio.CancelledError):
                    await producer_task

    async def _apply_chunk_data(self, data: bytes, chunk: ChunkRecord, f: Any) -> None:
        """Apply pause/cancel/throttle logic and persist one data block."""
        await self._paused.wait()
        if self._cancelled:
            raise asyncio.CancelledError()

        await self._network.throttle(len(data), self._download_id)
        await f.write(data)

        if not self._first_byte_event.is_set():
            self._first_byte_event.set()

        chunk.downloaded_bytes += len(data)
        self._downloaded_bytes += len(data)
        self._speed_tracker.record(self._downloaded_bytes)

    # ── Progress Reporting ─────────────────────────────────────────────────

    async def _mark_range_unsupported(self) -> None:
        """
        Mark the download as not supporting Range requests.
        
        This is called when a 416 error is received, indicating the server
        doesn't support HTTP Range requests despite the preflight suggesting it does.
        """
        if self._record:
            await self._storage.update_download_field(
                self._download_id,
                resume_supported=0,
            )
            self._record.resume_supported = False
            log.warning(
                "Download %s: Range requests not supported by server",
                self._download_id[:8],
            )

    async def _progress_reporter(self) -> None:
        """Periodically report progress and flush to DB."""
        interval = self._config.get("advanced", {}).get(
            "speed_sample_interval_ms", 500
        ) / 1000.0

        while True:
            await asyncio.sleep(interval)
            if self._cancelled:
                return

            speed = self._speed_tracker.speed
            total = self._record.file_size if self._record else -1
            remaining = max(0, total - self._downloaded_bytes) if total > 0 else 0
            eta = calculate_eta(remaining, speed)

            self._callbacks.on_progress(
                self._download_id,
                self._downloaded_bytes,
                total,
                speed,
                eta,
            )

            # Push live chunk activity counters to UI.
            total_chunks = len(self._chunk_records)
            if total_chunks > 1:
                completed_chunks = sum(
                    1 for c in self._chunk_records
                    if str(c.status).lower() == ChunkStatus.COMPLETED.value
                )
                started_chunks = sum(
                    1 for c in self._chunk_records
                    if int(c.downloaded_bytes) > 0
                    or str(c.status).lower() in {
                        ChunkStatus.DOWNLOADING.value,
                        ChunkStatus.COMPLETED.value,
                    }
                )
                visible_chunks = completed_chunks if completed_chunks > 0 else started_chunks
                self._callbacks.on_chunk_progress(
                    self._download_id,
                    visible_chunks,
                    total_chunks,
                )

            # Record speed sample for the graph
            await self._storage.add_speed_sample(
                self._download_id, speed, self._downloaded_bytes,
            )

            # Periodic flush of chunk progress to DB
            now = time.monotonic()
            if now - self._last_flush >= PROGRESS_FLUSH_INTERVAL:
                await self._flush_progress()
                self._last_flush = now

    async def _flush_progress(self) -> None:
        """Flush current progress to the database."""
        await self._storage.update_download_progress(
            self._download_id,
            self._downloaded_bytes,
            average_speed=self._speed_tracker.speed,
        )
        await self._storage.flush_chunk_progress()
    async def _verify_chunk_files(self, chunks: list[ChunkRecord]) -> None:
        """
        Verify integrity of existing chunk files to detect corruption before resume.
        
        For each completed or partially downloaded chunk file, verify:
        • File exists
        • File size matches expected (or is not beyond expected)
        • File is readable
        
        If a chunk file is corrupted, it's reset to allow re-download.
        
        Args:
            chunks: List of ChunkRecord objects to verify.
        """
        async def _reset_chunk(chunk: ChunkRecord, reason: str) -> None:
            """Reset a chunk so it is safely re-downloaded from byte zero."""
            previous = max(0, int(chunk.downloaded_bytes))
            if previous:
                self._downloaded_bytes = max(0, self._downloaded_bytes - previous)

            chunk.downloaded_bytes = 0
            await self._storage.update_chunk_progress(
                self._download_id,
                chunk.chunk_index,
                0,
            )
            await self._storage.update_chunk_status(
                self._download_id,
                chunk.chunk_index,
                ChunkStatus.PENDING,
                error_message=reason,
            )
            chunk.status = ChunkStatus.PENDING.value

        for chunk in chunks:
            if chunk.downloaded_bytes > 0:
                chunk_file = Path(chunk.temp_file)
                
                # File must exist
                if not chunk_file.exists():
                    log.warning(
                        "Chunk %d file missing, will re-download: %s",
                        chunk.chunk_index, chunk_file,
                    )
                    await _reset_chunk(chunk, "File was deleted; re-downloading")
                    continue
                
                try:
                    actual_size = chunk_file.stat().st_size
                    expected_size = chunk.end_byte - chunk.start_byte + 1
                    
                    # If file is larger than expected, something is wrong
                    if actual_size > expected_size:
                        log.warning(
                            "Chunk %d file corrupted (size %d > expected %d), "
                            "will re-download",
                            chunk.chunk_index, actual_size, expected_size,
                        )
                        chunk_file.unlink()
                        await _reset_chunk(chunk, "File corrupted; re-downloading")
                        continue

                    # If persisted progress does not match on-disk bytes, reset chunk
                    # to avoid out-of-range resumes and duplicate/truncated writes.
                    if actual_size != int(chunk.downloaded_bytes):
                        log.warning(
                            "Chunk %d progress mismatch (db=%d, disk=%d), will re-download",
                            chunk.chunk_index,
                            chunk.downloaded_bytes,
                            actual_size,
                        )
                        await _reset_chunk(chunk, "Progress mismatch; re-downloading")
                        continue
                    
                    # Test readability with a small read
                    try:
                        async with aiofiles.open(chunk_file, mode='rb') as f:
                            await f.read(min(1024, actual_size))
                    except (IOError, OSError) as e:
                        log.warning(
                            "Chunk %d file unreadable: %s, will re-download",
                            chunk.chunk_index, e,
                        )
                        chunk_file.unlink()
                        await _reset_chunk(chunk, "File unreadable; re-downloading")
                        continue
                    
                    log.debug(
                        "Chunk %d verified: %d/%d bytes readable",
                        chunk.chunk_index, actual_size, expected_size,
                    )
                
                except Exception as e:
                    log.error(
                        "Chunk %d verification failed unexpectedly: %s",
                        chunk.chunk_index, e,
                    )
                    # Reset chunk on any verification error to be safe
                    await _reset_chunk(chunk, "Verification failed; re-downloading")

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DOWNLOAD ENGINE                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class DownloadEngine:
    """
    Top-level download orchestrator.

    Manages the download queue, enforces concurrency limits, and provides
    the public API for adding / pausing / resuming / cancelling downloads.
    """

    def __init__(
        self,
        storage: StorageManager,
        network: NetworkManager,
        config: dict[str, Any],
        chunks_dir: Path | str = "chunks",
        callbacks: Optional[DownloadCallbacks] = None,
    ) -> None:
        self._storage = storage
        self._network = network
        self._config = config
        self._chunks_dir = Path(chunks_dir)
        self._callbacks: DownloadCallbacks = callbacks or NullCallbacks()

        self._max_concurrent: int = config.get("general", {}).get(
            "max_concurrent_downloads", 4
        )
        adv_cfg = config.get("advanced", {})
        self._queue_poll_idle_seconds = max(
            0.2,
            float(adv_cfg.get("queue_poll_interval_ms", int(QUEUE_POLL_IDLE_SECONDS * 1000)) or int(QUEUE_POLL_IDLE_SECONDS * 1000)) / 1000.0,
        )
        self._queue_poll_busy_seconds = max(
            0.05,
            float(adv_cfg.get("queue_poll_busy_interval_ms", int(QUEUE_POLL_BUSY_SECONDS * 1000)) or int(QUEUE_POLL_BUSY_SECONDS * 1000)) / 1000.0,
        )
        if self._queue_poll_busy_seconds > self._queue_poll_idle_seconds:
            self._queue_poll_busy_seconds = self._queue_poll_idle_seconds
        self._task_runtime_config = _build_task_runtime_config(config)
        self._active_tasks: dict[str, DownloadTask] = {}
        self._queue_task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._queue_wakeup = asyncio.Event()
        self._orphan_recovery_counts: dict[str, int] = {}
        self._max_orphan_recovery_attempts: int = 3

    @property
    def active_count(self) -> int:
        """Number of currently active downloads."""
        return len(self._active_tasks)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @max_concurrent.setter
    def max_concurrent(self, value: int) -> None:
        self._max_concurrent = max(1, value)
        log.info("Max concurrent downloads: %d", self._max_concurrent)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def network_manager(self) -> NetworkManager:
        """Expose network manager for diagnostics endpoints."""
        return self._network

    def set_callbacks(self, callbacks: DownloadCallbacks) -> None:
        """Attach UI callbacks."""
        self._callbacks = callbacks

    def apply_runtime_config(self, config: dict[str, Any]) -> None:
        """
        Apply updated configuration without restarting the engine.

        Existing downloads continue running; new queue decisions and chunking
        parameters pick up the updated values immediately.
        """
        self._config = config
        self._max_concurrent = max(
            1,
            int(config.get("general", {}).get("max_concurrent_downloads", self._max_concurrent)),
        )

        adv = config.get("advanced", {})
        self._task_runtime_config = _build_task_runtime_config(config)
        new_queue_poll_idle = max(
            0.2,
            float(adv.get("queue_poll_interval_ms", int(self._queue_poll_idle_seconds * 1000)) or int(self._queue_poll_idle_seconds * 1000)) / 1000.0,
        )
        new_queue_poll_busy = max(
            0.05,
            float(adv.get("queue_poll_busy_interval_ms", int(self._queue_poll_busy_seconds * 1000)) or int(self._queue_poll_busy_seconds * 1000)) / 1000.0,
        )
        if new_queue_poll_busy > new_queue_poll_idle:
            new_queue_poll_busy = new_queue_poll_idle
        per_download_rate_bps = max(
            0,
            int(config.get("network", {}).get("per_download_bandwidth_kbps", 0) or 0) * 1024,
        )

        self._queue_poll_idle_seconds = new_queue_poll_idle
        self._queue_poll_busy_seconds = new_queue_poll_busy

        for task in self._active_tasks.values():
            if hasattr(task, "apply_runtime_config"):
                task.apply_runtime_config(self._task_runtime_config, config)

        for dl_id in self._active_tasks.keys():
            self._network.create_download_limiter(dl_id, rate_bps=per_download_rate_bps)

        self._notify_queue_wakeup()

        log.info(
            "Runtime config applied (max_concurrent=%d, default_chunks=%d, per_download_rate=%s, queue_poll_idle=%.2fs, queue_poll_busy=%.2fs)",
            self._max_concurrent,
            self._task_runtime_config.default_chunks,
            f"{per_download_rate_bps // 1024} KB/s" if per_download_rate_bps > 0 else "unlimited",
            self._queue_poll_idle_seconds,
            self._queue_poll_busy_seconds,
        )

    def _notify_queue_wakeup(self) -> None:
        """Wake queue dispatcher on relevant events (new work, slot changes, config updates)."""
        self._queue_wakeup.set()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the queue processor."""
        if self._running:
            return

        # Recover interrupted in-progress downloads from previous app sessions.
        await self._recover_interrupted_downloads()

        self._running = True
        self._queue_task = asyncio.create_task(
            self._process_queue(), name="queue-processor"
        )
        self._notify_queue_wakeup()
        log.info("Download engine started (max concurrent: %d)", self._max_concurrent)

    async def stop(self, *, cancel_active: bool = False) -> None:
        """
        Stop the engine and terminate active tasks.

        Args:
            cancel_active: If True, active downloads are marked CANCELLED.
                If False (default), active downloads are persisted as PAUSED so
                they can resume on next startup.
        """
        self._running = False
        if self._queue_task:
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass

        # Terminate active downloads. By default we preserve resumable state.
        for dl_id in list(self._active_tasks.keys()):
            await self.cancel(dl_id, mark_cancelled=cancel_active)

        self._active_tasks.clear()
        log.info("Download engine stopped")

    async def _process_queue(self) -> None:
        """Queue processor loop with event-driven wakeups and polling fallback."""
        while self._running:
            try:
                recovered = await self._requeue_orphaned_active_downloads()

                # Clean up completed tasks
                done_ids = [
                    dl_id for dl_id, task in self._active_tasks.items()
                    if not task.is_active
                ]
                for dl_id in done_ids:
                    del self._active_tasks[dl_id]

                # Fill available slots from queue
                available = self._max_concurrent - len(self._active_tasks)
                started = 0
                if available > 0:
                    queued = await self._storage.get_queued_downloads()
                    for record in queued[:available]:
                        if record.id not in self._active_tasks:
                            await self._start_task(record.id)
                            started += 1

                if recovered > 0 or done_ids or started > 0:
                    # Continue immediately while work is moving.
                    continue

            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Queue processor error")

            # Event-driven wait with bounded fallback polling for recovery.
            self._queue_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._queue_wakeup.wait(),
                    timeout=self._queue_poll_idle_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _requeue_orphaned_active_downloads(self) -> int:
        """
        Re-queue active-status downloads that have no running in-memory task.

        This covers edge cases where a worker task was interrupted unexpectedly
        and the persisted status remained as downloading/merging/verifying.
        """
        active_records = await self._storage.get_active_downloads()
        if not active_records:
            self._orphan_recovery_counts.clear()
            return 0

        active_ids = {record.id for record in active_records}
        for tracked_id in list(self._orphan_recovery_counts.keys()):
            if tracked_id not in active_ids:
                self._orphan_recovery_counts.pop(tracked_id, None)

        recovered = 0
        for record in active_records:
            task = self._active_tasks.get(record.id)
            if task is not None and task.is_active:
                self._orphan_recovery_counts.pop(record.id, None)
                continue

            attempts = self._orphan_recovery_counts.get(record.id, 0) + 1
            self._orphan_recovery_counts[record.id] = attempts

            if attempts >= self._max_orphan_recovery_attempts:
                error_msg = (
                    "Download worker exited repeatedly during startup. "
                    "Marked as failed to stop restart loop."
                )
                await self._storage.update_download_status(
                    record.id,
                    DownloadStatus.FAILED,
                    error_message=error_msg,
                )
                try:
                    self._callbacks.on_status_changed(
                        record.id,
                        DownloadStatus.FAILED.value,
                        error=error_msg,
                    )
                except Exception:
                    log.exception("Failed to emit failed status for %s", record.id[:8])

                self._orphan_recovery_counts.pop(record.id, None)
                log.error(
                    "Download %s exceeded orphan recovery attempts (%d); marked failed",
                    record.id[:8],
                    self._max_orphan_recovery_attempts,
                )
                continue

            await self._storage.update_download_status(record.id, DownloadStatus.QUEUED)
            self._callbacks.on_status_changed(record.id, DownloadStatus.QUEUED.value)
            recovered += 1

        if recovered > 0:
            log.warning("Recovered %d orphaned active downloads to queued state", recovered)

        return recovered

    async def _recover_interrupted_downloads(self) -> None:
        """
        Re-queue downloads left in in-progress states after abrupt shutdown.

        If IDM exits while downloads are marked DOWNLOADING/MERGING/VERIFYING,
        those statuses can become stale. On startup we convert them back to
        QUEUED so queue processing can resume safely.
        """
        active = await self._storage.get_active_downloads()
        if not active:
            return

        for record in active:
            await self._storage.update_download_status(
                record.id, DownloadStatus.QUEUED
            )
            self._callbacks.on_status_changed(
                record.id, DownloadStatus.QUEUED.value
            )

        log.info(
            "Recovered %d interrupted downloads to queued state",
            len(active),
        )

    async def _start_task(self, download_id: str) -> None:
        """Create and start the appropriate task (HTTP, FTP, or Torrent)."""
        record = await self._storage.get_download(download_id)
        if not record:
            return

        url = record.url.lower()
        task: Any

        if url.startswith("ftp://"):
            task = FtpTask(
                download_id=download_id,
                storage=self._storage,
                config=self._config,
                callbacks=self._callbacks,
            )
        elif url.startswith("magnet:") or url.endswith(".torrent"):
            if not hasattr(self, "_torrent_manager"):
                from core.torrent import TorrentManager
                try:
                    self._torrent_manager = TorrentManager(self._config)
                    await self._torrent_manager.start()
                except RuntimeError as e:
                    # libtorrent not available — mark download as failed with clear message
                    error_msg = (
                        f"BitTorrent support unavailable: {e}. "
                        "Please install python-libtorrent to download torrents. "
                        "https://github.com/arvidn/libtorrent"
                    )
                    log.error("Torrent support disabled: %s", error_msg)
                    await self._storage.update_download_status(
                        download_id,
                        DownloadStatus.FAILED,
                        error_message=error_msg,
                    )
                    return
                except Exception as e:
                    error_msg = f"Failed to initialize torrent engine: {e}"
                    log.error("Torrent initialization failed: %s", error_msg)
                    await self._storage.update_download_status(
                        download_id,
                        DownloadStatus.FAILED,
                        error_message=error_msg,
                    )
                    return

            task = TorrentTask(
                download_id=download_id,
                storage=self._storage,
                torrent_manager=self._torrent_manager,
                callbacks=self._callbacks,
            )
        elif _looks_like_hls_url(record.url):
            task = HLSTask(
                download_id=download_id,
                storage=self._storage,
                config=self._config,
                callbacks=self._callbacks,
            )
        else:
            task = DownloadTask(
                download_id=download_id,
                storage=self._storage,
                network=self._network,
                config=self._config,
                callbacks=self._callbacks,
                chunks_dir=self._chunks_dir,
            )

        self._active_tasks[download_id] = task
        await task.start()
        log.info("Started %s task: %s", task.__class__.__name__, download_id[:8])

    # ── Public API ─────────────────────────────────────────────────────────

    async def add_download(
        self,
        url: str,
        *,
        filename: str = "",
        save_path: str = "",
        priority: str | DownloadPriority = DownloadPriority.NORMAL,
        category: str = "Other",
        referer: Optional[str] = None,
        cookies: Optional[str] = None,
        user_agent: Optional[str] = None,
        metadata_json: Optional[str] = None,
        hash_expected: Optional[str] = None,
        start_immediately: bool = True,
        allow_out_of_root: bool = False,
    ) -> str:
        """
        Add a new download to the queue.

        Returns:
            The download UUID.
        """
        if not filename:
            filename = extract_filename_from_url(url)
        filename = _sanitize_output_filename(filename)

        if not save_path:
            download_dir = str(
                self._config.get("general", {}).get("download_directory", "")
            ).strip()
            if not download_dir:
                download_dir = r"D:\idm down"
            parent = Path(download_dir) / category
            safe_filename = _truncate_filename_for_parent(parent, filename)
            if safe_filename != filename:
                log.info("Filename shortened for filesystem safety: %s -> %s", filename[:80], safe_filename)
                filename = safe_filename
            save_path = str(parent / filename)
        else:
            canonical_save_path, in_root = _canonicalize_save_path(save_path, self._config)
            if not in_root and not allow_out_of_root:
                raise ValueError(
                    "Save path is outside configured download roots. "
                    "Explicit UI confirmation is required for out-of-root paths."
                )
            save_path = _normalize_save_path_for_platform(canonical_save_path)

        download_id = await self._storage.add_download(
            url=url,
            filename=filename,
            save_path=save_path,
            priority=priority,
            category=category,
            referer=referer,
            cookies=cookies,
            user_agent=user_agent,
            metadata_json=metadata_json,
            hash_expected=hash_expected,
            initial_status=(
                DownloadStatus.QUEUED if start_immediately else DownloadStatus.PAUSED
            ),
        )

        record = await self._storage.get_download(download_id)
        if record:
            self._callbacks.on_download_added(download_id, record)

        log.info("Download added: %s → %s", download_id[:8], filename)
        self._notify_queue_wakeup()
        return download_id

    async def pause(self, download_id: str) -> None:
        """Pause a download."""
        task = self._active_tasks.get(download_id)
        if task:
            await task.pause()
            self._notify_queue_wakeup()
            return

        record = await self._storage.get_download(download_id)
        if record and record.status in (DownloadStatus.QUEUED.value, DownloadStatus.FAILED.value):
            await self._storage.update_download_status(download_id, DownloadStatus.PAUSED)
            self._callbacks.on_status_changed(download_id, DownloadStatus.PAUSED.value)
            self._notify_queue_wakeup()

    async def resume(self, download_id: str) -> None:
        """Resume a paused download."""
        task = self._active_tasks.get(download_id)
        if task and task.is_active:
            await task.resume()
            self._notify_queue_wakeup()
        else:
            # Re-queue the download
            record = await self._storage.get_download(download_id)
            if record and record.status in (
                DownloadStatus.PAUSED.value,
                DownloadStatus.FAILED.value,
                DownloadStatus.CANCELLED.value,
            ):
                await self._storage.update_download_status(
                    download_id, DownloadStatus.QUEUED
                )
                self._callbacks.on_status_changed(
                    download_id, DownloadStatus.QUEUED.value
                )
                self._notify_queue_wakeup()

    async def cancel(
        self, download_id: str, *, mark_cancelled: bool = True
    ) -> None:
        """Cancel a download.

        Args:
            download_id: Download ID to cancel.
            mark_cancelled: If False, persist as PAUSED for shutdown recovery.
        """
        task = self._active_tasks.get(download_id)
        if task:
            await task.cancel(mark_cancelled=mark_cancelled)
            if not mark_cancelled:
                await self._storage.update_download_status(
                    download_id, DownloadStatus.PAUSED
                )
                self._callbacks.on_status_changed(
                    download_id, DownloadStatus.PAUSED.value
                )
            del self._active_tasks[download_id]
        elif mark_cancelled:
            await self._storage.update_download_status(
                download_id, DownloadStatus.CANCELLED
            )
            self._callbacks.on_status_changed(download_id, DownloadStatus.CANCELLED.value)
        else:
            await self._storage.update_download_status(download_id, DownloadStatus.PAUSED)
            self._callbacks.on_status_changed(download_id, DownloadStatus.PAUSED.value)

        self._notify_queue_wakeup()

    async def retry(self, download_id: str) -> None:
        """Retry a failed download."""
        record = await self._storage.get_download(download_id)
        if record and record.status == DownloadStatus.FAILED.value:
            await self._storage.update_download_status(
                download_id, DownloadStatus.QUEUED
            )
            self._notify_queue_wakeup()

    async def remove(self, download_id: str, delete_file: bool = False) -> None:
        """Remove a download from the queue and database."""
        await self.cancel(download_id)
        record = await self._storage.get_download(download_id)
        await self._storage.delete_download(download_id)

        if delete_file:
            if record:
                path = Path(record.save_path)
                if path.exists():
                    path.unlink(missing_ok=True)

    async def pause_all(self) -> set[str]:
        """Pause all active downloads and return paused download IDs."""
        paused_ids: set[str] = set()
        for dl_id, task in list(self._active_tasks.items()):
            if task.is_active:
                await task.pause()
                paused_ids.add(dl_id)
        return paused_ids

    async def resume_all(self) -> None:
        """Resume all paused downloads."""
        paused = await self._storage.get_all_downloads(
            status=DownloadStatus.PAUSED
        )
        for record in paused:
            await self.resume(record.id)
        self._notify_queue_wakeup()

    async def resume_downloads(self, download_ids: set[str]) -> None:
        """Resume only the specified paused download IDs."""
        for download_id in download_ids:
            await self.resume(download_id)
        self._notify_queue_wakeup()

    def get_active_speeds(self) -> dict[str, float]:
        """Return current speed for each active download."""
        return {
            dl_id: task.speed
            for dl_id, task in self._active_tasks.items()
            if task.is_active
        }

    def get_total_speed(self) -> float:
        """Return total download speed across all active downloads."""
        return sum(
            task.speed for task in self._active_tasks.values()
            if task.is_active
        )


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  HLS TASK WRAPPER                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class HLSTask:
    """Wrapper for the HLS worker to match DownloadTask interface."""

    def __init__(
        self,
        download_id: str,
        storage: StorageManager,
        config: dict[str, Any],
        callbacks: DownloadCallbacks,
    ) -> None:
        self.download_id = download_id
        self._storage = storage
        self._config = config
        self._callbacks = callbacks
        self._task: Optional[asyncio.Task[None]] = None
        self._speed = 0.0
        self._downloaded = 0

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def downloaded_bytes(self) -> int:
        return self._downloaded

    async def start(self) -> None:
        record = await self._storage.get_download(self.download_id)
        if not record:
            return

        await self._storage.update_download_status(self.download_id, DownloadStatus.DOWNLOADING)
        self._callbacks.on_status_changed(self.download_id, DownloadStatus.DOWNLOADING.value)

        self._task = asyncio.create_task(
            self._run(record), name=f"hls-{self.download_id[:8]}"
        )

    async def pause(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

        await self._storage.update_download_status(self.download_id, DownloadStatus.PAUSED)
        self._callbacks.on_status_changed(self.download_id, DownloadStatus.PAUSED.value)

    async def resume(self) -> None:
        if not self.is_active:
            await self.start()

    async def cancel(self, *, mark_cancelled: bool = True) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

        status = DownloadStatus.CANCELLED if mark_cancelled else DownloadStatus.PAUSED
        await self._storage.update_download_status(self.download_id, status)
        self._callbacks.on_status_changed(self.download_id, status.value)

    async def _run(self, record: DownloadRecord) -> None:
        try:
            from core.hls_worker import HLSWorkerConfig, run_hls_download

            request_headers = _load_request_headers_from_metadata(record.metadata_json)
            output_path = str(record.save_path)

            # Avoid writing playlist text as final output when URL itself is .m3u8.
            if Path(output_path).suffix.lower() == ".m3u8":
                out = Path(output_path).with_suffix(".ts")
                output_path = str(out)
                await self._storage.update_download_field(
                    self.download_id,
                    save_path=output_path,
                    filename=out.name,
                )

            user_agent = record.user_agent or request_headers.get("User-Agent")
            last_db_flush = 0.0

            async def _on_hls_progress(snapshot: dict[str, Any]) -> None:
                nonlocal last_db_flush
                self._downloaded = int(snapshot.get("downloaded_bytes", 0) or 0)
                self._speed = float(snapshot.get("speed_bps", 0.0) or 0.0)

                total_segments = int(snapshot.get("total_segments", 0) or 0)
                downloaded_segments = int(snapshot.get("downloaded_segments", 0) or 0)
                self._callbacks.on_chunk_progress(
                    self.download_id,
                    downloaded_segments,
                    total_segments,
                )

                total_bytes = int(record.file_size) if int(record.file_size) > 0 else -1
                self._callbacks.on_progress(
                    self.download_id,
                    self._downloaded,
                    total_bytes,
                    self._speed,
                    -1.0,
                )

                now = time.monotonic()
                if now - last_db_flush >= PROGRESS_FLUSH_INTERVAL:
                    await self._storage.update_download_progress(
                        self.download_id,
                        downloaded_bytes=self._downloaded,
                        average_speed=self._speed,
                    )
                    last_db_flush = now

            cfg = HLSWorkerConfig(
                master_or_media_url=record.url,
                output_path=output_path,
                user_agent=user_agent or HLSWorkerConfig.user_agent,
                referer=record.referer,
                headers=request_headers,
                cookies=_cookie_header_to_dict(record.cookies),
                authorization=request_headers.get("Authorization"),
                merge_mode="auto",
                progress_callback=_on_hls_progress,
            )

            result = await run_hls_download(cfg)
            final_path = Path(result.output_path)
            file_size = final_path.stat().st_size if final_path.exists() else 0
            self._downloaded = int(file_size)

            await self._storage.update_download_field(
                self.download_id,
                save_path=str(final_path),
                filename=final_path.name,
                file_size=file_size,
            )
            await self._storage.update_download_progress(
                self.download_id,
                downloaded_bytes=file_size,
                average_speed=0.0,
            )
            await self._storage.update_download_status(self.download_id, DownloadStatus.COMPLETED)

            self._callbacks.on_status_changed(self.download_id, DownloadStatus.COMPLETED.value)
            self._callbacks.on_download_complete(self.download_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._storage.update_download_status(
                self.download_id,
                DownloadStatus.FAILED,
                error_message=str(exc),
            )
            self._callbacks.on_status_changed(
                self.download_id,
                DownloadStatus.FAILED.value,
                error=str(exc),
            )
            log.error("HLS task failed %s: %s", self.download_id[:8], exc)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FTP TASK WRAPPER                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class FtpTask:
    """Wrapper for FtpDownloader to match DownloadTask interface."""

    def __init__(
        self, download_id: str, storage: StorageManager,
        config: dict, callbacks: DownloadCallbacks,
    ) -> None:
        self.download_id = download_id
        self._storage = storage
        self._config = config
        self._callbacks = callbacks
        self._downloader: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._speed = 0.0
        self._downloaded = 0

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def downloaded_bytes(self) -> int:
        return self._downloaded

    async def start(self) -> None:
        from core.ftp import FtpDownloader
        record = await self._storage.get_download(self.download_id)
        if not record: return

        self._downloader = FtpDownloader(
            url=record.url,
            save_path=record.save_path,
            config=self._config,
            on_progress=self._on_progress,
            on_status=self._on_status,
        )
        self._task = asyncio.create_task(self._downloader.start())

    async def pause(self) -> None:
        if self._downloader: await self._downloader.pause()

    async def resume(self) -> None:
        if self._downloader: await self._downloader.resume()

    async def cancel(self, *, mark_cancelled: bool = True) -> None:
        if mark_cancelled:
            if self._downloader:
                await self._downloader.stop()
        if self._task:
            self._task.cancel()

    def _on_progress(self, downloaded: int, total: int, speed: float, eta: float) -> None:
        self._downloaded = downloaded
        self._speed = speed
        self._callbacks.on_progress(self.download_id, downloaded, total, speed, eta)

    def _on_status(self, status: str) -> None:
        self._callbacks.on_status_changed(self.download_id, status)
        if status == "completed":
            self._callbacks.on_download_complete(self.download_id)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  TORRENT TASK WRAPPER                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class TorrentTask:
    """Wrapper for TorrentManager to match DownloadTask interface."""

    def __init__(
        self, download_id: str, storage: StorageManager,
        torrent_manager: Any, callbacks: DownloadCallbacks,
    ) -> None:
        self.download_id = download_id
        self._storage = storage
        self._mgr = torrent_manager
        self._callbacks = callbacks
        self._info_hash: Optional[str] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._status: dict = {}

    @property
    def is_active(self) -> bool:
        return self._monitor_task is not None and not self._monitor_task.done()

    @property
    def speed(self) -> float:
        value = self._status.get("download_rate", 0.0)
        return float(value)

    @property
    def downloaded_bytes(self) -> int:
        value = self._status.get("total_done", 0)
        return int(value)

    async def start(self) -> None:
        record = await self._storage.get_download(self.download_id)
        if not record: return

        if record.url.startswith("magnet:"):
            self._info_hash = await self._mgr.add_magnet(record.url, record.save_path)
        else:
            self._info_hash = await self._mgr.add_torrent_file(record.url, record.save_path)

        self._monitor_task = asyncio.create_task(self._monitor())

    async def pause(self) -> None:
        if self._info_hash: await self._mgr.pause(self._info_hash)

    async def resume(self) -> None:
        if self._info_hash: await self._mgr.resume(self._info_hash)

    async def cancel(self, *, mark_cancelled: bool = True) -> None:
        if self._info_hash:
            if mark_cancelled:
                await self._mgr.remove(self._info_hash)
            else:
                await self._mgr.pause(self._info_hash)
        if self._monitor_task:
            self._monitor_task.cancel()

    async def _monitor(self) -> None:
        while True:
            if not self._info_hash: break
            status = self._mgr.get_status(self._info_hash)
            if not status: break

            self._status = status
            self._callbacks.on_progress(
                self.download_id,
                status["total_done"],
                status["total_wanted"],
                status["download_rate"],
                -1  # libtorrent doesn't provide easy ETA in Status
            )

            if status["state"] == "finished" or status["is_seeding"]:
                self._callbacks.on_status_changed(self.download_id, "completed")
                self._callbacks.on_download_complete(self.download_id)
                break

            await asyncio.sleep(1)
