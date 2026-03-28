"""
IDM Core — BitTorrent Support
==============================
Handles BitTorrent downloading via libtorrent (rasterbar).

Features:
    • Magnet link parsing and metadata fetching
    • Support for .torrent files
    • Fast-resume data persistence
    • Progress reporting and speed monitoring
    • Integration with the main DownloadEngine
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional, Callable

log = logging.getLogger("idm.core.torrent")

try:
    import libtorrent as lt
except ImportError:
    lt = None
    log.warning("libtorrent not found or DLL failed to load. "
                "BitTorrent support is disabled.")

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  TORRENT DOWNLOADER                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class TorrentManager:
    """
    Manages BitTorrent downloads using libtorrent.

    This class runs its own libtorrent session and provides an async
    interface for adding, pausing, and monitoring torrents.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._session: Optional[lt.session] = None
        self._handles: dict[str, lt.torrent_handle] = {}
        self._active = False
        self._loop_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Initialize the libtorrent session."""
        if self._active:
            return

        if lt is None:
            raise RuntimeError("BitTorrent support is unavailable: libtorrent is not installed")

        settings = {
            "user_agent": "Internet Download Manager/1.0",
            "listen_interfaces": "0.0.0.0:6881, [::]:6881",
            "alert_mask": (
                lt.alert.category_t.status_notification |
                lt.alert.category_t.storage_notification |
                lt.alert.category_t.error_notification
            ),
        }

        self._session = lt.session(settings)
        self._active = True
        self._loop_task = asyncio.create_task(self._session_loop())
        log.info("Torrent session started")

    async def stop(self) -> None:
        """Shutdown the libtorrent session."""
        self._active = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

        if self._session:
            # We don't delete handles manually; session destruction handles it.
            # But we might want to save resumes here if we weren't doing it continuously.
            self._session = None

        log.info("Torrent session stopped")

    # ── Download Operations ────────────────────────────────────────────────

    async def add_magnet(
        self,
        magnet_uri: str,
        save_path: str,
        resume_data: Optional[bytes] = None,
    ) -> str:
        """
        Add a magnet link to the queue.

        Returns:
            The info_hash of the torrent as a hex string (ID).
        """
        if not self._session:
            await self.start()

        session = self._session
        if session is None:
            raise RuntimeError("Torrent session failed to initialize")

        params = lt.parse_magnet_uri(magnet_uri)
        params.save_path = str(Path(save_path).parent)
        if resume_data:
            params.resume_data = list(resume_data)

        handle = session.add_torrent(params)
        info_hash = str(handle.info_hash())
        self._handles[info_hash] = handle

        log.info(f"Added magnet torrent: {info_hash}")
        return info_hash

    async def add_torrent_file(
        self,
        torrent_file: str,
        save_path: str,
        resume_data: Optional[bytes] = None,
    ) -> str:
        """Add a .torrent file to the queue."""
        if not self._session:
            await self.start()

        session = self._session
        if session is None:
            raise RuntimeError("Torrent session failed to initialize")

        info = lt.torrent_info(torrent_file)
        params = {
            "ti": info,
            "save_path": str(Path(save_path).parent),
        }
        if resume_data:
            params["resume_data"] = list(resume_data)

        handle = session.add_torrent(params)
        info_hash = str(handle.info_hash())
        self._handles[info_hash] = handle

        log.info(f"Added file torrent: {info_hash}")
        return info_hash

    async def pause(self, info_hash: str) -> None:
        if handle := self._handles.get(info_hash):
            handle.pause()

    async def resume(self, info_hash: str) -> None:
        if handle := self._handles.get(info_hash):
            handle.resume()

    async def remove(self, info_hash: str, delete_files: bool = False) -> None:
        if handle := self._handles.pop(info_hash, None):
            if not self._session:
                return
            self._session.remove_torrent(
                handle,
                lt.session.delete_files if delete_files else 0,
            )

    # ── Status Monitoring ──────────────────────────────────────────────────

    def get_status(self, info_hash: str) -> Optional[dict[str, Any]]:
        """Get the current status of a torrent."""
        handle = self._handles.get(info_hash)
        if not handle:
            return None

        status = handle.status()
        ti = handle.get_torrent_info()

        return {
            "id": info_hash,
            "name": status.name,
            "state": str(status.state),
            "progress": status.progress * 100,
            "download_rate": status.download_rate,
            "upload_rate": status.upload_rate,
            "num_peers": status.num_peers,
            "total_wanted": status.total_wanted,
            "total_done": status.total_done,
            "is_seeding": status.is_seeding,
            "save_path": status.save_path,
        }

    # ── Session Loop ───────────────────────────────────────────────────────

    async def _session_loop(self) -> None:
        """Internal loop to process alerts and state updates."""
        try:
            while self._active:
                if not self._session:
                    break

                alerts = self._session.pop_alerts()
                for alert in alerts:
                    if isinstance(alert, lt.add_torrent_alert):
                        log.info(f"Torrent {alert.torrent_name()} added")
                    elif isinstance(alert, lt.torrent_error_alert):
                        log.error(f"Torrent error: {alert.message()}")
                    elif isinstance(alert, lt.metadata_received_alert):
                        log.info("Metadata received for torrent")
                    elif isinstance(alert, lt.state_changed_alert):
                        log.info(f"State changed: {alert.message()}")

                await asyncio.sleep(1)
        except Exception:
            log.exception("Error in torrent session loop")
