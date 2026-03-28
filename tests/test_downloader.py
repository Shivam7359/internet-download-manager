"""
Unit tests for core/downloader.py — chunk download engine.

Tests cover:
    • SpeedTracker — rolling average calculation
    • calculate_chunks — byte-range splitting
    • dynamic_chunk_count — heuristic chunk sizing
    • DownloadTask — lifecycle and state
    • DownloadEngine — queue management, add/pause/cancel
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.downloader import (
    SpeedTracker,
    calculate_chunks,
    dynamic_chunk_count,
    should_use_parallel_chunks,
    DownloadEngine,
    DownloadTask,
    FirstByteTimeoutError,
    NullCallbacks,
    BUFFER_SIZE,
)
from core.storage import (
    StorageManager,
    DownloadStatus,
    DownloadPriority,
    ChunkRecord,
)


# ══════════════════════════════════════════════════════════════════════════════
#  SPEED TRACKER
# ══════════════════════════════════════════════════════════════════════════════

class TestSpeedTracker:
    """Tests for rolling-average speed tracking."""

    def test_initial_speed_is_zero(self) -> None:
        tracker = SpeedTracker()
        assert tracker.speed == 0.0

    def test_single_sample_is_zero(self) -> None:
        tracker = SpeedTracker()
        tracker.record(1000)
        assert tracker.speed == 0.0  # need at least 2 samples

    def test_speed_calculation(self) -> None:
        tracker = SpeedTracker(window_size=10)
        now = time.monotonic()
        tracker._samples.append((now - 1.0, 0))
        tracker._samples.append((now, 1000))
        assert tracker.speed == pytest.approx(1000.0)

    def test_average_speed(self) -> None:
        tracker = SpeedTracker()
        tracker._start_time = time.monotonic() - 10.0  # 10 seconds ago
        tracker._total_bytes = 10000
        assert tracker.average_speed == pytest.approx(1000.0, rel=0.1)

    def test_reset(self) -> None:
        tracker = SpeedTracker()
        tracker.record(1000)
        tracker.record(2000)
        tracker.reset()
        assert tracker.speed == 0.0
        assert len(tracker._samples) == 0

    def test_speed_becomes_zero_when_samples_are_stale(self) -> None:
        tracker = SpeedTracker(stale_after_seconds=1.0)
        now = time.monotonic()
        tracker._samples.append((now - 5.0, 0))
        tracker._samples.append((now - 2.0, 2000))
        assert tracker.speed == 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  CHUNK CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateChunks:
    """Tests for file-to-chunks splitting."""

    def test_simple_split(self) -> None:
        chunks = calculate_chunks(1000, num_chunks=4, min_chunk_size=1)
        assert len(chunks) == 4
        assert chunks[0][0] == 0
        assert chunks[-1][1] == 999  # last byte is inclusive

    def test_single_chunk_small_file(self) -> None:
        chunks = calculate_chunks(100, num_chunks=8, min_chunk_size=256)
        assert len(chunks) == 1
        assert chunks[0] == (0, 99)

    def test_full_coverage(self) -> None:
        """All bytes must be covered with no gaps."""
        file_size = 10_000_000
        chunks = calculate_chunks(file_size, num_chunks=8)

        # Verify no gaps
        for i in range(len(chunks) - 1):
            assert chunks[i][1] + 1 == chunks[i + 1][0], \
                f"Gap between chunk {i} and {i + 1}"

        # Verify full coverage
        assert chunks[0][0] == 0
        assert chunks[-1][1] == file_size - 1

    def test_zero_file_size(self) -> None:
        chunks = calculate_chunks(0)
        assert len(chunks) == 1
        assert chunks[0] == (0, 0)

    def test_exact_division(self) -> None:
        chunks = calculate_chunks(1000, num_chunks=5, min_chunk_size=1)
        assert len(chunks) == 5
        # Each chunk should be 200 bytes (except last gets remainder)

    def test_respects_max_chunk_size(self) -> None:
        # 100 MB file with max chunk of 10 MB → at least 10 chunks
        chunks = calculate_chunks(
            100_000_000, num_chunks=2,
            max_chunk_size=10_000_000,
        )
        assert len(chunks) >= 10

    def test_last_chunk_absorbs_remainder(self) -> None:
        chunks = calculate_chunks(1001, num_chunks=4, min_chunk_size=1)
        total = sum(end - start + 1 for start, end in chunks)
        assert total == 1001


class TestDynamicChunkCount:
    """Tests for heuristic chunk count calculation."""

    def test_tiny_file(self) -> None:
        assert dynamic_chunk_count(500_000) == 3       # < 1 MB

    def test_small_file(self) -> None:
        assert dynamic_chunk_count(5_000_000) == 3     # 5 MB

    def test_medium_file(self) -> None:
        result = dynamic_chunk_count(50_000_000)       # 50 MB
        assert 3 <= result <= 5

    def test_large_file(self) -> None:
        assert dynamic_chunk_count(500_000_000) == 4  # 500 MB

    def test_huge_file(self) -> None:
        assert dynamic_chunk_count(2_000_000_000) == 4  # ~2 GB

    def test_unknown_size(self) -> None:
        assert dynamic_chunk_count(-1) == 3
        assert dynamic_chunk_count(0) == 3


class TestParallelChunkThreshold:
    """Tests for minimum-size gating of parallel chunk downloads."""

    def test_small_file_uses_single_chunk(self) -> None:
        assert not should_use_parallel_chunks(
            file_size=5_000_000,
            resume_supported=True,
            min_parallel_size=100 * 1024 * 1024,
        )

    def test_large_file_uses_parallel_chunks(self) -> None:
        assert should_use_parallel_chunks(
            file_size=150 * 1024 * 1024,
            resume_supported=True,
            min_parallel_size=100 * 1024 * 1024,
        )

    def test_non_resumable_forces_single_chunk(self) -> None:
        assert not should_use_parallel_chunks(
            file_size=500 * 1024 * 1024,
            resume_supported=False,
            min_parallel_size=100 * 1024 * 1024,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  NULL CALLBACKS
# ══════════════════════════════════════════════════════════════════════════════

class TestNullCallbacks:
    """Verify NullCallbacks doesn't raise on any method."""

    def test_all_methods(self) -> None:
        cb = NullCallbacks()
        cb.on_progress("id", 0, 100, 50.0, 10.0)
        cb.on_status_changed("id", "downloading")
        cb.on_download_added("id", MagicMock())
        cb.on_download_complete("id")


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class TestDownloadEngine:
    """Tests for the DownloadEngine orchestrator."""

    @pytest.fixture
    def sample_config(self, tmp_path: Path) -> dict[str, Any]:
        return {
            "general": {
                "download_directory": str(tmp_path / "downloads"),
                "max_concurrent_downloads": 2,
                "default_chunks": 4,
            },
            "network": {
                "max_retries": 2,
                "retry_base_delay_seconds": 0.1,
                "retry_max_delay_seconds": 1.0,
            },
            "advanced": {
                "dynamic_chunk_adjustment": True,
                "min_chunk_size_bytes": 1024,
                "max_chunk_size_bytes": 1_048_576,
                "speed_sample_interval_ms": 100,
                "chunk_buffer_size_bytes": 4096,
            },
        }

    @pytest.fixture
    async def storage(self, tmp_path: Path) -> StorageManager:
        mgr = StorageManager(tmp_path / "test.db")
        await mgr.initialize()
        yield mgr
        await mgr.close()

    @pytest.fixture
    def mock_network(self) -> MagicMock:
        net = MagicMock()
        net.retry_policy = MagicMock()
        net.retry_policy.max_retries = 2
        net.session = MagicMock()
        net.proxy_url = None
        net.global_limiter = MagicMock()
        net.global_limiter.is_unlimited = True
        net.create_download_limiter = MagicMock()
        net.remove_download_limiter = MagicMock()
        net.throttle = AsyncMock()
        return net

    @pytest.mark.asyncio
    async def test_engine_start_stop(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )
        await engine.start()
        assert engine.is_running
        await engine.stop()
        assert not engine.is_running

    @pytest.mark.asyncio
    async def test_add_download(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )
        dl_id = await engine.add_download(
            "https://example.com/file.zip",
            filename="file.zip",
            category="Archive",
        )
        assert isinstance(dl_id, str)
        assert len(dl_id) == 36

        record = await storage.get_download(dl_id)
        assert record is not None
        assert record.filename == "file.zip"
        assert record.status == DownloadStatus.QUEUED.value

    @pytest.mark.asyncio
    async def test_add_download_truncates_overlong_filename_for_windows_path(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )
        long_name = "A" * 600
        dl_id = await engine.add_download(
            "https://example.com/file",
            filename=long_name,
            category="Other",
        )

        record = await storage.get_download(dl_id)
        assert record is not None
        assert len(record.filename) < len(long_name)
        assert len(record.save_path) <= 240
        assert record.status == DownloadStatus.QUEUED.value

    @pytest.mark.asyncio
    async def test_add_download_rejects_path_traversal_without_confirmation(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )

        with pytest.raises(ValueError, match="outside configured download roots"):
            await engine.add_download(
                "https://example.com/file.zip",
                filename="file.zip",
                save_path="../../etc/passwd",
                category="Other",
            )

    @pytest.mark.asyncio
    async def test_max_concurrent(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )
        assert engine.max_concurrent == 2
        engine.max_concurrent = 8
        assert engine.max_concurrent == 8
        engine.max_concurrent = 0  # clamps to 1
        assert engine.max_concurrent == 1

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )
        # Should not raise
        await engine.cancel("nonexistent-id")

    @pytest.mark.asyncio
    async def test_get_speeds_empty(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )
        assert engine.get_total_speed() == 0.0
        assert engine.get_active_speeds() == {}

    @pytest.mark.asyncio
    async def test_retry_failed_download(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )
        dl_id = await engine.add_download("https://example.com/f.zip")

        # Simulate failure
        await storage.update_download_status(dl_id, DownloadStatus.FAILED)

        # Retry should re-queue
        await engine.retry(dl_id)
        record = await storage.get_download(dl_id)
        assert record.status == DownloadStatus.QUEUED.value

    @pytest.mark.asyncio
    async def test_resume_paused_requeues(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )
        dl_id = await engine.add_download("https://example.com/f.zip")
        await storage.update_download_status(dl_id, DownloadStatus.PAUSED)

        await engine.resume(dl_id)
        record = await storage.get_download(dl_id)
        assert record.status == DownloadStatus.QUEUED.value

    @pytest.mark.asyncio
    async def test_recover_interrupted_downloads_requeues_active_statuses(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )

        dl_id = await engine.add_download("https://example.com/recover.zip")
        await storage.update_download_status(dl_id, DownloadStatus.DOWNLOADING)

        await engine._recover_interrupted_downloads()

        record = await storage.get_download(dl_id)
        assert record is not None
        assert record.status == DownloadStatus.QUEUED.value

    @pytest.mark.asyncio
    async def test_requeue_orphaned_active_downloads(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )

        dl_id = await engine.add_download("https://example.com/orphaned.bin")
        await storage.update_download_status(dl_id, DownloadStatus.DOWNLOADING)

        recovered = await engine._requeue_orphaned_active_downloads()
        assert recovered == 1

        record = await storage.get_download(dl_id)
        assert record is not None
        assert record.status == DownloadStatus.QUEUED.value

    @pytest.mark.asyncio
    async def test_stop_soft_cancels_as_paused(
        self, storage: StorageManager, mock_network: MagicMock,
        sample_config: dict, tmp_path: Path,
    ) -> None:
        engine = DownloadEngine(
            storage, mock_network, sample_config,
            chunks_dir=tmp_path / "chunks",
        )

        dl_id = await engine.add_download("https://example.com/soft-stop.zip")
        await storage.update_download_status(dl_id, DownloadStatus.DOWNLOADING)

        class DummyTask:
            def __init__(self) -> None:
                self.is_active = True
                self.called_with: Optional[bool] = None

            async def cancel(self, *, mark_cancelled: bool = True) -> None:
                self.called_with = mark_cancelled

        dummy = DummyTask()
        engine._active_tasks[dl_id] = dummy

        await engine.stop()

        record = await storage.get_download(dl_id)
        assert record is not None
        assert record.status == DownloadStatus.PAUSED.value
        assert dummy.called_with is False


