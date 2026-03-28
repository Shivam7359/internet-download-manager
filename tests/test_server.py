"""
Unit tests for server/ — REST API and WebSocket.

Tests cover:
    • Health check, add, status, list, pause, resume, cancel endpoints
    • Auth token middleware
    • WebSocket connection manager and broadcasting
    • API response format
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

sys.path.insert(0, str(Path(__file__).parent.parent))

from server.api import create_app, ApiResponse
from server.websocket import ConnectionManager, WebSocketCallbacks


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_engine() -> MagicMock:
    engine = MagicMock()
    engine.add_download = AsyncMock(return_value="test-uuid-1234-5678-9012")
    engine.pause = AsyncMock()
    engine.resume = AsyncMock()
    engine.cancel = AsyncMock()
    engine.active_count = 0
    engine.get_total_speed = MagicMock(return_value=0.0)
    engine.get_active_speeds = MagicMock(return_value={})
    engine.network_manager = MagicMock()
    engine.network_manager.get_pool_stats = MagicMock(return_value={
        "initialized": True,
        "connector": {"limit_total": 100, "limit_per_host": 10, "acquired": 0, "idle": 0},
    })
    return engine


@pytest.fixture
async def mock_storage(tmp_path: Path):
    from core.storage import StorageManager
    mgr = StorageManager(tmp_path / "test.db")
    await mgr.initialize()
    yield mgr
    await mgr.close()


@pytest.fixture
def app(mock_engine: MagicMock, mock_storage: Any) -> Any:
    instance = create_app(
        engine=mock_engine,
        storage=mock_storage,
        config={"server": {"auth_token": ""}},
    )
    instance.state.session_tokens.clear()
    instance.state.pairing_code = "TESTCODE"
    instance.state.pairing_code_expires_at = 2_000_000_000.0
    return instance


@pytest.fixture
def authed_app(mock_engine: MagicMock, mock_storage: Any) -> Any:
    instance = create_app(
        engine=mock_engine,
        storage=mock_storage,
        config={"server": {"auth_token": "secret-token"}},
    )
    instance.state.session_tokens.clear()
    instance.state.pairing_code = "TESTCODE"
    instance.state.pairing_code_expires_at = 2_000_000_000.0
    return instance


@pytest.fixture
def auth_headers(app: Any) -> dict[str, str]:
    token, _ = app.state.session_tokens.issue()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def authed_headers(authed_app: Any) -> dict[str, str]:
    token, _ = authed_app.state.session_tokens.issue()
    return {"Authorization": f"Bearer {token}"}


# ══════════════════════════════════════════════════════════════════════════════
#  REST API
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, app: Any) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["data"]["status"] == "ok"
            assert body["data"]["auth_required"] is True
            assert "pairing_pending" in body["data"]
            assert "pairing_expires_in_seconds" in body["data"]

    @pytest.mark.asyncio
    async def test_health_reports_auth_required(self, authed_app: Any) -> None:
        transport = ASGITransport(app=authed_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["data"]["auth_required"] is True


class TestAddEndpoint:
    @pytest.mark.asyncio
    async def test_add_download(self, app: Any, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/add", headers=auth_headers, json={
                "url": "https://example.com/file.zip",
                "filename": "file.zip",
                "priority": "high",
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert "download_id" in body["data"]

    @pytest.mark.asyncio
    async def test_add_minimal(self, app: Any, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/add", headers=auth_headers, json={
                "url": "https://example.com/file.zip",
            })
            assert resp.status_code == 200
            assert resp.json()["success"] is True

    @pytest.mark.asyncio
    async def test_add_rejects_unsupported_scheme(self, app: Any, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/add", headers=auth_headers, json={
                "url": "data:image/png;base64,AAAA",
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is False
            assert "Unsupported URL scheme" in body["error"]

    @pytest.mark.asyncio
    async def test_add_blocked_by_disabled_site_policy(self, app: Any, mock_engine: MagicMock, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/add", headers=auth_headers, json={
                "url": "https://cdn.example.com/file.zip",
                "page_url": "https://example.com/generate-link",
                "disabled_sites": ["example.com"],
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is False
            assert "disabled" in body["error"].lower()
            mock_engine.add_download.assert_not_called()


class TestStatusEndpoint:
    @pytest.mark.asyncio
    async def test_status_found(self, app: Any, mock_storage: Any, auth_headers: dict[str, str]) -> None:
        dl_id = await mock_storage.add_download(
            url="http://x.com/f.zip", filename="f.zip", save_path="/f.zip",
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/status/{dl_id}", headers=auth_headers)
            assert resp.status_code == 200
            body = resp.json()
            assert body["data"]["filename"] == "f.zip"

    @pytest.mark.asyncio
    async def test_status_not_found(self, app: Any, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/status/nonexistent", headers=auth_headers)
            assert resp.status_code == 404


class TestListEndpoint:
    @pytest.mark.asyncio
    async def test_list_downloads(self, app: Any, mock_storage: Any, auth_headers: dict[str, str]) -> None:
        await mock_storage.add_download(
            url="http://x.com/a.zip", filename="a.zip", save_path="/a",
        )
        await mock_storage.add_download(
            url="http://x.com/b.zip", filename="b.zip", save_path="/b",
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/downloads", headers=auth_headers)
            assert resp.status_code == 200
            body = resp.json()
            assert len(body["data"]["downloads"]) == 2
            assert body["data"]["total"] == 2


class TestPauseResumeCancel:
    @pytest.mark.asyncio
    async def test_pause(self, app: Any, mock_engine: MagicMock, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/pause", headers=auth_headers, json={"download_id": "dl-1"})
            assert resp.status_code == 200
            mock_engine.pause.assert_called_once_with("dl-1")

    @pytest.mark.asyncio
    async def test_resume(self, app: Any, mock_engine: MagicMock, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/resume", headers=auth_headers, json={"download_id": "dl-1"})
            assert resp.status_code == 200
            mock_engine.resume.assert_called_once_with("dl-1")

    @pytest.mark.asyncio
    async def test_cancel(self, app: Any, mock_engine: MagicMock, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/cancel", headers=auth_headers, json={"download_id": "dl-1"})
            assert resp.status_code == 200
            mock_engine.cancel.assert_called_once_with("dl-1")


class TestConfigEndpoint:
    @pytest.mark.asyncio
    async def test_config(self, app: Any, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/config", headers=auth_headers)
            assert resp.status_code == 200
            body = resp.json()
            assert "max_concurrent" in body["data"]


class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_no_token_rejected(self, authed_app: Any) -> None:
        transport = ASGITransport(app=authed_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/downloads")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self, authed_app: Any) -> None:
        transport = ASGITransport(app=authed_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/downloads",
                headers={"Authorization": "Bearer wrong"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_correct_token_accepted(self, authed_app: Any, authed_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=authed_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/config",
                headers=authed_headers,
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_bypasses_auth(self, authed_app: Any) -> None:
        transport = ASGITransport(app=authed_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/health")
            assert resp.status_code == 200


class TestTokenPairingSecurity:
    @pytest.mark.asyncio
    async def test_pairing_code_cannot_be_reused_after_first_use(
        self,
        mock_engine: MagicMock,
        mock_storage: Any,
    ) -> None:
        pairing_app = create_app(
            engine=mock_engine,
            storage=mock_storage,
            config={"server": {}},
        )

        transport = ASGITransport(app=pairing_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pairing_code = str(pairing_app.state.pairing_code)

            first = await client.post(
                "/api/pair",
                json={"pairing_code": pairing_code},
            )
            assert first.status_code == 200
            first_body = first.json()
            assert first_body["success"] is True
            assert first_body["data"]["session_token"]

            second = await client.post(
                "/api/pair",
                json={"pairing_code": pairing_code},
            )
            assert second.status_code == 403
            second_body = second.json()
            assert second_body["success"] is False


class TestDebugEndpoints:
    @pytest.mark.asyncio
    async def test_rate_limit_debug_endpoint(self, app: Any, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/debug/rate-limit", headers=auth_headers)
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert "tokens" in body["data"]

    @pytest.mark.asyncio
    async def test_network_debug_endpoint(self, app: Any, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/debug/network", headers=auth_headers)
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert "connector" in body["data"]

    @pytest.mark.asyncio
    async def test_logging_debug_endpoint(self, app: Any, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/debug/logging", headers=auth_headers)
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert "idm.server.api" in body["data"]["levels"]

    @pytest.mark.asyncio
    async def test_set_logging_level_endpoint(self, app: Any, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/debug/logging",
                headers=auth_headers,
                json={"logger": "idm.server.api", "level": "DEBUG"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["data"]["level"] == "DEBUG"


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_rate_limit_blocks_burst(self, mock_engine: MagicMock, mock_storage: Any) -> None:
        limited_app = create_app(
            engine=mock_engine,
            storage=mock_storage,
            config={"server": {"rate_limit": {"requests_per_second": 1.0, "burst_size": 1}}},
        )

        transport = ASGITransport(app=limited_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token, _ = limited_app.state.session_tokens.issue()
            headers = {"Authorization": f"Bearer {token}"}
            first = await client.get("/api/config", headers=headers)
            second = await client.get("/api/config", headers=headers)

            assert first.status_code == 200
            assert second.status_code == 429


class TestSavePathBoundary:
    @pytest.mark.asyncio
    async def test_add_rejects_path_traversal_save_path(self, app: Any, mock_engine: MagicMock, auth_headers: dict[str, str]) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/add",
                headers=auth_headers,
                json={
                    "url": "https://example.com/file.zip",
                    "filename": "file.zip",
                    "save_path": "../../etc/passwd",
                },
            )

            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is False
            assert "outside allowed download roots" in body["error"]
            mock_engine.add_download.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET CONNECTION MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class TestConnectionManager:
    def test_initial_state(self) -> None:
        mgr = ConnectionManager()
        assert mgr.connection_count == 0

    @pytest.mark.asyncio
    async def test_broadcast_no_connections(self) -> None:
        mgr = ConnectionManager()
        # Should not raise
        await mgr.broadcast({"type": "test"})

    @pytest.mark.asyncio
    async def test_broadcast_progress_rate_limited(self) -> None:
        mgr = ConnectionManager(progress_interval=1.0)
        # No connections — just test rate limiting logic
        await mgr.broadcast_progress("dl-1", 100, 1000, 50.0, 10.0)
        # Second call within interval should be skipped (no error)
        await mgr.broadcast_progress("dl-1", 200, 1000, 55.0, 9.0)

    @pytest.mark.asyncio
    async def test_broadcast_complete_cleans_tracker(self) -> None:
        mgr = ConnectionManager()
        mgr._last_progress["dl-1"] = 100.0
        await mgr.broadcast_complete("dl-1")
        assert "dl-1" not in mgr._last_progress


class TestWebSocketCallbacks:
    def test_callbacks_instantiation(self) -> None:
        mgr = ConnectionManager()
        cb = WebSocketCallbacks(mgr)
        assert cb._manager is mgr
