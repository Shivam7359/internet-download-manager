"""Targeted redirect-resolution and /api/add interception flow tests."""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from typing import Any
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.downloader import ResolvedUrlMetadata, resolve_url_metadata
from server.api import create_app


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

    mgr = StorageManager(tmp_path / "test-resolve-flow.db")
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
def auth_headers(app: Any) -> dict[str, str]:
    token, _ = app.state.session_tokens.issue()
    return {"Authorization": f"Bearer {token}"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def redirect_test_server() -> AsyncGenerator[str, None]:
    async def dl_handler(_request: web.Request) -> web.Response:
        raise web.HTTPFound(location="/files/real-file.zip")

    async def file_handler(request: web.Request) -> web.Response:
        headers = {
            "Content-Type": "application/zip",
            "Content-Disposition": 'attachment; filename="real-file.zip"',
            "Accept-Ranges": "bytes",
            "Content-Length": "12345",
        }
        if request.method == "HEAD":
            return web.Response(status=200, headers=headers)
        if request.headers.get("Range"):
            partial_headers = dict(headers)
            partial_headers["Content-Range"] = "bytes 0-0/12345"
            return web.Response(status=206, headers=partial_headers, body=b"x")
        return web.Response(status=200, headers=headers, body=b"zip-body")

    async def html_handler(_request: web.Request) -> web.Response:
        return web.Response(status=200, headers={"Content-Type": "text/html; charset=utf-8"}, text="<html/>" )

    async def head_blocked_handler(request: web.Request) -> web.Response:
        if request.method == "HEAD":
            return web.Response(status=405, headers={"Allow": "GET"})
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="fallback.bin"',
            "Accept-Ranges": "bytes",
            "Content-Range": "bytes 0-0/4096",
            "Content-Length": "1",
        }
        return web.Response(status=206, headers=headers, body=b"x")

    app = web.Application()
    app.router.add_route("*", "/dl.php", dl_handler)
    app.router.add_route("*", "/files/real-file.zip", file_handler)
    app.router.add_route("*", "/landing", html_handler)
    app.router.add_route("*", "/head-blocked", head_blocked_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base = f"http://127.0.0.1:{port}"
    try:
        yield base
    finally:
        await runner.cleanup()


class TestRedirectResolverFlow:
    @pytest.mark.asyncio
    async def test_resolve_redirect_handler_to_binary_file(self, redirect_test_server: str) -> None:
        result = await resolve_url_metadata(f"{redirect_test_server}/dl.php")

        assert result.verified is True
        assert result.redirected is True
        assert result.is_binary is True
        assert result.is_html_page is False
        assert result.verification_method == "head"
        assert result.final_url.endswith("/files/real-file.zip")
        assert result.filename == "real-file.zip"

    @pytest.mark.asyncio
    async def test_resolve_html_page_is_marked_html(self, redirect_test_server: str) -> None:
        result = await resolve_url_metadata(f"{redirect_test_server}/landing")

        assert result.verified is True
        assert result.is_html_page is True
        assert result.is_binary is False
        assert result.verification_method == "head"

    @pytest.mark.asyncio
    async def test_resolve_falls_back_to_get_when_head_blocked(self, redirect_test_server: str) -> None:
        result = await resolve_url_metadata(f"{redirect_test_server}/head-blocked")

        assert result.verified is True
        assert result.is_binary is True
        assert result.verification_method == "get-probe"
        assert result.filename == "fallback.bin"
        assert "GET probe" in result.warning


class TestApiAddResolveInterceptorFlow:
    @pytest.mark.asyncio
    async def test_resolve_endpoint_exposes_verification_method(
        self,
        app: Any,
        auth_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_resolve(*_args: Any, **_kwargs: Any) -> ResolvedUrlMetadata:
            return ResolvedUrlMetadata(
                requested_url="https://example.com/dl.php?id=9",
                final_url="https://cdn.example.com/releases/tool-v1.2.5.zip",
                filename="tool-v1.2.5.zip",
                content_type="application/zip",
                content_disposition='attachment; filename="tool-v1.2.5.zip"',
                content_length=4096,
                resume_supported=True,
                redirected=True,
                is_html_page=False,
                is_binary=True,
                verified=True,
                verification_method="head",
            )

        monkeypatch.setattr("server.api.resolve_url_metadata", fake_resolve)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/resolve",
                headers=auth_headers,
                json={"url": "https://example.com/dl.php?id=9"},
            )

        body = resp.json()
        assert resp.status_code == 200
        assert body["success"] is True
        assert body["data"]["verificationMethod"] == "head"

    @pytest.mark.asyncio
    async def test_add_interceptor_receives_resolved_url_and_filename(
        self,
        app: Any,
        auth_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, str] = {}

        async def fake_resolve(*_args: Any, **_kwargs: Any) -> ResolvedUrlMetadata:
            return ResolvedUrlMetadata(
                requested_url="https://example.com/dl.php?id=7",
                final_url="https://cdn.example.com/releases/tool-v1.2.3.zip",
                filename="tool-v1.2.3.zip",
                content_type="application/zip",
                content_disposition='attachment; filename="tool-v1.2.3.zip"',
                content_length=1024,
                resume_supported=True,
                redirected=True,
                is_html_page=False,
                is_binary=True,
                verified=True,
            )

        async def interceptor(req: Any) -> dict[str, str]:
            captured["url"] = str(req.url)
            captured["filename"] = str(req.filename or "")
            return {"download_id": "intercepted-001"}

        monkeypatch.setattr("server.api.resolve_url_metadata", fake_resolve)
        app.state.add_download_interceptor = interceptor

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/add",
                headers=auth_headers,
                json={"url": "https://example.com/dl.php?id=7"},
            )

        body = resp.json()
        assert resp.status_code == 200
        assert body["success"] is True
        assert body["data"]["download_id"] == "intercepted-001"
        assert captured["url"] == "https://cdn.example.com/releases/tool-v1.2.3.zip"
        assert captured["filename"] == "tool-v1.2.3.zip"

    @pytest.mark.asyncio
    async def test_add_engine_receives_resolved_url_when_no_interceptor(
        self,
        app: Any,
        mock_engine: MagicMock,
        auth_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_resolve(*_args: Any, **_kwargs: Any) -> ResolvedUrlMetadata:
            return ResolvedUrlMetadata(
                requested_url="https://example.com/dl.php?id=8",
                final_url="https://cdn.example.com/releases/tool-v1.2.4.zip",
                filename="tool-v1.2.4.zip",
                content_type="application/zip",
                content_disposition='attachment; filename="tool-v1.2.4.zip"',
                content_length=2048,
                resume_supported=True,
                redirected=True,
                is_html_page=False,
                is_binary=True,
                verified=True,
            )

        monkeypatch.setattr("server.api.resolve_url_metadata", fake_resolve)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/add",
                headers=auth_headers,
                json={"url": "https://example.com/dl.php?id=8"},
            )

        body = resp.json()
        assert resp.status_code == 200
        assert body["success"] is True

        kwargs = mock_engine.add_download.await_args.kwargs
        assert kwargs["url"] == "https://cdn.example.com/releases/tool-v1.2.4.zip"
        assert kwargs["filename"] == "tool-v1.2.4.zip"
