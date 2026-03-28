# IDM v2.0 — storage.py — audited 2026-03-28
"""
IDM Core — SQLite Storage Layer
================================
Async database wrapper for persisting all download state.

This module provides the ``StorageManager`` class which handles:

    • Download records (url, filename, status, progress, priority, …)
    • Chunk records (byte ranges, per-chunk progress for resume)
    • Speed history samples (for real-time graphs)
    • Daily statistics (aggregated bytes / download counts)
    • Download history with full-text search and filtering
    • Schema versioning and automatic migrations

All public methods are async (powered by ``aiosqlite``) so they can be
called directly from the engine's asyncio event loop without blocking.

Usage::

    storage = StorageManager("downloads.db")
    await storage.initialize()

    download_id = await storage.add_download(
        url="https://example.com/file.zip",
        filename="file.zip",
        save_path="/downloads/file.zip",
    )

    await storage.update_download_status(download_id, DownloadStatus.DOWNLOADING)
    await storage.close()

Schema version history:
    1 — Initial schema (downloads, chunks, speed_history, daily_stats)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
import time
import re
from datetime import datetime, date, timezone
from enum import Enum
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional, Sequence

import aiosqlite

from utils.credentials import decrypt_secret, encrypt_secret

# ── Module Logger ──────────────────────────────────────────────────────────────
log = logging.getLogger("idm.core.storage")

# ── Current Schema Version ─────────────────────────────────────────────────────
SCHEMA_VERSION: int = 1


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ENUMS                                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class DownloadStatus(str, Enum):
    """Lifecycle states for a download job."""
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MERGING = "merging"          # chunks being assembled
    VERIFYING = "verifying"      # hash check in progress
    SEEDING = "seeding"          # torrent seeding


class DownloadPriority(str, Enum):
    """Queue priority levels."""
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class ChunkStatus(str, Enum):
    """Lifecycle states for a single chunk."""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DATA CLASSES                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class DownloadRecord:
    """
    Represents a single download job in the database.

    All fields map 1:1 to the ``downloads`` table columns.
    """
    id: str
    url: str
    filename: str
    save_path: str
    file_size: int = -1                               # -1 = unknown
    downloaded_bytes: int = 0
    status: str = DownloadStatus.QUEUED.value
    priority: str = DownloadPriority.NORMAL.value
    category: str = "Other"
    chunks_count: int = 0
    date_added: str = ""
    date_completed: Optional[str] = None
    average_speed: float = 0.0                         # bytes/sec
    hash_expected: Optional[str] = None
    hash_actual: Optional[str] = None
    hash_verified: bool = False
    referer: Optional[str] = None
    cookies: Optional[str] = None
    user_agent: Optional[str] = None
    proxy_config: Optional[str] = None                 # JSON string
    error_message: Optional[str] = None
    retry_count: int = 0
    resume_supported: bool = False
    metadata_json: Optional[str] = None                # extensible JSON blob

    def __post_init__(self) -> None:
        if not self.date_added:
            self.date_added = _now_iso()

    @property
    def progress_percent(self) -> float:
        """Calculate download progress as 0.0–100.0."""
        if self.file_size <= 0:
            return 0.0
        return min(100.0, (self.downloaded_bytes / self.file_size) * 100.0)

    @property
    def is_active(self) -> bool:
        """Return True if the download is currently in progress."""
        return self.status in (
            DownloadStatus.DOWNLOADING.value,
            DownloadStatus.MERGING.value,
            DownloadStatus.VERIFYING.value,
        )

    @property
    def is_resumable(self) -> bool:
        """Return True if the download can be resumed."""
        return (
            self.resume_supported
            and self.status in (
                DownloadStatus.PAUSED.value,
                DownloadStatus.FAILED.value,
            )
        )


@dataclass
class ChunkRecord:
    """
    Represents a single byte-range chunk within a download.

    Each download is split into N chunks that are downloaded in parallel.
    Chunk offsets are persisted so that interrupted downloads can resume
    from exactly where each chunk left off.
    """
    id: int = 0                          # auto-incremented by DB
    download_id: str = ""
    chunk_index: int = 0
    start_byte: int = 0
    end_byte: int = 0
    downloaded_bytes: int = 0
    status: str = ChunkStatus.PENDING.value
    temp_file: str = ""
    error_message: Optional[str] = None

    @property
    def total_bytes(self) -> int:
        """Total size of this chunk in bytes."""
        return self.end_byte - self.start_byte + 1

    @property
    def remaining_bytes(self) -> int:
        """Bytes still to download for this chunk."""
        return max(0, self.total_bytes - self.downloaded_bytes)

    @property
    def progress_percent(self) -> float:
        """Chunk progress as 0.0–100.0."""
        total = self.total_bytes
        if total <= 0:
            return 0.0
        return min(100.0, (self.downloaded_bytes / total) * 100.0)

    @property
    def resume_offset(self) -> int:
        """The byte offset from which to resume downloading this chunk."""
        return self.start_byte + self.downloaded_bytes


@dataclass
class SpeedSample:
    """A single speed measurement point for graphs and statistics."""
    id: int = 0
    download_id: str = ""
    timestamp: str = ""
    speed_bytes_per_sec: float = 0.0
    downloaded_bytes: int = 0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = _now_iso()


@dataclass
class DailyStats:
    """Aggregated download statistics for a single day."""
    date: str = ""                       # YYYY-MM-DD
    total_bytes: int = 0
    total_downloads: int = 0
    average_speed: float = 0.0           # bytes/sec

    def __post_init__(self) -> None:
        if not self.date:
            self.date = date.today().isoformat()


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SQL SCHEMA                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_SCHEMA_V1: str = """
-- ╔══════════════════════════════════════════════════════════════════════╗
-- ║  Schema v1 — Initial                                               ║
-- ╚══════════════════════════════════════════════════════════════════════╝

