"""
Unit tests for core/storage.py — the async SQLite storage layer.

Tests cover:
    • Database initialization and schema creation
    • Download CRUD (create, read, update, delete)
    • Priority-ordered queue retrieval
    • Chunk management (add, update progress, resume)
    • Speed history recording and retrieval
    • Daily statistics aggregation
    • Search and filtering
    • Database maintenance (integrity check, vacuum)
    • Edge cases (missing records, corrupt data, concurrent access)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.storage import (
    StorageManager,
    DownloadRecord,
    DownloadStatus,
    DownloadPriority,
    ChunkRecord,
    ChunkStatus,
    SpeedSample,
    DailyStats,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
async def storage(tmp_path: Path) -> StorageManager:
    """Create and initialize an in-memory-like storage instance."""
    db_path = tmp_path / "test_downloads.db"
    mgr = StorageManager(db_path)
    await mgr.initialize()
    yield mgr
    await mgr.close()


@pytest.fixture
async def populated_storage(storage: StorageManager) -> StorageManager:
    """Storage instance pre-populated with sample downloads."""
    await storage.add_download(
        url="https://example.com/video.mp4",
        filename="video.mp4",
        save_path="/downloads/Video/video.mp4",
        file_size=104857600,  # 100 MB
        priority=DownloadPriority.HIGH,
        category="Video",
        resume_supported=True,
    )
    await storage.add_download(
        url="https://example.com/document.pdf",
        filename="document.pdf",
        save_path="/downloads/Document/document.pdf",
        file_size=5242880,    # 5 MB
        priority=DownloadPriority.NORMAL,
        category="Document",
    )
    await storage.add_download(
        url="https://example.com/image.png",
        filename="image.png",
        save_path="/downloads/Image/image.png",
        file_size=1048576,    # 1 MB
        priority=DownloadPriority.LOW,
        category="Image",
    )
    return storage


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

class TestDatabaseLifecycle:
    """Tests for database initialization and connection management."""

    @pytest.mark.asyncio
    async def test_initialize_creates_db_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "new" / "sub" / "test.db"
        mgr = StorageManager(db_path)
        await mgr.initialize()

        assert db_path.exists()
        assert mgr.is_open

        await mgr.close()
        assert not mgr.is_open

    @pytest.mark.asyncio
    async def test_double_initialize_is_safe(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        mgr = StorageManager(db_path)
        await mgr.initialize()
        await mgr.initialize()  # should not raise

        assert mgr.is_open
        await mgr.close()

    @pytest.mark.asyncio
    async def test_operations_fail_when_closed(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        mgr = StorageManager(db_path)
        # Never initialized
        with pytest.raises(RuntimeError, match="not open"):
            await mgr.add_download(
                url="http://x.com/f", filename="f", save_path="/f"
            )

    @pytest.mark.asyncio
    async def test_integrity_check(self, storage: StorageManager) -> None:
        result = await storage.integrity_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_get_db_size(self, storage: StorageManager) -> None:
        size = await storage.get_db_size()
        assert size > 0  # Schema alone takes space


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestDownloadCRUD:
    """Tests for download record management."""

    @pytest.mark.asyncio
    async def test_add_download_returns_uuid(self, storage: StorageManager) -> None:
        dl_id = await storage.add_download(
            url="https://example.com/file.zip",
            filename="file.zip",
            save_path="/downloads/file.zip",
        )
        assert isinstance(dl_id, str)
        assert len(dl_id) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_get_download(self, storage: StorageManager) -> None:
        dl_id = await storage.add_download(
            url="https://example.com/file.zip",
            filename="file.zip",
            save_path="/downloads/file.zip",
            file_size=1024,
            priority=DownloadPriority.HIGH,
            category="Archive",
            hash_expected="abc123",
            referer="https://example.com",
            resume_supported=True,
        )

        record = await storage.get_download(dl_id)
        assert record is not None
        assert record.id == dl_id
        assert record.url == "https://example.com/file.zip"
        assert record.filename == "file.zip"
        assert record.file_size == 1024
        assert record.status == DownloadStatus.QUEUED.value
        assert record.priority == DownloadPriority.HIGH.value
        assert record.category == "Archive"
        assert record.hash_expected == "abc123"
        assert record.referer == "https://example.com"
        assert record.resume_supported is True
        assert record.downloaded_bytes == 0

    @pytest.mark.asyncio
    async def test_get_download_not_found(self, storage: StorageManager) -> None:
        record = await storage.get_download("nonexistent-id")
        assert record is None

    @pytest.mark.asyncio
    async def test_get_all_downloads(
        self, populated_storage: StorageManager
    ) -> None:
        downloads = await populated_storage.get_all_downloads()
        assert len(downloads) == 3

    @pytest.mark.asyncio
    async def test_filter_by_status(
        self, populated_storage: StorageManager
    ) -> None:
        # All are queued
        queued = await populated_storage.get_all_downloads(
            status=DownloadStatus.QUEUED
        )
        assert len(queued) == 3

        completed = await populated_storage.get_all_downloads(
            status=DownloadStatus.COMPLETED
        )
        assert len(completed) == 0

    @pytest.mark.asyncio
    async def test_filter_by_category(
        self, populated_storage: StorageManager
    ) -> None:
        videos = await populated_storage.get_all_downloads(category="Video")
        assert len(videos) == 1
        assert videos[0].filename == "video.mp4"

    @pytest.mark.asyncio
    async def test_filter_by_priority(
        self, populated_storage: StorageManager
    ) -> None:
        high = await populated_storage.get_all_downloads(
            priority=DownloadPriority.HIGH
        )
        assert len(high) == 1
        assert high[0].filename == "video.mp4"

    @pytest.mark.asyncio
    async def test_search_query(
        self, populated_storage: StorageManager
    ) -> None:
        results = await populated_storage.get_all_downloads(
            search_query="video"
        )
        assert len(results) == 1
        assert results[0].filename == "video.mp4"

        # Search in URL
        results = await populated_storage.get_all_downloads(
            search_query="example.com"
        )
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_pagination(
        self, populated_storage: StorageManager
    ) -> None:
        page1 = await populated_storage.get_all_downloads(limit=2, offset=0)
        assert len(page1) == 2

        page2 = await populated_storage.get_all_downloads(limit=2, offset=2)
        assert len(page2) == 1

    @pytest.mark.asyncio
    async def test_delete_download(self, storage: StorageManager) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f"
        )
        deleted = await storage.delete_download(dl_id)
        assert deleted is True

        record = await storage.get_download(dl_id)
        assert record is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, storage: StorageManager) -> None:
        deleted = await storage.delete_download("nonexistent")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_delete_completed_downloads(
        self, populated_storage: StorageManager
    ) -> None:
        # Mark one as completed
        downloads = await populated_storage.get_all_downloads()
        await populated_storage.update_download_status(
            downloads[0].id, DownloadStatus.COMPLETED
        )

        count = await populated_storage.delete_completed_downloads()
        assert count == 1

        remaining = await populated_storage.get_all_downloads()
        assert len(remaining) == 2

    @pytest.mark.asyncio
    async def test_download_count(
        self, populated_storage: StorageManager
    ) -> None:
        total = await populated_storage.get_download_count()
        assert total == 3

        queued = await populated_storage.get_download_count(
            status=DownloadStatus.QUEUED
        )
        assert queued == 3


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD STATUS & PROGRESS
# ══════════════════════════════════════════════════════════════════════════════

class TestDownloadStatusProgress:
    """Tests for status transitions and progress tracking."""

    @pytest.mark.asyncio
    async def test_update_status(self, storage: StorageManager) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f"
        )

        await storage.update_download_status(dl_id, DownloadStatus.DOWNLOADING)
        record = await storage.get_download(dl_id)
        assert record.status == DownloadStatus.DOWNLOADING.value

    @pytest.mark.asyncio
    async def test_completed_sets_date(self, storage: StorageManager) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f"
        )

        await storage.update_download_status(dl_id, DownloadStatus.COMPLETED)
        record = await storage.get_download(dl_id)
        assert record.status == DownloadStatus.COMPLETED.value
        assert record.date_completed is not None

    @pytest.mark.asyncio
    async def test_failed_increments_retry(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f"
        )

        await storage.update_download_status(
            dl_id, DownloadStatus.FAILED, error_message="Connection timeout"
        )
        record = await storage.get_download(dl_id)
        assert record.status == DownloadStatus.FAILED.value
        assert record.retry_count == 1
        assert record.error_message == "Connection timeout"

        # Fail again
        await storage.update_download_status(
            dl_id, DownloadStatus.FAILED, error_message="DNS error"
        )
        record = await storage.get_download(dl_id)
        assert record.retry_count == 2
        assert record.error_message == "DNS error"

    @pytest.mark.asyncio
    async def test_update_progress(self, storage: StorageManager) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f",
            file_size=1000,
        )

        await storage.update_download_progress(
            dl_id, downloaded_bytes=500, average_speed=1024.5
        )
        record = await storage.get_download(dl_id)
        assert record.downloaded_bytes == 500
        assert record.average_speed == 1024.5
        assert record.progress_percent == 50.0

    @pytest.mark.asyncio
    async def test_update_arbitrary_fields(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f"
        )

        await storage.update_download_field(
            dl_id,
            filename="new_name.zip",
            category="Archive",
            hash_actual="sha256hash",
        )

        record = await storage.get_download(dl_id)
        assert record.filename == "new_name.zip"
        assert record.category == "Archive"
        assert record.hash_actual == "sha256hash"

    @pytest.mark.asyncio
    async def test_update_invalid_field_raises(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f"
        )
        with pytest.raises(ValueError, match="Invalid field"):
            await storage.update_download_field(dl_id, evil_field="drop table")

    @pytest.mark.asyncio
    async def test_update_no_fields_raises(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f"
        )
        with pytest.raises(ValueError, match="No fields"):
            await storage.update_download_field(dl_id)


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD RECORD PROPERTIES
# ══════════════════════════════════════════════════════════════════════════════

class TestDownloadRecordProperties:
    """Tests for computed properties on DownloadRecord."""

    def test_progress_percent_normal(self) -> None:
        rec = DownloadRecord(
            id="x", url="x", filename="x", save_path="x",
            file_size=1000, downloaded_bytes=250,
        )
        assert rec.progress_percent == 25.0

    def test_progress_percent_unknown_size(self) -> None:
        rec = DownloadRecord(
            id="x", url="x", filename="x", save_path="x",
            file_size=-1, downloaded_bytes=250,
        )
        assert rec.progress_percent == 0.0

    def test_progress_percent_complete(self) -> None:
        rec = DownloadRecord(
            id="x", url="x", filename="x", save_path="x",
            file_size=1000, downloaded_bytes=1000,
        )
        assert rec.progress_percent == 100.0

    def test_is_active(self) -> None:
        for status in ["downloading", "merging", "verifying"]:
            rec = DownloadRecord(
                id="x", url="x", filename="x", save_path="x", status=status
            )
            assert rec.is_active is True

        for status in ["queued", "paused", "completed", "failed"]:
            rec = DownloadRecord(
                id="x", url="x", filename="x", save_path="x", status=status
            )
            assert rec.is_active is False

    def test_is_resumable(self) -> None:
        rec = DownloadRecord(
            id="x", url="x", filename="x", save_path="x",
            status="paused", resume_supported=True,
        )
        assert rec.is_resumable is True

        rec2 = DownloadRecord(
            id="x", url="x", filename="x", save_path="x",
            status="paused", resume_supported=False,
        )
        assert rec2.is_resumable is False


# ══════════════════════════════════════════════════════════════════════════════
#  QUEUE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

class TestQueueManagement:
    """Tests for priority-ordered queue retrieval."""

    @pytest.mark.asyncio
    async def test_queued_downloads_ordered_by_priority(
        self, populated_storage: StorageManager
    ) -> None:
        queued = await populated_storage.get_queued_downloads()
        assert len(queued) == 3
        # High comes first, then Normal, then Low
        assert queued[0].priority == DownloadPriority.HIGH.value
        assert queued[1].priority == DownloadPriority.NORMAL.value
        assert queued[2].priority == DownloadPriority.LOW.value

    @pytest.mark.asyncio
    async def test_active_downloads(
        self, populated_storage: StorageManager
    ) -> None:
        # Initially none are active
        active = await populated_storage.get_active_downloads()
        assert len(active) == 0

        # Start one
        downloads = await populated_storage.get_all_downloads()
        await populated_storage.update_download_status(
            downloads[0].id, DownloadStatus.DOWNLOADING
        )

        active = await populated_storage.get_active_downloads()
        assert len(active) == 1


# ══════════════════════════════════════════════════════════════════════════════
#  CHUNKS
# ══════════════════════════════════════════════════════════════════════════════

class TestChunkManagement:
    """Tests for chunk (byte-range segment) management."""

    @pytest.mark.asyncio
    async def test_add_and_get_chunks(self, storage: StorageManager) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f",
            file_size=1000,
        )

        chunks = [
            ChunkRecord(download_id=dl_id, chunk_index=0,
                        start_byte=0, end_byte=249, temp_file="/tmp/c0"),
            ChunkRecord(download_id=dl_id, chunk_index=1,
                        start_byte=250, end_byte=499, temp_file="/tmp/c1"),
            ChunkRecord(download_id=dl_id, chunk_index=2,
                        start_byte=500, end_byte=749, temp_file="/tmp/c2"),
            ChunkRecord(download_id=dl_id, chunk_index=3,
                        start_byte=750, end_byte=999, temp_file="/tmp/c3"),
        ]
        await storage.add_chunks(dl_id, chunks)

        # Verify
        saved = await storage.get_chunks(dl_id)
        assert len(saved) == 4
        assert saved[0].start_byte == 0
        assert saved[0].end_byte == 249
        assert saved[3].start_byte == 750
        assert saved[3].end_byte == 999

        # Verify chunks_count updated on download
        record = await storage.get_download(dl_id)
        assert record.chunks_count == 4

    @pytest.mark.asyncio
    async def test_chunk_progress_update(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f",
            file_size=1000,
        )
        chunks = [
            ChunkRecord(download_id=dl_id, chunk_index=0,
                        start_byte=0, end_byte=499, temp_file="/tmp/c0"),
            ChunkRecord(download_id=dl_id, chunk_index=1,
                        start_byte=500, end_byte=999, temp_file="/tmp/c1"),
        ]
        await storage.add_chunks(dl_id, chunks)

        # Update progress for chunk 0
        await storage.update_chunk_progress(dl_id, 0, 250)
        await storage.flush_chunk_progress()

        saved = await storage.get_chunks(dl_id)
        assert saved[0].downloaded_bytes == 250
        assert saved[0].progress_percent == 50.0
        assert saved[0].resume_offset == 250

    @pytest.mark.asyncio
    async def test_chunk_status_update(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f",
            file_size=500,
        )
        chunks = [
            ChunkRecord(download_id=dl_id, chunk_index=0,
                        start_byte=0, end_byte=499, temp_file="/tmp/c0"),
        ]
        await storage.add_chunks(dl_id, chunks)

        await storage.update_chunk_status(
            dl_id, 0, ChunkStatus.COMPLETED
        )

        saved = await storage.get_chunks(dl_id)
        assert saved[0].status == ChunkStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_get_incomplete_chunks(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f",
            file_size=1000,
        )
        chunks = [
            ChunkRecord(download_id=dl_id, chunk_index=0,
                        start_byte=0, end_byte=499, temp_file="/tmp/c0"),
            ChunkRecord(download_id=dl_id, chunk_index=1,
                        start_byte=500, end_byte=999, temp_file="/tmp/c1"),
        ]
        await storage.add_chunks(dl_id, chunks)

        # Mark one as completed
        await storage.update_chunk_status(dl_id, 0, ChunkStatus.COMPLETED)

        incomplete = await storage.get_incomplete_chunks(dl_id)
        assert len(incomplete) == 1
        assert incomplete[0].chunk_index == 1

    @pytest.mark.asyncio
    async def test_chunk_record_properties(self) -> None:
        chunk = ChunkRecord(
            download_id="x", chunk_index=0,
            start_byte=100, end_byte=299,
            downloaded_bytes=50,
        )
        assert chunk.total_bytes == 200
        assert chunk.remaining_bytes == 150
        assert chunk.progress_percent == 25.0
        assert chunk.resume_offset == 150  # start_byte + downloaded_bytes

    @pytest.mark.asyncio
    async def test_re_chunking_replaces_old_chunks(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f",
            file_size=1000,
        )

        # First chunking: 2 chunks
        chunks_v1 = [
            ChunkRecord(download_id=dl_id, chunk_index=0,
                        start_byte=0, end_byte=499, temp_file="/tmp/c0"),
            ChunkRecord(download_id=dl_id, chunk_index=1,
                        start_byte=500, end_byte=999, temp_file="/tmp/c1"),
        ]
        await storage.add_chunks(dl_id, chunks_v1)

        # Re-chunk: 4 chunks
        chunks_v2 = [
            ChunkRecord(download_id=dl_id, chunk_index=i,
                        start_byte=i*250, end_byte=(i+1)*250-1,
                        temp_file=f"/tmp/c{i}")
            for i in range(4)
        ]
        await storage.add_chunks(dl_id, chunks_v2)

        saved = await storage.get_chunks(dl_id)
        assert len(saved) == 4

        record = await storage.get_download(dl_id)
        assert record.chunks_count == 4


# ══════════════════════════════════════════════════════════════════════════════
#  SPEED HISTORY
# ══════════════════════════════════════════════════════════════════════════════

class TestSpeedHistory:
    """Tests for speed measurement recording and retrieval."""

    @pytest.mark.asyncio
    async def test_add_and_get_speed_samples(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f"
        )

        await storage.add_speed_sample(dl_id, 1024.0, 1024)
        await storage.add_speed_sample(dl_id, 2048.0, 3072)
        await storage.add_speed_sample(dl_id, 4096.0, 7168)

        history = await storage.get_speed_history(dl_id, limit=10)
        assert len(history) == 3
        # Should be chronological (oldest first)
        assert history[0].speed_bytes_per_sec == 1024.0
        assert history[2].speed_bytes_per_sec == 4096.0

    @pytest.mark.asyncio
    async def test_speed_history_limit(
        self, storage: StorageManager
    ) -> None:
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f"
        )

        for i in range(20):
            await storage.add_speed_sample(dl_id, float(i * 100), i * 100)

        history = await storage.get_speed_history(dl_id, limit=5)
        assert len(history) == 5


# ══════════════════════════════════════════════════════════════════════════════
#  DAILY STATISTICS
# ══════════════════════════════════════════════════════════════════════════════

class TestDailyStats:
    """Tests for daily statistics aggregation."""

    @pytest.mark.asyncio
    async def test_update_and_get_daily_stats(
        self, storage: StorageManager
    ) -> None:
        await storage.update_daily_stats(
            bytes_downloaded=1_000_000,
            downloads_completed=2,
            average_speed=500_000.0,
        )

        stats = await storage.get_daily_stats(days=7)
        assert len(stats) == 1
        assert stats[0].total_bytes == 1_000_000
        assert stats[0].total_downloads == 2

    @pytest.mark.asyncio
    async def test_daily_stats_accumulate(
        self, storage: StorageManager
    ) -> None:
        # First batch
        await storage.update_daily_stats(bytes_downloaded=1000, downloads_completed=1)
        # Second batch (same day)
        await storage.update_daily_stats(bytes_downloaded=2000, downloads_completed=3)

        stats = await storage.get_daily_stats(days=1)
        assert len(stats) == 1
        assert stats[0].total_bytes == 3000       # accumulated
        assert stats[0].total_downloads == 4       # accumulated

    @pytest.mark.asyncio
    async def test_total_statistics(
        self, storage: StorageManager
    ) -> None:
        await storage.update_daily_stats(bytes_downloaded=5000, downloads_completed=10)

        totals = await storage.get_total_statistics()
        assert totals["total_bytes"] == 5000
        assert totals["total_downloads"] == 10
        assert totals["total_days"] == 1
        assert totals["avg_daily_bytes"] == 5000.0


# ══════════════════════════════════════════════════════════════════════════════
#  HISTORY & SEARCH
# ══════════════════════════════════════════════════════════════════════════════

class TestHistorySearch:
    """Tests for download history search functionality."""

    @pytest.mark.asyncio
    async def test_search_history(
        self, populated_storage: StorageManager
    ) -> None:
        results = await populated_storage.search_history("video")
        assert len(results) == 1
        assert results[0].filename == "video.mp4"

    @pytest.mark.asyncio
    async def test_search_empty_query(
        self, populated_storage: StorageManager
    ) -> None:
        results = await populated_storage.search_history("")
        assert len(results) == 3  # no filter = all results

    @pytest.mark.asyncio
    async def test_category_summary(
        self, populated_storage: StorageManager
    ) -> None:
        summary = await populated_storage.get_category_summary()
        assert "Video" in summary
        assert summary["Video"]["count"] == 1
        assert summary["Video"]["total_bytes"] == 104857600


# ══════════════════════════════════════════════════════════════════════════════
#  MAINTENANCE
# ══════════════════════════════════════════════════════════════════════════════

class TestMaintenance:
    """Tests for database maintenance operations."""

    @pytest.mark.asyncio
    async def test_vacuum(self, storage: StorageManager) -> None:
        # Just verify it doesn't crash
        await storage.vacuum()

    @pytest.mark.asyncio
    async def test_cascade_delete(self, storage: StorageManager) -> None:
        """Deleting a download should also delete its chunks."""
        dl_id = await storage.add_download(
            url="http://x.com/f", filename="f", save_path="/f",
            file_size=1000,
        )
        chunks = [
            ChunkRecord(download_id=dl_id, chunk_index=0,
                        start_byte=0, end_byte=999, temp_file="/tmp/c0"),
        ]
        await storage.add_chunks(dl_id, chunks)
        await storage.add_speed_sample(dl_id, 1024.0, 512)

        # Delete the download
        await storage.delete_download(dl_id)

        # Chunks should be gone too
        remaining = await storage.get_chunks(dl_id)
        assert len(remaining) == 0
