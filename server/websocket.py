# IDM v2.0 — websocket.py — audited 2026-03-28
"""
IDM Server — WebSocket Handler
================================
Real-time progress updates pushed to connected browser extensions.

The WebSocket endpoint at ``/ws`` sends JSON messages for:
    • Download progress updates (speed, bytes, ETA)
    • Status change notifications
    • New download added notifications
    • Download completion notifications

Message format::

    {
        "type": "progress" | "status" | "added" | "complete" | "error",
        "download_id": "uuid",
        "data": { ... }
    }

Usage::

    manager = ConnectionManager()
    # In the FastAPI app:
    @app.websocket("/ws")
    async def ws(websocket): await manager.handle(websocket)

    # From the engine callbacks:
    await manager.broadcast_progress(download_id, ...)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("idm.server.websocket")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CONNECTION MANAGER                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class ConnectionManager:
    """
    Manages WebSocket connections and broadcasts messages.

    Features:
        • Multiple concurrent connections (one per browser tab/extension)
        • Automatic cleanup on disconnect
        • Rate-limited progress broadcasts (debounce per download)
        • JSON message serialization
    """

    def __init__(self, progress_interval: float = 0.5) -> None:
        self._connections: Set[WebSocket] = set()
        self._progress_interval = progress_interval
        self._last_progress: dict[str, float] = {}  # download_id → timestamp

    @property
    def connection_count(self) -> int:
        """Number of active WebSocket connections."""
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self._connections.add(websocket)
        log.info("WebSocket connected (%d active)", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket."""
        self._connections.discard(websocket)
        log.info("WebSocket disconnected (%d active)", len(self._connections))

    async def handle(self, websocket: WebSocket) -> None:
        """
        Handle a WebSocket connection lifecycle.

        Accepts the connection, listens for messages (handles pings),
        and cleans up on disconnect.
        """
        await self.connect(websocket)
        try:
            while True:
                # Listen for client messages (ping/pong, subscribe, etc.)
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                    await self._handle_client_message(websocket, msg)
                except json.JSONDecodeError:
                    await self._send(websocket, {
                        "type": "error",
                        "data": {"message": "Invalid JSON"},
                    })
        except WebSocketDisconnect:
            self.disconnect(websocket)
        except Exception:
            log.debug("WebSocket error", exc_info=True)
            self.disconnect(websocket)

    async def _handle_client_message(
        self, websocket: WebSocket, msg: dict[str, Any]
    ) -> None:
        """Process a message from the client."""
        msg_type = msg.get("type", "")

        if msg_type == "ping":
            await self._send(websocket, {"type": "pong"})

        elif msg_type == "subscribe":
            # Future: per-download subscriptions
            await self._send(websocket, {
                "type": "subscribed",
                "data": {"message": "Subscribed to all events"},
            })

    # ── Broadcasting ───────────────────────────────────────────────────────

    async def broadcast(self, message: dict[str, Any]) -> None:
        """
        Send a message to all connected clients.

        Disconnected clients are automatically removed.
        """
        if not self._connections:
            return

        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self._connections.discard(ws)

    async def broadcast_progress(
        self,
        download_id: str,
        downloaded: int,
        total: int,
        speed: float,
        eta_seconds: float,
    ) -> None:
        """
        Broadcast download progress (rate-limited).

        Only sends if enough time has passed since the last progress
        update for this download.
        """
        now = time.monotonic()
        last = self._last_progress.get(download_id, 0)
        if now - last < self._progress_interval:
            return

        self._last_progress[download_id] = now

        progress_pct = (downloaded / total * 100.0) if total > 0 else 0.0

        await self.broadcast({
            "type": "progress",
            "download_id": download_id,
            "data": {
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "progress_percent": round(progress_pct, 1),
                "speed_bps": round(speed, 1),
                "eta_seconds": round(eta_seconds, 1),
            },
        })

    async def broadcast_status(
        self,
        download_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Broadcast a download status change."""
        await self.broadcast({
            "type": "status",
            "download_id": download_id,
            "data": {
                "status": status,
                "error": error,
            },
        })

    async def broadcast_added(
        self,
        download_id: str,
        filename: str,
        url: str,
    ) -> None:
        """Broadcast that a new download was added."""
        await self.broadcast({
            "type": "added",
            "download_id": download_id,
            "data": {
                "filename": filename,
                "url": url,
            },
        })

    async def broadcast_complete(self, download_id: str) -> None:
        """Broadcast download completion."""
        # Clean up rate limit tracker
        self._last_progress.pop(download_id, None)

        await self.broadcast({
            "type": "complete",
            "download_id": download_id,
            "data": {},
        })

    async def _send(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        """Send a message to a single client."""
        try:
            await websocket.send_json(message)
        except Exception:
            self._connections.discard(websocket)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ENGINE CALLBACKS BRIDGE                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class WebSocketCallbacks:
    """
    Bridges DownloadEngine callbacks to WebSocket broadcasts.

    Implements the ``DownloadCallbacks`` protocol and forwards all
    events to the ``ConnectionManager`` for WebSocket broadcasting.
    """

    def __init__(self, manager: ConnectionManager) -> None:
        self._manager = manager

    # Track all fire-and-forget tasks so unhandled exceptions are surfaced.
    def _spawn(self, coro: Any, *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("WebSocket background task failed: %s", name)

        task.add_done_callback(_on_done)

    def on_progress(
        self,
        download_id: str,
        downloaded: int,
        total: int,
        speed: float,
        eta_seconds: float,
    ) -> None:
        self._spawn(
            self._manager.broadcast_progress(download_id, downloaded, total, speed, eta_seconds),
            name=f"ws-progress-{download_id[:8]}",
        )

    def on_status_changed(
        self,
        download_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        self._spawn(
            self._manager.broadcast_status(download_id, status, error),
            name=f"ws-status-{download_id[:8]}",
        )

    def on_download_added(
        self, download_id: str, record: Any
    ) -> None:
        self._spawn(
            self._manager.broadcast_added(
                download_id,
                getattr(record, "filename", ""),
                getattr(record, "url", ""),
            ),
            name=f"ws-added-{download_id[:8]}",
        )

    def on_chunk_progress(self, download_id: str, completed: int, total: int) -> None:
        """Chunk counters are UI-only; no websocket broadcast required."""
        return

    def on_download_complete(self, download_id: str) -> None:
        self._spawn(
            self._manager.broadcast_complete(download_id),
            name=f"ws-complete-{download_id[:8]}",
        )


def register_websocket(app: Any, manager: ConnectionManager) -> None:
    """Register the WebSocket endpoint on the FastAPI app."""

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await manager.handle(websocket)

    log.info("WebSocket endpoint registered at /ws")