-- Metadata table for tracking schema version
CREATE TABLE IF NOT EXISTS schema_info (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- ── Downloads ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS downloads (
    id                TEXT    PRIMARY KEY,
    url               TEXT    NOT NULL,
    filename          TEXT    NOT NULL,
    save_path         TEXT    NOT NULL,
    file_size         INTEGER NOT NULL DEFAULT -1,
    downloaded_bytes  INTEGER NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'queued',
    priority          TEXT    NOT NULL DEFAULT 'normal',
    category          TEXT    NOT NULL DEFAULT 'Other',
    chunks_count      INTEGER NOT NULL DEFAULT 0,
    date_added        TEXT    NOT NULL,
    date_completed    TEXT,
    average_speed     REAL    NOT NULL DEFAULT 0.0,
    hash_expected     TEXT,
    hash_actual       TEXT,
    hash_verified     INTEGER NOT NULL DEFAULT 0,
    referer           TEXT,
    cookies           TEXT,
    user_agent        TEXT,
    proxy_config      TEXT,
    error_message     TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    resume_supported  INTEGER NOT NULL DEFAULT 0,
    metadata_json     TEXT
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_downloads_status
    ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_downloads_priority
    ON downloads(priority);
CREATE INDEX IF NOT EXISTS idx_downloads_date_added
    ON downloads(date_added);
CREATE INDEX IF NOT EXISTS idx_downloads_category
    ON downloads(category);

-- Full-text index for scalable history search.
CREATE VIRTUAL TABLE IF NOT EXISTS downloads_fts USING fts5(
    filename,
    url,
    category,
    content='downloads',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS downloads_ai AFTER INSERT ON downloads BEGIN
    INSERT INTO downloads_fts(rowid, filename, url, category)
    VALUES (new.rowid, new.filename, new.url, new.category);
END;

CREATE TRIGGER IF NOT EXISTS downloads_ad AFTER DELETE ON downloads BEGIN
    INSERT INTO downloads_fts(downloads_fts, rowid, filename, url, category)
    VALUES('delete', old.rowid, old.filename, old.url, old.category);
END;

CREATE TRIGGER IF NOT EXISTS downloads_au AFTER UPDATE ON downloads BEGIN
    INSERT INTO downloads_fts(downloads_fts, rowid, filename, url, category)
    VALUES('delete', old.rowid, old.filename, old.url, old.category);
    INSERT INTO downloads_fts(rowid, filename, url, category)
    VALUES (new.rowid, new.filename, new.url, new.category);
END;

-- ── Chunks ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chunks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id      TEXT    NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
    chunk_index      INTEGER NOT NULL,
    start_byte       INTEGER NOT NULL,
    end_byte         INTEGER NOT NULL,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'pending',
    temp_file        TEXT    NOT NULL DEFAULT '',
    error_message    TEXT,
    UNIQUE(download_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_download_id
    ON chunks(download_id);

-- ── Speed History ──────────────────────────────────────────────────────
-- Stores periodic speed samples for the real-time graph.
-- Old entries are pruned automatically by the manager.
CREATE TABLE IF NOT EXISTS speed_history (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id           TEXT    NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
    timestamp             TEXT    NOT NULL,
    speed_bytes_per_sec   REAL    NOT NULL DEFAULT 0.0,
    downloaded_bytes      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_speed_history_download_id
    ON speed_history(download_id);
CREATE INDEX IF NOT EXISTS idx_speed_history_timestamp
    ON speed_history(timestamp);

-- ── Daily Statistics ───────────────────────────────────────────────────
-- Aggregated per-day totals used by the statistics dashboard.
CREATE TABLE IF NOT EXISTS daily_stats (
    date             TEXT    PRIMARY KEY,
    total_bytes      INTEGER NOT NULL DEFAULT 0,
    total_downloads  INTEGER NOT NULL DEFAULT 0,
    average_speed    REAL    NOT NULL DEFAULT 0.0
);
"""


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  STORAGE MANAGER                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class StorageManager:
    """
    Async SQLite storage layer for IDM.

    Wraps ``aiosqlite`` to provide typed CRUD operations for downloads,
    chunks, speed history, and daily statistics.

    Thread-safety note:
        This class is designed to be called exclusively from the
        ``EngineThread``'s asyncio loop.  If you need to call from the
        Qt thread, use ``engine.run_coroutine(storage.some_method(…))``.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        # Batch speed history writes to reduce hot-path commit pressure.
        self._speed_samples_buffer: list[tuple[str, str, float, int]] = []
        self._speed_flush_interval_seconds: float = 4.0
        self._speed_flush_max_samples: int = 25
        self._last_speed_flush_monotonic: float = time.monotonic()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """
        Open the database connection and apply schema migrations.

        Creates the database file and parent directories if they don't exist.
        Enables WAL mode for better concurrent read performance.
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        log.info("Opening database: %s", self._db_path)

        self._db = await aiosqlite.connect(
            str(self._db_path),
            isolation_level=None,  # autocommit off — we manage transactions
        )

        # ── Performance pragmas ────────────────────────────────────────
        # WAL mode allows concurrent reads while writing
        await self._db.execute("PRAGMA journal_mode=WAL")
        # Synchronous NORMAL is a good balance of safety vs speed
        await self._db.execute("PRAGMA synchronous=NORMAL")
        # Enable foreign key constraint enforcement
        await self._db.execute("PRAGMA foreign_keys=ON")
        # Larger cache for better read performance (4000 × 4 KB ≈ 16 MB)
        await self._db.execute("PRAGMA cache_size=-16000")

        # Row factory for dict-like access
        self._db.row_factory = aiosqlite.Row

        await self._apply_migrations()
        log.info("Database initialized (schema v%d)", SCHEMA_VERSION)

    async def close(self) -> None:
        """Close the database connection gracefully."""
        if self._db:
            log.info("Closing database…")
            await self.flush_speed_samples(force=True)
            await self._db.close()
            self._db = None

    @property
    def is_open(self) -> bool:
        """Return True if the database connection is active."""
        return self._db is not None

    def _ensure_open(self) -> aiosqlite.Connection:
        """Raise RuntimeError if the database is not open."""
        if self._db is None:
            raise RuntimeError("Database is not open. Call initialize() first.")
        return self._db

    # ── Schema Migrations ──────────────────────────────────────────────────

    async def _apply_migrations(self) -> None:
        """
        Apply pending schema migrations.

        Reads the current schema version from ``schema_info`` and applies
        any migrations from that version to ``SCHEMA_VERSION``.
        """
        db = self._ensure_open()

        # Check if schema_info table exists (first-time setup)
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_info'"
        )
        row = await cursor.fetchone()

        if row is None:
            # First-time: apply full v1 schema
            log.info("Applying initial schema (v1)…")
            await db.executescript(_SCHEMA_V1)
            await db.execute(
                "INSERT INTO schema_info (key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),)
            )
            await db.commit()
            return

        # Read current version
        cursor = await db.execute(
            "SELECT value FROM schema_info WHERE key='version'"
        )
        row = await cursor.fetchone()
        current_version = int(row["value"]) if row else 0

        if current_version >= SCHEMA_VERSION:
            log.debug("Schema up to date (v%d)", current_version)
            return

        # Apply incremental migrations
        # Future migrations go here:
        # if current_version < 2:
        #     await self._migrate_v1_to_v2(db)
        #     current_version = 2

        await self._ensure_fts_index(db)

        # Update stored version
        await db.execute(
            "UPDATE schema_info SET value=? WHERE key='version'",
            (str(SCHEMA_VERSION),)
        )
        await db.commit()
        log.info("Schema migrated to v%d", SCHEMA_VERSION)

    async def _ensure_fts_index(self, db: aiosqlite.Connection) -> None:
        """Ensure FTS5 table/triggers exist and backfill content."""
        await db.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS downloads_fts USING fts5(
                filename,
                url,
                category,
                content='downloads',
                content_rowid='rowid'
            );

            CREATE TRIGGER IF NOT EXISTS downloads_ai AFTER INSERT ON downloads BEGIN
                INSERT INTO downloads_fts(rowid, filename, url, category)
                VALUES (new.rowid, new.filename, new.url, new.category);
            END;

            CREATE TRIGGER IF NOT EXISTS downloads_ad AFTER DELETE ON downloads BEGIN
                INSERT INTO downloads_fts(downloads_fts, rowid, filename, url, category)
                VALUES('delete', old.rowid, old.filename, old.url, old.category);
            END;

            CREATE TRIGGER IF NOT EXISTS downloads_au AFTER UPDATE ON downloads BEGIN
                INSERT INTO downloads_fts(downloads_fts, rowid, filename, url, category)
                VALUES('delete', old.rowid, old.filename, old.url, old.category);
                INSERT INTO downloads_fts(rowid, filename, url, category)
                VALUES (new.rowid, new.filename, new.url, new.category);
            END;
            """
        )
        await db.execute("INSERT INTO downloads_fts(downloads_fts) VALUES ('rebuild')")
        await db.commit()

    # ══════════════════════════════════════════════════════════════════════
    #  DOWNLOAD CRUD
    # ══════════════════════════════════════════════════════════════════════

    async def add_download(
        self,
        url: str,
        filename: str,
        save_path: str,
        *,
        file_size: int = -1,
        priority: str | DownloadPriority = DownloadPriority.NORMAL,
        category: str = "Other",
        chunks_count: int = 0,
        hash_expected: Optional[str] = None,
        referer: Optional[str] = None,
        cookies: Optional[str] = None,
        user_agent: Optional[str] = None,
        proxy_config: Optional[str] = None,
        resume_supported: bool = False,
        metadata_json: Optional[str] = None,
        initial_status: str | DownloadStatus = DownloadStatus.QUEUED,
    ) -> str:
        """
        Insert a new download record.

        Args:
            url: The full download URL.
            filename: Target filename (basename).
            save_path: Full path where the file will be saved.
            file_size: Total file size in bytes (-1 if unknown).
            priority: Queue priority (high/normal/low).
            category: File category (Video, Audio, etc.).
            chunks_count: Number of parallel chunks.
            hash_expected: Expected SHA-256 hash for verification.
            referer: HTTP Referer header value.
            cookies: Cookie string to send with the request.
            user_agent: Custom User-Agent string.
            proxy_config: JSON-encoded proxy configuration.
            resume_supported: Whether the server supports Range requests.
            metadata_json: Arbitrary JSON metadata.

        Returns:
            The UUID of the newly created download record.
        """
        db = self._ensure_open()
        download_id = str(uuid.uuid4())
        priority_val = priority.value if isinstance(priority, DownloadPriority) else priority
        status_val = (
            initial_status.value
            if isinstance(initial_status, DownloadStatus)
            else str(initial_status)
        )

        async with self._lock:
            await db.execute(
                """
                INSERT INTO downloads (
                    id, url, filename, save_path, file_size, downloaded_bytes,
                    status, priority, category, chunks_count, date_added,
                    hash_expected, referer, cookies, user_agent, proxy_config,
                    resume_supported, metadata_json
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    download_id, url, filename, save_path, file_size,
                    status_val, priority_val, category,
                    chunks_count, _now_iso(), hash_expected, referer,
                    _encrypt_sensitive(cookies),
                    user_agent,
                    _encrypt_sensitive(proxy_config),
                    1 if resume_supported else 0, _encrypt_sensitive(metadata_json),
                ),
            )
            await db.commit()

        log.info("Download added: %s → %s [%s]", download_id[:8], filename, priority_val)
        return download_id

    async def get_download(self, download_id: str) -> Optional[DownloadRecord]:
        """
        Fetch a single download by ID.

        Returns:
            A ``DownloadRecord`` or None if not found.
        """
        db = self._ensure_open()
        cursor = await db.execute(
            "SELECT * FROM downloads WHERE id = ?", (download_id,)
        )
        row = await cursor.fetchone()
        return _row_to_download(row) if row else None

    async def get_all_downloads(
        self,
        *,
        status: Optional[str | DownloadStatus] = None,
        category: Optional[str] = None,
        priority: Optional[str | DownloadPriority] = None,
        search_query: Optional[str] = None,
        order_by: str = "date_added",
        order_desc: bool = True,
        limit: int = 0,
        offset: int = 0,
    ) -> list[DownloadRecord]:
        """
        Fetch downloads with optional filters.

        Args:
            status: Filter by download status.
            category: Filter by file category.
            priority: Filter by queue priority.
            search_query: Search in filename and URL (LIKE match).
            order_by: Column name to sort by.
            order_desc: True for DESC, False for ASC.
            limit: Maximum rows to return (0 = unlimited).
            offset: Row offset for pagination.

        Returns:
            List of ``DownloadRecord`` objects matching the filters.
        """
        db = self._ensure_open()

        # Build dynamic WHERE clause
        conditions: list[str] = []
        params: list[Any] = []

        if status is not None:
            status_val = status.value if isinstance(status, DownloadStatus) else status
            conditions.append("status = ?")
            params.append(status_val)

        if category is not None:
            conditions.append("category = ?")
            params.append(category)

        if priority is not None:
            priority_val = priority.value if isinstance(priority, DownloadPriority) else priority
            conditions.append("priority = ?")
            params.append(priority_val)

        if search_query:
            fts_query = _to_fts_query(search_query)
            conditions.append("rowid IN (SELECT rowid FROM downloads_fts WHERE downloads_fts MATCH ?)")
            params.append(fts_query)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Validate order_by to prevent SQL injection
        allowed_columns = {
            "date_added", "date_completed", "filename", "file_size",
            "downloaded_bytes", "status", "priority", "category",
            "average_speed",
        }
        if order_by not in allowed_columns:
            order_by = "date_added"

        direction = "DESC" if order_desc else "ASC"
        query = f"SELECT * FROM downloads WHERE {where_clause} ORDER BY {order_by} {direction}"

        if limit > 0:
            query += f" LIMIT {limit} OFFSET {offset}"

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [_row_to_download(row) for row in rows]

    async def get_active_downloads(self) -> list[DownloadRecord]:
        """Fetch all currently active (in-progress) downloads."""
        db = self._ensure_open()
        cursor = await db.execute(
            "SELECT * FROM downloads WHERE status IN (?, ?, ?) ORDER BY date_added",
            (
                DownloadStatus.DOWNLOADING.value,
                DownloadStatus.MERGING.value,
                DownloadStatus.VERIFYING.value,
            ),
        )
        rows = await cursor.fetchall()
        return [_row_to_download(row) for row in rows]

    async def get_queued_downloads(self) -> list[DownloadRecord]:
        """
        Fetch queued downloads ordered by priority then date.

        Priority ordering: high → normal → low.
        Within the same priority, older downloads come first (FIFO).
        """
        db = self._ensure_open()
        cursor = await db.execute(
            """
            SELECT * FROM downloads
            WHERE status = ?
            ORDER BY
                CASE priority
                    WHEN 'high' THEN 0
                    WHEN 'normal' THEN 1
                    WHEN 'low' THEN 2
                    ELSE 3
                END,
                date_added ASC
            """,
            (DownloadStatus.QUEUED.value,),
        )
        rows = await cursor.fetchall()
        return [_row_to_download(row) for row in rows]

    async def update_download_status(
        self,
        download_id: str,
        status: str | DownloadStatus,
        *,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Update the status of a download.

        Automatically sets ``date_completed`` when status is COMPLETED.
        Increments ``retry_count`` when status is FAILED.
        """
        db = self._ensure_open()
        status_val = status.value if isinstance(status, DownloadStatus) else status

        async with self._lock:
            if status_val == DownloadStatus.COMPLETED.value:
                await db.execute(
                    """
                    UPDATE downloads
                    SET status = ?, date_completed = ?, error_message = NULL
                    WHERE id = ?
                    """,
                    (status_val, _now_iso(), download_id),
                )
            elif status_val == DownloadStatus.FAILED.value:
                await db.execute(
                    """
                    UPDATE downloads
                    SET status = ?, error_message = ?, retry_count = retry_count + 1
                    WHERE id = ?
                    """,
                    (status_val, error_message, download_id),
                )
            else:
                await db.execute(
                    "UPDATE downloads SET status = ?, error_message = ? WHERE id = ?",
                    (status_val, error_message, download_id),
                )
            await db.commit()

        log.debug("Download %s → status=%s", download_id[:8], status_val)

    async def update_download_progress(
        self,
        download_id: str,
        downloaded_bytes: int,
        *,
        average_speed: Optional[float] = None,
    ) -> None:
        """
        Update the byte progress of a download.

        Args:
            download_id: The download UUID.
            downloaded_bytes: Total bytes downloaded so far.
            average_speed: Current average speed in bytes/sec.
        """
        db = self._ensure_open()
        async with self._lock:
            if average_speed is not None:
                await db.execute(
                    """
                    UPDATE downloads
                    SET downloaded_bytes = ?, average_speed = ?
                    WHERE id = ?
                    """,
                    (downloaded_bytes, average_speed, download_id),
                )
            else:
                await db.execute(
                    "UPDATE downloads SET downloaded_bytes = ? WHERE id = ?",
                    (downloaded_bytes, download_id),
                )
            await db.commit()

    async def update_download_field(
        self,
        download_id: str,
        **fields: Any,
    ) -> None:
        """
        Update arbitrary fields on a download record.

        Args:
            download_id: The download UUID.
            **fields: Column-name=value pairs to update.

        Raises:
            ValueError: If no fields are provided or a field name is invalid.
        """
        if not fields:
            raise ValueError("No fields provided to update")

        allowed = {
            "url", "filename", "save_path", "file_size", "downloaded_bytes",
            "status", "priority", "category", "chunks_count", "date_completed",
            "average_speed", "hash_expected", "hash_actual", "hash_verified",
            "referer", "cookies", "user_agent", "proxy_config",
            "error_message", "retry_count", "resume_supported", "metadata_json",
        }
        invalid = set(fields.keys()) - allowed
        if invalid:
            raise ValueError(f"Invalid field names: {invalid}")

        # Encrypt sensitive values before persistence.
        for sensitive_field in ("cookies", "proxy_config", "metadata_json"):
            if sensitive_field in fields:
                fields[sensitive_field] = _encrypt_sensitive(fields.get(sensitive_field))

        db = self._ensure_open()
        set_clause = ", ".join(f"{col} = ?" for col in fields)
        values = list(fields.values()) + [download_id]

        async with self._lock:
            await db.execute(
                f"UPDATE downloads SET {set_clause} WHERE id = ?", values
            )
            await db.commit()

    async def normalize_legacy_filenames(self) -> int:
        """
        Normalize historical filename values that accidentally contain paths.

        Returns:
            Number of records updated.
        """
        db = self._ensure_open()

        cursor = await db.execute(
            "SELECT id, filename FROM downloads WHERE filename IS NOT NULL"
        )
        rows = await cursor.fetchall()

        updates: list[tuple[str, str]] = []
        for row in rows:
            dl_id = str(row["id"])
            raw = str(row["filename"] or "").strip()
            if not raw:
                continue

            if "\\" not in raw and "/" not in raw:
                continue

            normalized = Path(raw).name.strip()
            if normalized and normalized != raw:
                updates.append((normalized, dl_id))

        if not updates:
            return 0

        async with self._lock:
            await db.executemany(
                "UPDATE downloads SET filename = ? WHERE id = ?",
                updates,
            )
            await db.commit()

        log.info("Normalized %d legacy filename entries", len(updates))
        return len(updates)

    async def delete_download(
        self,
        download_id: str,
        *,
        delete_chunks: bool = True,
    ) -> bool:
        """
        Delete a download record and optionally its chunks.

        The CASCADE constraint on chunks handles automatic deletion
        when ``delete_chunks`` is True (default via ON DELETE CASCADE).

        Args:
            download_id: The download UUID.
            delete_chunks: Also delete associated chunk records.

        Returns:
            True if a record was deleted, False if not found.
        """
        db = self._ensure_open()

        async with self._lock:
            if delete_chunks:
                await db.execute(
                    "DELETE FROM chunks WHERE download_id = ?", (download_id,)
                )
                await db.execute(
                    "DELETE FROM speed_history WHERE download_id = ?", (download_id,)
                )

            cursor = await db.execute(
                "DELETE FROM downloads WHERE id = ?", (download_id,)
            )
            await db.commit()

        deleted = cursor.rowcount > 0
        if deleted:
            log.info("Download deleted: %s", download_id[:8])
        return deleted

    async def delete_completed_downloads(self) -> int:
        """
        Delete all completed downloads from history.

        Returns:
            Number of records deleted.
        """
        db = self._ensure_open()

        async with self._lock:
            # First delete related data
            await db.execute(
                """
                DELETE FROM chunks WHERE download_id IN
                    (SELECT id FROM downloads WHERE status = ?)
                """,
                (DownloadStatus.COMPLETED.value,),
            )
            await db.execute(
                """
                DELETE FROM speed_history WHERE download_id IN
                    (SELECT id FROM downloads WHERE status = ?)
                """,
                (DownloadStatus.COMPLETED.value,),
            )
            cursor = await db.execute(
                "DELETE FROM downloads WHERE status = ?",
                (DownloadStatus.COMPLETED.value,),
            )
            await db.commit()

        count = cursor.rowcount
        log.info("Cleared %d completed downloads", count)
        return count

    async def get_download_count(
        self,
        status: Optional[str | DownloadStatus] = None,
    ) -> int:
        """
        Count downloads, optionally filtered by status.

        Returns:
            The number of matching download records.
        """
        db = self._ensure_open()

        if status is not None:
            status_val = status.value if isinstance(status, DownloadStatus) else status
            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM downloads WHERE status = ?",
                (status_val,),
            )
        else:
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM downloads")

        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    # ══════════════════════════════════════════════════════════════════════
    #  CHUNK CRUD
    # ══════════════════════════════════════════════════════════════════════

    async def add_chunks(
        self,
        download_id: str,
        chunks: Sequence[ChunkRecord],
    ) -> None:
        """
        Insert multiple chunk records for a download.

        Existing chunks for the same download are deleted first
        (full recalculation of chunk boundaries).

        Args:
            download_id: The parent download UUID.
            chunks: Sequence of ``ChunkRecord`` objects to insert.
        """
        db = self._ensure_open()

        async with self._lock:
            # Clear old chunks (in case of re-chunking)
            await db.execute(
                "DELETE FROM chunks WHERE download_id = ?", (download_id,)
            )

            await db.executemany(
                """
                INSERT INTO chunks (
                    download_id, chunk_index, start_byte, end_byte,
                    downloaded_bytes, status, temp_file
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        download_id, c.chunk_index, c.start_byte, c.end_byte,
                        c.downloaded_bytes, c.status, c.temp_file,
                    )
                    for c in chunks
                ],
            )

            # Update chunks_count on the download record
            await db.execute(
                "UPDATE downloads SET chunks_count = ? WHERE id = ?",
                (len(chunks), download_id),
            )
            await db.commit()

        log.debug(
            "Added %d chunks for download %s", len(chunks), download_id[:8]
        )

    async def get_chunks(self, download_id: str) -> list[ChunkRecord]:
        """
        Fetch all chunks for a download, ordered by chunk index.

        Returns:
            List of ``ChunkRecord`` objects.
        """
        db = self._ensure_open()
        cursor = await db.execute(
            "SELECT * FROM chunks WHERE download_id = ? ORDER BY chunk_index",
            (download_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_chunk(row) for row in rows]

    async def delete_chunks(self, download_id: str) -> int:
        """
        Delete all chunk records for a download.

        Returns:
            Number of deleted chunk rows.
        """
        db = self._ensure_open()
        async with self._lock:
            cursor = await db.execute(
                "DELETE FROM chunks WHERE download_id = ?",
                (download_id,),
            )
            await db.execute(
                "UPDATE downloads SET chunks_count = 0 WHERE id = ?",
                (download_id,),
            )
            await db.commit()

        return max(0, cursor.rowcount)

    async def update_chunk_progress(
        self,
        download_id: str,
        chunk_index: int,
        downloaded_bytes: int,
    ) -> None:
        """
        Update the byte progress of a specific chunk.

        This is called frequently during downloading to persist
        resume points.
        """
        db = self._ensure_open()
        async with self._lock:
            await db.execute(
                """
                UPDATE chunks
                SET downloaded_bytes = ?
                WHERE download_id = ? AND chunk_index = ?
                """,
                (downloaded_bytes, download_id, chunk_index),
            )
        # Note: commit is deferred — caller should use flush_chunk_progress()
        # periodically to batch commits for performance.

    async def update_chunk_status(
        self,
        download_id: str,
        chunk_index: int,
        status: str | ChunkStatus,
        *,
        error_message: Optional[str] = None,
    ) -> None:
        """Update the status of a specific chunk."""
        db = self._ensure_open()
        status_val = status.value if isinstance(status, ChunkStatus) else status

        async with self._lock:
            await db.execute(
                """
                UPDATE chunks
                SET status = ?, error_message = ?
                WHERE download_id = ? AND chunk_index = ?
                """,
                (status_val, error_message, download_id, chunk_index),
            )
            await db.commit()

    async def flush_chunk_progress(self) -> None:
        """
        Commit any pending chunk progress updates.

        Call this periodically (e.g. every 1–2 seconds) rather than
        after every chunk write to reduce I/O overhead.
        """
        db = self._ensure_open()
        async with self._lock:
            await db.commit()

    async def get_incomplete_chunks(self, download_id: str) -> list[ChunkRecord]:
        """
        Fetch chunks that need downloading (pending or failed).

        This is used when resuming an interrupted download.

        Returns:
            List of ``ChunkRecord`` objects that are not yet complete.
        """
        db = self._ensure_open()
        cursor = await db.execute(
            """
            SELECT * FROM chunks
            WHERE download_id = ? AND status IN (?, ?)
            ORDER BY chunk_index
            """,
            (download_id, ChunkStatus.PENDING.value, ChunkStatus.FAILED.value),
        )
        rows = await cursor.fetchall()
        return [_row_to_chunk(row) for row in rows]

    # ══════════════════════════════════════════════════════════════════════
    #  SPEED HISTORY
    # ══════════════════════════════════════════════════════════════════════

    async def add_speed_sample(
        self,
        download_id: str,
        speed_bytes_per_sec: float,
        downloaded_bytes: int,
    ) -> None:
        """
        Record a speed measurement sample.

        Args:
            download_id: The download this sample belongs to.
            speed_bytes_per_sec: Current speed reading.
            downloaded_bytes: Cumulative bytes at this point.
        """
        db = self._ensure_open()
        self._speed_samples_buffer.append(
            (download_id, _now_iso(), speed_bytes_per_sec, downloaded_bytes)
        )

        now = time.monotonic()
        if (
            len(self._speed_samples_buffer) >= self._speed_flush_max_samples
            or (now - self._last_speed_flush_monotonic) >= self._speed_flush_interval_seconds
        ):
            await self.flush_speed_samples(force=False)

    async def flush_speed_samples(self, *, force: bool = False) -> None:
        """Flush buffered speed samples in one transaction."""
        if not self._speed_samples_buffer:
            return

        now = time.monotonic()
        if not force:
            if (
                len(self._speed_samples_buffer) < self._speed_flush_max_samples
                and (now - self._last_speed_flush_monotonic) < self._speed_flush_interval_seconds
            ):
                return

        db = self._ensure_open()
        batch = list(self._speed_samples_buffer)
        self._speed_samples_buffer.clear()

        async with self._lock:
            await db.executemany(
                """
                INSERT INTO speed_history (download_id, timestamp, speed_bytes_per_sec, downloaded_bytes)
                SELECT ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM downloads WHERE id = ?)
                """,
                [
                    (download_id, ts, speed, downloaded, download_id)
                    for (download_id, ts, speed, downloaded) in batch
                ],
            )
            await db.commit()

        self._last_speed_flush_monotonic = now

    async def get_speed_history(
        self,
        download_id: str,
        *,
        limit: int = 120,
    ) -> list[SpeedSample]:
        """
        Fetch recent speed samples for a download.

        Args:
            download_id: The download UUID.
            limit: Maximum samples to return (most recent first).

        Returns:
            List of ``SpeedSample`` objects, ordered by timestamp ascending.
        """
        await self.flush_speed_samples(force=True)
        db = self._ensure_open()
        cursor = await db.execute(
            """
            SELECT * FROM speed_history
            WHERE download_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (download_id, limit),
        )
        rows = await cursor.fetchall()
        # Reverse so the result is chronological (oldest first)
        return [_row_to_speed_sample(row) for row in reversed(list(rows))]

    async def get_global_speed_history(
        self,
        *,
        limit: int = 120,
    ) -> list[SpeedSample]:
        """
        Fetch aggregated speed samples across all active downloads.

        Groups samples by timestamp (rounded to the nearest second)
        and sums the speeds.

        Returns:
            Aggregated ``SpeedSample`` list, chronological order.
        """
        await self.flush_speed_samples(force=True)
        db = self._ensure_open()
        cursor = await db.execute(
            """
            SELECT
                0 as id,
                '' as download_id,
                SUBSTR(timestamp, 1, 19) as timestamp,
                SUM(speed_bytes_per_sec) as speed_bytes_per_sec,
                SUM(downloaded_bytes) as downloaded_bytes
            FROM speed_history
            GROUP BY SUBSTR(timestamp, 1, 19)
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [_row_to_speed_sample(row) for row in reversed(list(rows))]

    async def prune_speed_history(
        self,
        *,
        older_than_hours: int = 24,
    ) -> int:
        """
        Delete speed history samples older than the specified age.

        Returns:
            Number of samples deleted.
        """
        db = self._ensure_open()
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(older_than_hours)))

        async with self._lock:
            cursor = await db.execute(
                "DELETE FROM speed_history WHERE timestamp < ?",
                (cutoff.isoformat(),),
            )
            await db.commit()

        count = cursor.rowcount
        if count > 0:
            log.debug("Pruned %d old speed history samples", count)
        return count

    # ══════════════════════════════════════════════════════════════════════
    #  DAILY STATISTICS
    # ══════════════════════════════════════════════════════════════════════

    async def update_daily_stats(
        self,
        bytes_downloaded: int,
        downloads_completed: int = 0,
        average_speed: float = 0.0,
    ) -> None:
        """
        Add to today's statistics.

        Uses INSERT … ON CONFLICT to upsert the daily record.

        Args:
            bytes_downloaded: Bytes to add to today's total.
            downloads_completed: Number of completed downloads to add.
            average_speed: Average speed to record (replaces previous value).
        """
        db = self._ensure_open()
        today = date.today().isoformat()

        async with self._lock:
            await db.execute(
                """
                INSERT INTO daily_stats (date, total_bytes, total_downloads, average_speed)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_bytes = total_bytes + excluded.total_bytes,
                    total_downloads = total_downloads + excluded.total_downloads,
                    average_speed = CASE
                        WHEN excluded.average_speed > 0
                        THEN excluded.average_speed
                        ELSE average_speed
                    END
                """,
                (today, bytes_downloaded, downloads_completed, average_speed),
            )
            await db.commit()

    async def get_daily_stats(
        self,
        *,
        days: int = 30,
    ) -> list[DailyStats]:
        """
        Fetch daily statistics for the last N days.

        Args:
            days: Number of days to look back.

        Returns:
            List of ``DailyStats`` objects, most recent first.
        """
        db = self._ensure_open()
        cursor = await db.execute(
            """
            SELECT * FROM daily_stats
            ORDER BY date DESC
            LIMIT ?
            """,
            (days,),
        )
        rows = await cursor.fetchall()
        return [
            DailyStats(
                date=row["date"],
                total_bytes=row["total_bytes"],
                total_downloads=row["total_downloads"],
                average_speed=row["average_speed"],
            )
            for row in rows
        ]

    async def get_total_statistics(self) -> dict[str, Any]:
        """
        Get all-time aggregate statistics.

        Returns:
            Dict with keys: total_bytes, total_downloads, total_days, avg_daily_bytes.
        """
        db = self._ensure_open()
        cursor = await db.execute(
            """
            SELECT
                COALESCE(SUM(total_bytes), 0) as total_bytes,
                COALESCE(SUM(total_downloads), 0) as total_downloads,
                COUNT(*) as total_days
            FROM daily_stats
            """
        )
        row = await cursor.fetchone()

        if row is None:
            return {
                "total_bytes": 0,
                "total_downloads": 0,
                "total_days": 0,
                "avg_daily_bytes": 0.0,
            }

        total_bytes = row["total_bytes"]
        total_days = row["total_days"]

        return {
            "total_bytes": total_bytes,
            "total_downloads": row["total_downloads"],
            "total_days": total_days,
            "avg_daily_bytes": total_bytes / max(1, total_days),
        }

    async def prune_old_stats(self, retention_days: int = 90) -> int:
        """
        Delete daily stats older than ``retention_days``.

        Returns:
            Number of records deleted.
        """
        db = self._ensure_open()
        from datetime import timedelta
        cutoff = (date.today() - timedelta(days=retention_days)).isoformat()

        async with self._lock:
            cursor = await db.execute(
                "DELETE FROM daily_stats WHERE date < ?", (cutoff,)
            )
            await db.commit()

        count = cursor.rowcount
        if count > 0:
            log.info("Pruned %d old daily stat records", count)
        return count

    # ══════════════════════════════════════════════════════════════════════
    #  HISTORY & SEARCH
    # ══════════════════════════════════════════════════════════════════════

    async def search_history(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DownloadRecord]:
        """
        Search download history by filename or URL.

        Uses LIKE matching with case-insensitive comparison.

        Args:
            query: Search term (matched against filename and URL).
            limit: Maximum results.
            offset: Pagination offset.

        Returns:
            List of matching ``DownloadRecord`` objects.
        """
        return await self.get_all_downloads(
            search_query=query,
            order_by="date_added",
            order_desc=True,
            limit=limit,
            offset=offset,
        )

    async def get_history_by_date_range(
        self,
        start_date: str,
        end_date: str,
    ) -> list[DownloadRecord]:
        """
        Fetch downloads within a date range.

        Args:
            start_date: ISO date string (inclusive).
            end_date: ISO date string (inclusive).

        Returns:
            List of ``DownloadRecord`` objects within the range.
        """
        db = self._ensure_open()
        cursor = await db.execute(
            """
            SELECT * FROM downloads
            WHERE date_added >= ? AND date_added <= ?
            ORDER BY date_added DESC
            """,
            (start_date, end_date + "T23:59:59"),
        )
        rows = await cursor.fetchall()
        return [_row_to_download(row) for row in rows]

    async def get_category_summary(self) -> dict[str, dict[str, int]]:
        """
        Get a summary of downloads grouped by category.

        Returns:
            Dict mapping category name to {count, total_bytes}.
        """
        db = self._ensure_open()
        cursor = await db.execute(
            """
            SELECT
                category,
                COUNT(*) as count,
                COALESCE(SUM(CASE WHEN file_size > 0 THEN file_size ELSE 0 END), 0) as total_bytes
            FROM downloads
            GROUP BY category
            ORDER BY count DESC
            """
        )
        rows = await cursor.fetchall()
        return {
            row["category"]: {
                "count": row["count"],
                "total_bytes": row["total_bytes"],
            }
            for row in rows
        }

    # ══════════════════════════════════════════════════════════════════════
    #  MAINTENANCE
    # ══════════════════════════════════════════════════════════════════════

    async def vacuum(self) -> None:
        """
        Reclaim disk space by running SQLite VACUUM.

        This should be called periodically (e.g. weekly) or after
        large batch deletions.
        """
        db = self._ensure_open()
        log.info("Running VACUUM on database…")
        await db.execute("VACUUM")
        log.info("VACUUM complete")

    async def get_db_size(self) -> int:
        """Return the database file size in bytes."""
        if self._db_path.exists():
            return self._db_path.stat().st_size
        return 0

    async def integrity_check(self) -> bool:
        """
        Run SQLite integrity check.

        Returns:
            True if the database passes integrity checks.
        """
        db = self._ensure_open()
        cursor = await db.execute("PRAGMA integrity_check")
        row = await cursor.fetchone()
        result = row[0] if row else "error"
        ok = result == "ok"
        if ok:
            log.info("Database integrity check: PASS")
        else:
            log.error("Database integrity check FAILED: %s", result)
        return ok


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  HELPER FUNCTIONS (module-private)                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_download(row: aiosqlite.Row) -> DownloadRecord:
    """Convert a database row to a ``DownloadRecord``."""
    return DownloadRecord(
        id=row["id"],
        url=row["url"],
        filename=row["filename"],
        save_path=row["save_path"],
        file_size=row["file_size"],
        downloaded_bytes=row["downloaded_bytes"],
        status=row["status"],
        priority=row["priority"],
        category=row["category"],
        chunks_count=row["chunks_count"],
        date_added=row["date_added"],
        date_completed=row["date_completed"],
        average_speed=row["average_speed"],
        hash_expected=row["hash_expected"],
        hash_actual=row["hash_actual"],
        hash_verified=bool(row["hash_verified"]),
        referer=row["referer"],
        cookies=_decrypt_sensitive(row["cookies"]),
        user_agent=row["user_agent"],
        proxy_config=_decrypt_sensitive(row["proxy_config"]),
        error_message=row["error_message"],
        retry_count=row["retry_count"],
        resume_supported=bool(row["resume_supported"]),
        metadata_json=_decrypt_sensitive(row["metadata_json"]),
    )


def _row_to_chunk(row: aiosqlite.Row) -> ChunkRecord:
    """Convert a database row to a ``ChunkRecord``."""
    return ChunkRecord(
        id=row["id"],
        download_id=row["download_id"],
        chunk_index=row["chunk_index"],
        start_byte=row["start_byte"],
        end_byte=row["end_byte"],
        downloaded_bytes=row["downloaded_bytes"],
        status=row["status"],
        temp_file=row["temp_file"],
        error_message=row["error_message"],
    )


def _row_to_speed_sample(row: aiosqlite.Row) -> SpeedSample:
    """Convert a database row to a ``SpeedSample``."""
    return SpeedSample(
        id=row["id"],
        download_id=row["download_id"],
        timestamp=row["timestamp"],
        speed_bytes_per_sec=row["speed_bytes_per_sec"],
        downloaded_bytes=row["downloaded_bytes"],
    )


def _to_fts_query(value: str) -> str:
    """Convert free-text query into conservative FTS5 MATCH expression."""
    tokens = [t for t in re.split(r"\s+", str(value or "").strip()) if t]
    if not tokens:
        return "*"
    escaped = [f'"{t.replace("\"", "")}"*' for t in tokens]
    return " AND ".join(escaped)


def _encrypt_sensitive(value: Optional[str]) -> Optional[str]:
    """Encrypt sensitive DB fields before persistence."""
    raw = str(value or "")
    if not raw:
        return None
    if raw.startswith("enc:v1:"):
        return raw

    token = encrypt_secret(raw)
    if not token:
        log.warning("Falling back to redacted sentinel for sensitive field encryption failure")
        return "enc:v1:UNAVAILABLE"
    return f"enc:v1:{token}"


def _decrypt_sensitive(value: Optional[str]) -> Optional[str]:
    """Decrypt sensitive DB fields when reading records."""
    raw = str(value or "")
    if not raw:
        return None
    if not raw.startswith("enc:v1:"):
        return raw
    token = raw[len("enc:v1:"):]
    if token == "UNAVAILABLE":
        return None
    return decrypt_secret(token)
