"""
IDM Core — File Assembler & Hash Verifier
===========================================
Merges downloaded chunk temp files into the final output file and
optionally verifies its SHA-256 hash.

This module provides:

    • **FileAssembler** — Reads chunk temp files in order and writes
      them sequentially into the final destination file.  Supports
      progress callbacks and cancellation.
    • **HashVerifier** — Computes SHA-256 of the assembled file and
      compares against an expected hash.
    • **assemble_and_verify()** — Convenience coroutine that runs both
      steps and updates the storage layer.

Design notes:
    • All I/O is async via ``aiofiles`` to avoid blocking the engine loop.
    • Chunks are read/written in configurable buffer sizes (default 1 MB).
    • Temp files are deleted after successful assembly.
    • The assembler creates parent directories automatically.

Usage::

    result = await assemble_and_verify(
        download_id=dl_id,
        storage=storage,
        on_progress=lambda pct: print(f"{pct:.1f}%"),
    )
    if result.success:
        print(f"File saved: {result.output_path}")
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import aiofiles

from core.storage import (
    StorageManager,
    DownloadRecord,
    DownloadStatus,
    ChunkRecord,
    ChunkStatus,
)

log = logging.getLogger("idm.core.assembler")

# ── Constants ──────────────────────────────────────────────────────────────────
ASSEMBLE_BUFFER_SIZE: int = 1_048_576    # 1 MB read/write buffer
HASH_BUFFER_SIZE: int = 1_048_576        # 1 MB hash read buffer


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DATA CLASSES                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class AssemblyResult:
    """
    Result of file assembly and optional hash verification.

    Attributes:
        success: True if the file was assembled (and verified, if expected).
        output_path: Absolute path to the final assembled file.
        file_size: Size of the assembled file in bytes.
        hash_actual: Computed SHA-256 hex digest.
        hash_verified: True if hash matches expected, None if no expected hash.
        error: Error message if assembly or verification failed.
        chunks_merged: Number of chunk files merged.
        temp_files_cleaned: Number of temp files deleted.
    """
    success: bool = False
    output_path: str = ""
    file_size: int = 0
    hash_actual: str = ""
    hash_verified: Optional[bool] = None
    error: Optional[str] = None
    chunks_merged: int = 0
    temp_files_cleaned: int = 0


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FILE ASSEMBLER                                                             ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class FileAssembler:
    """
    Merges chunk temp files into the final output file.

    Chunks are read in ascending ``chunk_index`` order and written
    sequentially into the output file.  If a chunk's temp file is
    missing, assembly fails with a descriptive error.

    Args:
        output_path: Destination path for the assembled file.
        buffer_size: Read/write buffer size in bytes.
    """

    def __init__(
        self,
        output_path: str | Path,
        buffer_size: int = ASSEMBLE_BUFFER_SIZE,
    ) -> None:
        self._output_path = Path(output_path)
        self._buffer_size = buffer_size
        self._cancelled = False

    @property
    def output_path(self) -> Path:
        return self._output_path

    def cancel(self) -> None:
        """Signal cancellation — assembly will stop after the current buffer."""
        self._cancelled = True

    async def assemble(
        self,
        chunks: list[ChunkRecord],
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> AssemblyResult:
        """
        Merge chunk temp files into the final output file.

        Args:
            chunks: List of ``ChunkRecord`` objects, will be sorted by index.
            on_progress: Optional callback receiving progress 0.0–100.0.

        Returns:
            An ``AssemblyResult`` with the outcome.
        """
        result = AssemblyResult(output_path=str(self._output_path))

        if self._cancelled:
            result.error = "Assembly cancelled"
            return result

        if not chunks:
            result.error = "No chunks to assemble"
            return result

        # Sort by chunk_index
        sorted_chunks = sorted(chunks, key=lambda c: c.chunk_index)

        # Validate all temp files exist
        for chunk in sorted_chunks:
            temp = Path(chunk.temp_file)
            if not temp.exists():
                result.error = (
                    f"Missing temp file for chunk {chunk.chunk_index}: "
                    f"{chunk.temp_file}"
                )
                log.error(result.error)
                return result

        # Calculate total bytes for progress
        total_bytes = sum(
            Path(c.temp_file).stat().st_size for c in sorted_chunks
        )
        bytes_written = 0

        # Create output directory
        self._output_path.parent.mkdir(parents=True, exist_ok=True)

        # Handle file name conflicts
        final_path = self._resolve_conflict(self._output_path)
        result.output_path = str(final_path)

        async def _cleanup_partial_output() -> None:
            """Best-effort cleanup for partially assembled output files."""
            try:
                await asyncio.to_thread(final_path.unlink, True)
            except OSError:
                pass

        try:
            async with aiofiles.open(final_path, "wb") as out_file:
                for chunk in sorted_chunks:
                    if self._cancelled:
                        result.error = "Assembly cancelled"
                        await _cleanup_partial_output()
                        return result

                    async with aiofiles.open(chunk.temp_file, "rb") as in_file:
                        while True:
                            data = await in_file.read(self._buffer_size)
                            if not data:
                                break

                            await out_file.write(data)
                            bytes_written += len(data)

                            if on_progress and total_bytes > 0:
                                pct = (bytes_written / total_bytes) * 100.0
                                on_progress(min(100.0, pct))

                            if self._cancelled:
                                result.error = "Assembly cancelled"
                                await _cleanup_partial_output()
                                return result

            result.success = True
            result.file_size = bytes_written
            result.chunks_merged = len(sorted_chunks)

            log.info(
                "Assembly complete: %s (%d chunks, %d bytes)",
                final_path.name, len(sorted_chunks), bytes_written,
            )

        except OSError as exc:
            result.error = f"File write error: {exc}"
            log.error("Assembly failed: %s", exc)
            await _cleanup_partial_output()

        return result

    @staticmethod
    def _resolve_conflict(path: Path) -> Path:
        """
        Resolve filename conflicts by appending a counter.

        ``file.zip`` → ``file (1).zip`` → ``file (2).zip`` → …

        Args:
            path: The desired output path.

        Returns:
            A path that does not conflict with existing files.
        """
        if not path.exists():
            return path

        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 1

        while True:
            candidate = parent / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  HASH VERIFIER                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class HashVerifier:
    """
    Compute and verify SHA-256 file hashes.

    Args:
        buffer_size: Read buffer size for hashing.
    """

    def __init__(self, buffer_size: int = HASH_BUFFER_SIZE) -> None:
        self._buffer_size = buffer_size
        self._cancelled = False

    def cancel(self) -> None:
        """Signal cancellation."""
        self._cancelled = True

    async def compute_hash(
        self,
        file_path: str | Path,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> str:
        """
        Compute the SHA-256 hash of a file.

        Args:
            file_path: Path to the file.
            on_progress: Optional callback receiving progress 0.0–100.0.

        Returns:
            Hex digest string, or empty string on error/cancellation.
        """
        path = Path(file_path)
        if not path.exists():
            log.error("Cannot hash — file not found: %s", path)
            return ""

        if self._cancelled:
            return ""

        file_size = path.stat().st_size
        bytes_read = 0
        hasher = hashlib.sha256()

        try:
            async with aiofiles.open(path, "rb") as f:
                while True:
                    data = await f.read(self._buffer_size)
                    if not data:
                        break

                    # Hash computation is CPU-bound — offload to thread
                    await asyncio.to_thread(hasher.update, data)
                    bytes_read += len(data)

                    if on_progress and file_size > 0:
                        pct = (bytes_read / file_size) * 100.0
                        on_progress(min(100.0, pct))

                    if self._cancelled:
                        return ""

            digest = hasher.hexdigest()
            log.info("SHA-256: %s → %s", path.name, digest)
            return digest

        except OSError as exc:
            log.error("Hash computation failed: %s", exc)
            return ""

    async def verify(
        self,
        file_path: str | Path,
        expected_hash: str,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> bool:
        """
        Verify a file's SHA-256 hash against an expected value.

        Args:
            file_path: Path to the file.
            expected_hash: Expected SHA-256 hex digest.
            on_progress: Optional progress callback.

        Returns:
            True if hashes match, False otherwise.
        """
        actual = await self.compute_hash(file_path, on_progress)
        if not actual:
            return False

        match = actual.lower() == expected_hash.lower()
        if match:
            log.info("Hash verified: %s ✓", Path(file_path).name)
        else:
            log.warning(
                "Hash mismatch for %s: expected=%s actual=%s",
                Path(file_path).name, expected_hash, actual,
            )
        return match


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  TEMP FILE CLEANUP                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

async def cleanup_temp_files(
    chunks: list[ChunkRecord],
    chunks_dir: Optional[Path] = None,
) -> int:
    """
    Delete chunk temp files and their parent directory.

    Args:
        chunks: List of chunk records whose temp files should be deleted.
        chunks_dir: Optional parent directory to remove if empty.

    Returns:
        Number of temp files successfully deleted.
    """
    cleaned = 0
    for chunk in chunks:
        temp = Path(chunk.temp_file)
        if temp.exists():
            try:
                await asyncio.to_thread(temp.unlink)
                cleaned += 1
            except OSError as exc:
                log.warning("Failed to delete temp file %s: %s", temp, exc)

    # Remove the chunk directory if empty
    if chunks_dir and chunks_dir.exists():
        try:
            await asyncio.to_thread(shutil.rmtree, str(chunks_dir), True)
        except OSError:
            pass

    log.debug("Cleaned up %d temp files", cleaned)
    return cleaned


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CONVENIENCE: ASSEMBLE & VERIFY                                             ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

async def assemble_and_verify(
    download_id: str,
    storage: StorageManager,
    *,
    chunks_dir: Optional[Path] = None,
    on_assemble_progress: Optional[Callable[[float], None]] = None,
    on_verify_progress: Optional[Callable[[float], None]] = None,
    verify_hash: bool = True,
    hash_algorithm: str = "sha256",
    cleanup: bool = True,
) -> AssemblyResult:
    """
    Complete post-download pipeline: assemble → verify → cleanup.

    This is the primary entry point called by ``DownloadTask`` after all
    chunks finish downloading.

    Steps:
        1. Load download record and chunks from storage.
        2. Update status to MERGING.
        3. Assemble chunks into the final file.
        4. If an expected hash is set, update status to VERIFYING and
           compute/compare SHA-256.
        5. Update status to COMPLETED (or FAILED on error).
        6. Clean up temp files.

    Args:
        download_id: The download UUID.
        storage: The storage manager.
        chunks_dir: Directory containing chunk temp files.
        on_assemble_progress: Progress callback for assembly phase.
        on_verify_progress: Progress callback for verification phase.
        verify_hash: If False, skip hash computation/verification.
        hash_algorithm: Hash algorithm requested by config. Currently only
            ``sha256`` is supported; other values are ignored with a warning.
        cleanup: Whether to delete temp files after success.

    Returns:
        An ``AssemblyResult`` with full details.
    """
    record = await storage.get_download(download_id)
    if not record:
        return AssemblyResult(error=f"Download {download_id} not found")

    chunks = await storage.get_chunks(download_id)
    if not chunks:
        return AssemblyResult(error=f"No chunks for download {download_id}")

    # Verify all chunks are completed
    incomplete = [c for c in chunks if c.status != ChunkStatus.COMPLETED.value]
    if incomplete:
        return AssemblyResult(
            error=f"{len(incomplete)} chunks still incomplete"
        )

    # ── Step 1: Assemble ───────────────────────────────────────────────
    await storage.update_download_status(download_id, DownloadStatus.MERGING)

    assembler = FileAssembler(record.save_path)
    result = await assembler.assemble(chunks, on_progress=on_assemble_progress)

    if not result.success:
        await storage.update_download_status(
            download_id, DownloadStatus.FAILED,
            error_message=result.error or "Assembly failed",
        )
        return result

    # ── Step 2: Verify hash (if enabled) ───────────────────────────────
    if verify_hash:
        algo = str(hash_algorithm or "sha256").strip().lower()
        if algo != "sha256":
            log.warning(
                "Unsupported hash algorithm '%s'; falling back to sha256",
                hash_algorithm,
            )

    if verify_hash and record.hash_expected:
        await storage.update_download_status(
            download_id, DownloadStatus.VERIFYING
        )

        verifier = HashVerifier()
        result.hash_actual = await verifier.compute_hash(
            result.output_path, on_progress=on_verify_progress
        )
        result.hash_verified = (
            result.hash_actual.lower() == record.hash_expected.lower()
        )

        await storage.update_download_field(
            download_id,
            hash_actual=result.hash_actual,
            hash_verified=result.hash_verified,
        )

        if not result.hash_verified:
            result.success = False
            result.error = (
                f"Hash mismatch: expected {record.hash_expected}, "
                f"got {result.hash_actual}"
            )
            await storage.update_download_status(
                download_id, DownloadStatus.FAILED,
                error_message=result.error,
            )
            return result

        log.info("Hash verified for %s ✓", record.filename)
    elif verify_hash:
        # No hash to verify — still compute for records
        verifier = HashVerifier()
        result.hash_actual = await verifier.compute_hash(result.output_path)
        if result.hash_actual:
            await storage.update_download_field(
                download_id, hash_actual=result.hash_actual
            )

    # ── Step 3: Mark complete ──────────────────────────────────────────
    await storage.update_download_status(download_id, DownloadStatus.COMPLETED)
    result.success = True

    # ── Step 4: Cleanup ────────────────────────────────────────────────
    if cleanup:
        cleaned = await cleanup_temp_files(chunks, chunks_dir)
        result.temp_files_cleaned = cleaned

    log.info(
        "Download complete: %s → %s",
        record.filename, result.output_path,
    )

    return result