class TestDownloadTaskFirstByteTimeout:
    @pytest.mark.asyncio
    async def test_stream_chunk_data_fails_fast_when_first_byte_stalls(self, tmp_path: Path) -> None:
        storage = MagicMock()
        network = MagicMock()
        network.throttle = AsyncMock()

        task = DownloadTask(
            download_id="dl-timeout",
            storage=storage,
            network=network,
            config={
                "advanced": {
                    "chunk_prefetch_buffers": 1,
                    "first_byte_timeout_seconds": 1,
                }
            },
            callbacks=NullCallbacks(),
            chunks_dir=tmp_path / "chunks",
        )

        class _SlowContent:
            async def iter_chunked(self, _size: int):
                await asyncio.sleep(1.2)
                yield b"late-byte"

        class _Resp:
            content = _SlowContent()

        chunk = ChunkRecord(
            download_id="dl-timeout",
            chunk_index=0,
            start_byte=0,
            end_byte=100,
            temp_file=str(tmp_path / "chunk_0.part"),
        )

        file_handle = AsyncMock()

        with pytest.raises(FirstByteTimeoutError):
            await task._stream_chunk_data(_Resp(), file_handle, chunk)

    @pytest.mark.asyncio
    async def test_first_byte_guard_times_out_when_no_data_arrives(self, tmp_path: Path) -> None:
        task = DownloadTask(
            download_id="dl-guard-timeout",
            storage=MagicMock(),
            network=MagicMock(),
            config={"advanced": {"first_byte_timeout_seconds": 1}},
            callbacks=NullCallbacks(),
            chunks_dir=tmp_path / "chunks",
        )

        with pytest.raises(FirstByteTimeoutError):
            await task._await_first_byte_or_timeout()

    @pytest.mark.asyncio
    async def test_first_byte_guard_ignores_paused_time(self, tmp_path: Path) -> None:
        task = DownloadTask(
            download_id="dl-guard-paused",
            storage=MagicMock(),
            network=MagicMock(),
            config={"advanced": {"first_byte_timeout_seconds": 1}},
            callbacks=NullCallbacks(),
            chunks_dir=tmp_path / "chunks",
        )

        async def _emit_byte_after_pause() -> None:
            task._paused.clear()
            await asyncio.sleep(1.3)
            task._paused.set()
            await asyncio.sleep(0.25)
            task._first_byte_event.set()

        producer = asyncio.create_task(_emit_byte_after_pause())
        await task._await_first_byte_or_timeout()
        await producer
