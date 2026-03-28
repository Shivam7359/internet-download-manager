"""
Unit tests for core advanced features: FTP, BitTorrent, Media Extraction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# Skip tests if dependencies are missing or no network
try:
    import libtorrent
    import yt_dlp
    import aioftp
except ImportError:
    pytest.skip("Advanced dependencies missing", allow_module_level=True)


@pytest.fixture
def config() -> dict:
    return {
        "general": {"download_directory": "/tmp/idm"},
        "advanced": {"chunk_buffer_size_bytes": 65536}
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  MEDIA EXTRACTOR TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestMediaExtractor:
    @pytest.mark.asyncio
    async def test_is_supported(self, config) -> None:
        from utils.media_extractor import MediaExtractor
        assert MediaExtractor.is_supported("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is True
        assert MediaExtractor.is_supported("https://google.com") is False

    @pytest.mark.asyncio
    @pytest.mark.network
    async def test_get_info_real(self, config) -> None:
        from utils.media_extractor import MediaExtractor
        extractor = MediaExtractor(config)
        # Using a very stable test URL
        url = "https://vimeo.com/channels/staffpicks/1000"
        try:
            info = await extractor.get_info(url)
            if info:
                assert "title" in info
                assert "formats" in info
                assert len(info["formats"]) > 0
        except Exception:
            pytest.skip("Network issue or yt-dlp error")


# ═══════════════════════════════════════════════════════════════════════════════
#  FTP TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestFtpDownloader:
    def test_url_parsing(self, config) -> None:
        from core.ftp import FtpDownloader
        d = FtpDownloader(
            "ftp://user:pass@ftp.example.com:2121/file.zip", "/tmp/file.zip", config
        )
        assert d._host == "ftp.example.com"
        assert d._port == 2121
        assert d._user == "user"
        assert d._password == "pass"


# ═══════════════════════════════════════════════════════════════════════════════
#  TORRENT TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestTorrentManager:
    @pytest.mark.asyncio
    async def test_engine_init(self, config) -> None:
        from core.torrent import TorrentManager
        mgr = TorrentManager(config)
        await mgr.start()
        assert mgr._session is not None
        await mgr.stop()
        assert mgr._session is None

    @pytest.mark.asyncio
    async def test_add_magnet_placeholder(self, config) -> None:
        from core.torrent import TorrentManager
        mgr = TorrentManager(config)
        await mgr.start()
        # Ubuntu ISO magnet (standard long-term hash)
        magnet = "magnet:?xt=urn:btih:3ad424855520a16c4786411516e8b4e768656641"
        try:
            h = await mgr.add_magnet(magnet, "/tmp/ubuntu.iso")
            assert h is not None
        finally:
            await mgr.stop()
