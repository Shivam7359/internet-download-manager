"""Unit tests for core/torrent.py."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.torrent as torrent_mod
from core.torrent import TorrentManager


class _FakeHandle:
    def __init__(self, info_hash: str = "fakehash") -> None:
        self._info_hash = info_hash
        self.paused = False

    def info_hash(self) -> str:
        return self._info_hash

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def status(self):
        return SimpleNamespace(
            name="sample",
            state=3,
            progress=0.5,
            download_rate=1024,
            upload_rate=256,
            num_peers=4,
            total_wanted=1000,
            total_done=500,
            is_seeding=False,
            save_path="/tmp",
        )

    def get_torrent_info(self):
        return SimpleNamespace(name=lambda: "sample")


class _FakeSession:
    delete_files = 1

    def __init__(self, settings):
        self.settings = settings
        self.removed = []

    def add_torrent(self, _params):
        return _FakeHandle()

    def remove_torrent(self, handle, flags):
        self.removed.append((handle, flags))

    def pop_alerts(self):
        return []


class _FakeLt:
    class alert:
        class category_t:
            status_notification = 1
            storage_notification = 2
            error_notification = 4

    add_torrent_alert = type("add_torrent_alert", (), {})
    torrent_error_alert = type("torrent_error_alert", (), {})
    metadata_received_alert = type("metadata_received_alert", (), {})
    state_changed_alert = type("state_changed_alert", (), {})
    session = _FakeSession

    @staticmethod
    def parse_magnet_uri(_uri):
        return SimpleNamespace(save_path="", resume_data=None)

    @staticmethod
    def torrent_info(_path):
        return {"ok": True}


@pytest.mark.asyncio
async def test_start_raises_when_libtorrent_missing(monkeypatch) -> None:
    monkeypatch.setattr(torrent_mod, "lt", None)
    manager = TorrentManager({})
    with pytest.raises(RuntimeError, match="BitTorrent support is unavailable"):
        await manager.start()


@pytest.mark.asyncio
async def test_add_magnet_initializes_session_and_returns_hash(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(torrent_mod, "lt", _FakeLt)
    manager = TorrentManager({})

    info_hash = await manager.add_magnet("magnet:?xt=urn:btih:abc", str(tmp_path / "x.bin"))
    assert info_hash == "fakehash"

    await manager.stop()


@pytest.mark.asyncio
async def test_get_status_none_for_unknown_torrent(monkeypatch) -> None:
    monkeypatch.setattr(torrent_mod, "lt", _FakeLt)
    manager = TorrentManager({})
    assert manager.get_status("missing") is None


@pytest.mark.asyncio
async def test_remove_ignores_when_session_not_running(monkeypatch) -> None:
    monkeypatch.setattr(torrent_mod, "lt", _FakeLt)
    manager = TorrentManager({})
    manager._handles["fakehash"] = _FakeHandle()

    await manager.remove("fakehash", delete_files=True)
    assert "fakehash" not in manager._handles
