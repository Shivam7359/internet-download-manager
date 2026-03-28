"""
IDM Core — FTP Support
========================
Handles FTP and FTPS downloading using aioftp.

Features:
    • Anonymous and authenticated login
    • Passive mode support (default)
    • Resuming downloads via REST command (Restart)
    • Progress reporting and speed monitoring
    • FTPS support (SSL/TLS)
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional, Callable

import aioftp

log = logging.getLogger("idm.core.ftp")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FTP DOWNLOADER                                                             ║
# ╚══════════════════════════════════════════════════════════════════════════════╗

class FtpDownloader:
    """
    Handles downloading from FTP and FTPS servers.

    This class provides an async interface for downloading files from FTP server.
    It supports anonymous and authenticated logins, passive mode, and resume.
    """

    def __init__(
        self,
        url: str,
        save_path: str,
        config: dict[str, Any],
        on_progress: Optional[Callable[[int, int, float, float], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._url = url
        self._save_path = Path(save_path)
        self._config = config
        self._on_progress = on_progress
        self._on_status = on_status

        self._active = False
        self._paused = False
        self._stop_event = asyncio.Event()

        # Parse FTP URL (ftp://[user[:password]@]host[:port]/path)
        from urllib.parse import urlparse
        self._parsed_url = urlparse(url)
        self._host = self._parsed_url.hostname or "localhost"
        self._port = self._parsed_url.port or 21
        self._user = self._parsed_url.username or "anonymous"
        self._password = self._parsed_url.password or "anonymous"
        self._remote_path = self._parsed_url.path

    async def start(self) -> None:
        """Start the FTP download."""
        if self._active:
            return

        self._active = True
        self._paused = False
        self._stop_event.clear()

        try:
            if self._on_status:
                self._on_status("connecting")

            async with aioftp.Client.context(
                self._host,
                self._port,
                self._user,
                self._password,
            ) as client:
                if self._on_status:
                    self._on_status("downloading")

                # Get file size
                stats = await client.stat(self._remote_path)
                total_size = int(stats.get("size", -1))
                if total_size < 0:
                    log.warning(f"Unable to determine file size for {self._remote_path}")

                # Check if file exists and we should resume
                start_offset = 0
                if self._save_path.exists():
                    start_offset = self._save_path.stat().st_size
                    if start_offset >= total_size:
                        log.info(f"File {self._save_path} already complete")
                        if self._on_status:
                            self._on_status("completed")
                        return

                mode = "ab" if start_offset > 0 else "wb"
                log.info(f"Downloading from {self._host}:{self._port} offset {start_offset}")

                start_time = time.time()
                downloaded = start_offset

                async with client.download_stream(
                    self._remote_path,
                    offset=start_offset,
                ) as stream:
                    with open(self._save_path, mode) as f:
                        async for chunk in stream.iter_by_block(
                            self._config.get("advanced", {}).get(
                                "chunk_buffer_size_bytes", 65536
                            )
                        ):
                            if not self._active:
                                break
                            while self._paused:
                                await asyncio.sleep(0.5)

                            f.write(chunk)
                            downloaded += len(chunk)

                            # Progress signaling
                            if self._on_progress:
                                now = time.time()
                                elapsed = now - start_time
                                speed = (downloaded - start_offset) / elapsed if elapsed > 0 else 0
                                eta = (total_size - downloaded) / speed if speed > 0 else 0
                                self._on_progress(downloaded, total_size, speed, eta)

                if self._active:
                    if self._on_status:
                        self._on_status("completed")
                    log.info(f"FTP download completed: {self._save_path.name}")

        except Exception as e:
            log.exception(f"FTP download error: {e}")
            if self._on_status:
                self._on_status("failed")
        finally:
            self._active = False

    async def pause(self) -> None:
        self._paused = True
        if self._on_status:
            self._on_status("paused")

    async def resume(self) -> None:
        self._paused = False
        if self._on_status:
            self._on_status("downloading")

    async def stop(self) -> None:
        self._active = False
        self._stop_event.set()
        if self._on_status:
            self._on_status("cancelled")
