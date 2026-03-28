"""Concurrency stress tests for queue scheduling behavior."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.downloader import DownloadEngine
from core.storage import DownloadStatus, StorageManager


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncGenerator[StorageManager, None]:
    mgr = StorageManager(tmp_path / "stress.db")
    await mgr.initialize()
    yield mgr
    await mgr.close()


@pytest.fixture
def sample_config(tmp_path: Path) -> dict:
    return {
        "general": {
            "download_directory": str(tmp_path / "downloads"),
            "max_concurrent_downloads": 3,
            "default_chunks": 4,
        },
        "network": {
            "max_retries": 1,
            "retry_base_delay_seconds": 0.05,
            "retry_max_delay_seconds": 0.2,
        },
        "advanced": {
            "dynamic_chunk_adjustment": True,
            "min_chunk_size_bytes": 1024,
            "max_chunk_size_bytes": 1_048_576,
            "chunk_buffer_size_bytes": 4096,
            "chunk_prefetch_buffers": 2,
            "speed_sample_interval_ms": 100,
        },
    }


@pytest.fixture
def mock_network() -> MagicMock:
    net = MagicMock()
    net.retry_policy = MagicMock()
    net.retry_policy.max_retries = 1
    net.session = MagicMock()
    net.proxy_url = None
    net.global_limiter = MagicMock()
    net.global_limiter.is_unlimited = True
    net.create_download_limiter = MagicMock()
    net.remove_download_limiter = MagicMock()
    net.throttle = AsyncMock()
    return net


@pytest.mark.asyncio
async def test_queue_processor_respects_max_concurrency_under_load(
    storage: StorageManager,
    sample_config: dict,
    mock_network: MagicMock,
    tmp_path: Path,
) -> None:
    engine = DownloadEngine(
        storage,
        mock_network,
        sample_config,
        chunks_dir=tmp_path / "chunks",
    )

    # Create a burst of queued downloads.
    ids: list[str] = []
    for i in range(12):
        dl_id = await engine.add_download(
            f"https://example.com/file-{i}.bin",
            filename=f"file-{i}.bin",
            category="Other",
        )
        ids.append(dl_id)

    max_seen = 0

    class _DummyTask:
        def __init__(self) -> None:
            self.is_active = True

        async def pause(self) -> None:
            self.is_active = False

        async def cancel(self, *, mark_cancelled: bool = True) -> None:
            self.is_active = False

    async def fake_start_task(download_id: str) -> None:
        nonlocal max_seen
        task = _DummyTask()
        engine._active_tasks[download_id] = task
        max_seen = max(max_seen, len(engine._active_tasks))
        await storage.update_download_status(download_id, DownloadStatus.DOWNLOADING)

        async def complete_soon() -> None:
            await asyncio.sleep(0.05)
            task.is_active = False
            await storage.update_download_status(download_id, DownloadStatus.COMPLETED)

        asyncio.create_task(complete_soon())

    engine._start_task = fake_start_task  # type: ignore[method-assign]

    await engine.start()
    await asyncio.sleep(5.0)
    await engine.stop()

    assert max_seen <= sample_config["general"]["max_concurrent_downloads"]

    completed = await storage.get_all_downloads(status=DownloadStatus.COMPLETED)
    completed_ids = {r.id for r in completed}
    assert set(ids).issubset(completed_ids)


@pytest.mark.asyncio
async def test_speed_telemetry_is_batched_under_concurrency(
    storage: StorageManager,
) -> None:
    ids = [
        await storage.add_download(
            url=f"https://example.com/speed-{idx}.bin",
            filename=f"speed-{idx}.bin",
            save_path=f"/tmp/speed-{idx}.bin",
        )
        for idx in range(4)
    ]

    db = storage._ensure_open()  # type: ignore[attr-defined]
    commit_calls = 0
    original_commit = db.commit

    async def counted_commit() -> None:
        nonlocal commit_calls
        commit_calls += 1
        await original_commit()

    db.commit = counted_commit  # type: ignore[method-assign]

    per_download_samples = 80
    await asyncio.gather(*[
        _emit_speed_samples(storage, dl_id, per_download_samples)
        for dl_id in ids
    ])

    await storage.flush_speed_samples(force=True)

    total_samples = len(ids) * per_download_samples
    # Batch flush should keep commit count significantly below sample count.
    assert commit_calls < total_samples / 4


async def _emit_speed_samples(storage: StorageManager, download_id: str, count: int) -> None:
    downloaded = 0
    for i in range(count):
        downloaded += 4096
        await storage.add_speed_sample(download_id, float(1000 + i), downloaded)
