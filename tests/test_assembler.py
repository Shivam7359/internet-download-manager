"""
Unit tests for core/assembler.py — file assembly and hash verification.

Tests cover:
    • FileAssembler — merge chunks, filename conflict resolution, cancellation
    • HashVerifier — SHA-256 computation and verification
    • cleanup_temp_files — temp file deletion
    • assemble_and_verify — full pipeline with storage integration
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.assembler import (
    FileAssembler,
    HashVerifier,
    AssemblyResult,
    cleanup_temp_files,
    assemble_and_verify,
)
from core.storage import (
    StorageManager,
    DownloadStatus,
    ChunkRecord,
    ChunkStatus,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _create_chunk_file(path: Path, data: bytes) -> ChunkRecord:
    """Create a temp file and return a matching ChunkRecord."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return ChunkRecord(
        download_id="test-dl",
        chunk_index=0,
        start_byte=0,
        end_byte=len(data) - 1,
        downloaded_bytes=len(data),
        status=ChunkStatus.COMPLETED.value,
        temp_file=str(path),
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
#  FILE ASSEMBLER
# ══════════════════════════════════════════════════════════════════════════════

class TestFileAssembler:
    """Tests for chunk file merging."""

    @pytest.mark.asyncio
    async def test_single_chunk(self, tmp_path: Path) -> None:
        data = b"Hello, World!"
        chunk = _create_chunk_file(tmp_path / "chunks" / "c0.part", data)

        assembler = FileAssembler(tmp_path / "output" / "file.txt")
        result = await assembler.assemble([chunk])

        assert result.success is True
        assert result.chunks_merged == 1
        assert result.file_size == len(data)
        assert Path(result.output_path).read_bytes() == data

    @pytest.mark.asyncio
    async def test_multiple_chunks_order(self, tmp_path: Path) -> None:
        """Chunks are assembled in chunk_index order."""
        chunks_dir = tmp_path / "chunks"
        chunks = []
        for i, text in enumerate([b"AAA", b"BBB", b"CCC"]):
            path = chunks_dir / f"c{i}.part"
            c = _create_chunk_file(path, text)
            c.chunk_index = i
            chunks.append(c)

        assembler = FileAssembler(tmp_path / "output.bin")
        result = await assembler.assemble(chunks)

        assert result.success is True
        assert result.chunks_merged == 3
        assert Path(result.output_path).read_bytes() == b"AAABBBCCC"

    @pytest.mark.asyncio
    async def test_reverse_order_still_correct(self, tmp_path: Path) -> None:
        """Assembler sorts by chunk_index regardless of input order."""
        chunks_dir = tmp_path / "chunks"
        c0 = _create_chunk_file(chunks_dir / "c0.part", b"FIRST")
        c0.chunk_index = 0
        c1 = _create_chunk_file(chunks_dir / "c1.part", b"SECOND")
        c1.chunk_index = 1

        assembler = FileAssembler(tmp_path / "out.bin")
        result = await assembler.assemble([c1, c0])  # reversed

        assert result.success is True
        assert Path(result.output_path).read_bytes() == b"FIRSTSECOND"

    @pytest.mark.asyncio
    async def test_no_chunks(self, tmp_path: Path) -> None:
        assembler = FileAssembler(tmp_path / "out.bin")
        result = await assembler.assemble([])
        assert result.success is False
        assert "No chunks" in result.error

    @pytest.mark.asyncio
    async def test_missing_temp_file(self, tmp_path: Path) -> None:
        chunk = ChunkRecord(
            download_id="x", chunk_index=0,
            start_byte=0, end_byte=99,
            temp_file=str(tmp_path / "nonexistent.part"),
            status=ChunkStatus.COMPLETED.value,
        )

        assembler = FileAssembler(tmp_path / "out.bin")
        result = await assembler.assemble([chunk])
        assert result.success is False
        assert "Missing temp file" in result.error

    @pytest.mark.asyncio
    async def test_conflict_resolution(self, tmp_path: Path) -> None:
        """Existing file gets renamed with counter."""
        out_path = tmp_path / "file.zip"
        out_path.write_bytes(b"existing")

        data = b"new content"
        chunk = _create_chunk_file(tmp_path / "c0.part", data)

        assembler = FileAssembler(out_path)
        result = await assembler.assemble([chunk])

        assert result.success is True
        # Should be file (1).zip
        assert "(1)" in result.output_path
        assert Path(result.output_path).read_bytes() == data
        # Original still exists
        assert out_path.read_bytes() == b"existing"

    @pytest.mark.asyncio
    async def test_double_conflict_resolution(self, tmp_path: Path) -> None:
        out_path = tmp_path / "file.zip"
        out_path.write_bytes(b"v0")
        (tmp_path / "file (1).zip").write_bytes(b"v1")

        chunk = _create_chunk_file(tmp_path / "c0.part", b"v2")
        assembler = FileAssembler(out_path)
        result = await assembler.assemble([chunk])

        assert "(2)" in result.output_path

    @pytest.mark.asyncio
    async def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        data = b"data"
        chunk = _create_chunk_file(tmp_path / "c0.part", data)

        deep_path = tmp_path / "a" / "b" / "c" / "file.bin"
        assembler = FileAssembler(deep_path)
        result = await assembler.assemble([chunk])

        assert result.success is True
        assert deep_path.read_bytes() == data

    @pytest.mark.asyncio
    async def test_progress_callback(self, tmp_path: Path) -> None:
        data = b"x" * 10000
        chunk = _create_chunk_file(tmp_path / "c0.part", data)

        progress_values: list[float] = []
        assembler = FileAssembler(
            tmp_path / "out.bin", buffer_size=1000
        )
        result = await assembler.assemble(
            [chunk],
            on_progress=lambda p: progress_values.append(p),
        )

        assert result.success is True
        assert len(progress_values) > 0
        assert progress_values[-1] == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_cancellation(self, tmp_path: Path) -> None:
        data = b"x" * 100000
        chunk = _create_chunk_file(tmp_path / "c0.part", data)

        assembler = FileAssembler(
            tmp_path / "out.bin", buffer_size=100
        )
        assembler.cancel()  # cancel before starting
        result = await assembler.assemble([chunk])

        assert result.success is False
        assert "cancelled" in result.error.lower()

    @pytest.mark.asyncio
    async def test_large_file_assembly(self, tmp_path: Path) -> None:
        """Assemble a file from many small chunks."""
        chunks = []
        for i in range(20):
            data = bytes([i % 256]) * 500
            c = _create_chunk_file(tmp_path / "chunks" / f"c{i}.part", data)
            c.chunk_index = i
            chunks.append(c)

        assembler = FileAssembler(tmp_path / "big.bin")
        result = await assembler.assemble(chunks)

        assert result.success is True
        assert result.chunks_merged == 20
        assert result.file_size == 20 * 500

    @pytest.mark.asyncio
    async def test_interruption_during_merging_phase(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test the edge case where an interruption (e.g., system error) occurs during the merging phase of large downloads."""
        chunks = []
        for i in range(5):
            data = b"chunk_data_block_" + str(i).encode()
            c = _create_chunk_file(tmp_path / "chunks" / f"c{i}.part", data)
            c.chunk_index = i
            chunks.append(c)

        assembler = FileAssembler(tmp_path / "interrupted.bin")
        
        # Simulate interruption using a stable monkeypatch on aiofiles.open.
        import aiofiles
        original_open = aiofiles.open
        call_count = [0]

        class _FailingFileProxy:
            def __init__(self, wrapped_file):
                self._wrapped = wrapped_file

            async def read(self, *args, **kwargs):
                if call_count[0] > 2:
                    raise OSError("Simulated interruption during merging phase")
                call_count[0] += 1
                return await self._wrapped.read(*args, **kwargs)

            async def write(self, *args, **kwargs):
                if call_count[0] > 2:
                    raise OSError("Simulated interruption during merging phase (network/disk dropout)")
                call_count[0] += 1
                return await self._wrapped.write(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        class _FailingOpenContext:
            def __init__(self, *args, **kwargs):
                self._ctx = original_open(*args, **kwargs)

            async def __aenter__(self):
                wrapped = await self._ctx.__aenter__()
                return _FailingFileProxy(wrapped)

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return await self._ctx.__aexit__(exc_type, exc_val, exc_tb)

        monkeypatch.setattr(aiofiles, "open", _FailingOpenContext)
        
        result = await assembler.assemble(chunks)

        assert result.success is False
        assert "Simulated interruption" in result.error
        # The partial file should be preserved or cleaned up depending on implementation, 
        # but the assembly itself must gracefully fail and report the error.




# ══════════════════════════════════════════════════════════════════════════════
#  HASH VERIFIER
# ══════════════════════════════════════════════════════════════════════════════

class TestHashVerifier:
    """Tests for SHA-256 hash computation and verification."""

    @pytest.mark.asyncio
    async def test_compute_hash(self, tmp_path: Path) -> None:
        data = b"test data for hashing"
        path = tmp_path / "testfile.bin"
        path.write_bytes(data)

        verifier = HashVerifier()
        digest = await verifier.compute_hash(path)

        assert digest == _sha256(data)

    @pytest.mark.asyncio
    async def test_verify_correct_hash(self, tmp_path: Path) -> None:
        data = b"correct content"
        path = tmp_path / "file.bin"
        path.write_bytes(data)

        verifier = HashVerifier()
        result = await verifier.verify(path, _sha256(data))
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_wrong_hash(self, tmp_path: Path) -> None:
        path = tmp_path / "file.bin"
        path.write_bytes(b"actual content")

        verifier = HashVerifier()
        result = await verifier.verify(path, "0000deadbeef")
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_case_insensitive(self, tmp_path: Path) -> None:
        data = b"case test"
        path = tmp_path / "file.bin"
        path.write_bytes(data)
        expected = _sha256(data).upper()

        verifier = HashVerifier()
        result = await verifier.verify(path, expected)
        assert result is True

    @pytest.mark.asyncio
    async def test_hash_missing_file(self, tmp_path: Path) -> None:
        verifier = HashVerifier()
        digest = await verifier.compute_hash(tmp_path / "nope.bin")
        assert digest == ""

    @pytest.mark.asyncio
    async def test_hash_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")

        verifier = HashVerifier()
        digest = await verifier.compute_hash(path)
        assert digest == _sha256(b"")

    @pytest.mark.asyncio
    async def test_hash_progress(self, tmp_path: Path) -> None:
        data = b"x" * 50000
        path = tmp_path / "file.bin"
        path.write_bytes(data)

        progress: list[float] = []
        verifier = HashVerifier(buffer_size=5000)
        await verifier.compute_hash(
            path, on_progress=lambda p: progress.append(p)
        )

        assert len(progress) > 0
        assert progress[-1] == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_hash_cancellation(self, tmp_path: Path) -> None:
        data = b"x" * 100000
        path = tmp_path / "file.bin"
        path.write_bytes(data)

        verifier = HashVerifier(buffer_size=100)
        verifier.cancel()
        digest = await verifier.compute_hash(path)
        assert digest == ""


# ══════════════════════════════════════════════════════════════════════════════
#  TEMP FILE CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

class TestCleanupTempFiles:
    """Tests for temp file cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_deletes_files(self, tmp_path: Path) -> None:
        chunks_dir = tmp_path / "chunks"
        c = _create_chunk_file(chunks_dir / "c0.part", b"data")

        cleaned = await cleanup_temp_files([c], chunks_dir)
        assert cleaned == 1
        assert not (chunks_dir / "c0.part").exists()

    @pytest.mark.asyncio
    async def test_cleanup_missing_files(self, tmp_path: Path) -> None:
        chunk = ChunkRecord(
            download_id="x", chunk_index=0,
            temp_file=str(tmp_path / "nope.part"),
        )
        cleaned = await cleanup_temp_files([chunk])
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_cleanup_removes_directory(self, tmp_path: Path) -> None:
        chunks_dir = tmp_path / "chunks"
        c = _create_chunk_file(chunks_dir / "c0.part", b"x")

        await cleanup_temp_files([c], chunks_dir)
        assert not chunks_dir.exists()


# ══════════════════════════════════════════════════════════════════════════════
#  ASSEMBLE & VERIFY PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

class TestAssembleAndVerify:
    """Integration tests for the full post-download pipeline."""

    @pytest.fixture
    async def storage(self, tmp_path: Path) -> StorageManager:
        mgr = StorageManager(tmp_path / "test.db")
        await mgr.initialize()
        yield mgr
        await mgr.close()

    @pytest.mark.asyncio
    async def test_full_pipeline_no_hash(
        self, storage: StorageManager, tmp_path: Path,
    ) -> None:
        dl_id = await storage.add_download(
            url="http://example.com/f.bin",
            filename="f.bin",
            save_path=str(tmp_path / "output" / "f.bin"),
            file_size=6,
        )

        # Create chunk files
        chunks_dir = tmp_path / "chunks"
        c0 = _create_chunk_file(chunks_dir / "c0.part", b"ABC")
        c0.download_id = dl_id
        c0.chunk_index = 0
        c1 = _create_chunk_file(chunks_dir / "c1.part", b"DEF")
        c1.download_id = dl_id
        c1.chunk_index = 1

        await storage.add_chunks(dl_id, [c0, c1])
        # Mark chunks completed
        await storage.update_chunk_status(dl_id, 0, ChunkStatus.COMPLETED)
        await storage.update_chunk_status(dl_id, 1, ChunkStatus.COMPLETED)

        result = await assemble_and_verify(
            dl_id, storage, chunks_dir=chunks_dir,
        )

        assert result.success is True
        assert result.chunks_merged == 2
        assert Path(result.output_path).read_bytes() == b"ABCDEF"
        assert result.hash_actual != ""

        # Verify storage status updated
        record = await storage.get_download(dl_id)
        assert record.status == DownloadStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_full_pipeline_with_hash_match(
        self, storage: StorageManager, tmp_path: Path,
    ) -> None:
        data = b"verified content"
        expected_hash = _sha256(data)

        dl_id = await storage.add_download(
            url="http://example.com/v.bin",
            filename="v.bin",
            save_path=str(tmp_path / "output" / "v.bin"),
            file_size=len(data),
            hash_expected=expected_hash,
        )

        chunks_dir = tmp_path / "chunks"
        c = _create_chunk_file(chunks_dir / "c0.part", data)
        c.download_id = dl_id
        await storage.add_chunks(dl_id, [c])
        await storage.update_chunk_status(dl_id, 0, ChunkStatus.COMPLETED)

        result = await assemble_and_verify(dl_id, storage, chunks_dir=chunks_dir)

        assert result.success is True
        assert result.hash_verified is True

    @pytest.mark.asyncio
    async def test_full_pipeline_hash_mismatch(
        self, storage: StorageManager, tmp_path: Path,
    ) -> None:
        dl_id = await storage.add_download(
            url="http://example.com/bad.bin",
            filename="bad.bin",
            save_path=str(tmp_path / "output" / "bad.bin"),
            file_size=4,
            hash_expected="0000000000000000000000000000000000000000",
        )

        chunks_dir = tmp_path / "chunks"
        c = _create_chunk_file(chunks_dir / "c0.part", b"data")
        c.download_id = dl_id
        await storage.add_chunks(dl_id, [c])
        await storage.update_chunk_status(dl_id, 0, ChunkStatus.COMPLETED)

        result = await assemble_and_verify(dl_id, storage, chunks_dir=chunks_dir)

        assert result.success is False
        assert result.hash_verified is False
        assert "mismatch" in result.error.lower()

        record = await storage.get_download(dl_id)
        assert record.status == DownloadStatus.FAILED.value

    @pytest.mark.asyncio
    async def test_missing_download(self, storage: StorageManager) -> None:
        result = await assemble_and_verify("nonexistent", storage)
        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_incomplete_chunks_rejected(
        self, storage: StorageManager, tmp_path: Path,
    ) -> None:
        dl_id = await storage.add_download(
            url="http://example.com/f.bin",
            filename="f.bin",
            save_path=str(tmp_path / "f.bin"),
        )
        c = ChunkRecord(download_id=dl_id, chunk_index=0, temp_file="/x")
        await storage.add_chunks(dl_id, [c])
        # Don't mark as completed

        result = await assemble_and_verify(dl_id, storage)
        assert result.success is False
        assert "incomplete" in result.error.lower()
