"""
Unit tests for utils/ — categoriser, scheduler, clipboard_monitor.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.categoriser import (
    categorise_file,
    categorise_by_mime,
    categorise,
    get_category_directory,
    get_all_extensions,
    ALL_CATEGORIES,
)
from utils.scheduler import ScheduleRule, DownloadScheduler
from utils.clipboard_monitor import (
    ClipboardMonitor,
    extract_urls,
    is_downloadable_url,
)


# ══════════════════════════════════════════════════════════════════════════════
#  CATEGORISER
# ══════════════════════════════════════════════════════════════════════════════

class TestCategoriseFile:
    def test_video(self) -> None:
        assert categorise_file("movie.mp4") == "Video"
        assert categorise_file("clip.mkv") == "Video"
        assert categorise_file("stream.webm") == "Video"

    def test_audio(self) -> None:
        assert categorise_file("song.mp3") == "Audio"
        assert categorise_file("track.flac") == "Audio"

    def test_image(self) -> None:
        assert categorise_file("photo.jpg") == "Image"
        assert categorise_file("icon.png") == "Image"
        assert categorise_file("anim.gif") == "Image"

    def test_document(self) -> None:
        assert categorise_file("report.pdf") == "Document"
        assert categorise_file("data.xlsx") == "Document"
        assert categorise_file("notes.txt") == "Document"

    def test_software(self) -> None:
        assert categorise_file("setup.exe") == "Software"
        assert categorise_file("installer.msi") == "Software"
        assert categorise_file("app.apk") == "Software"

    def test_archive(self) -> None:
        assert categorise_file("backup.zip") == "Archive"
        assert categorise_file("files.7z") == "Archive"
        assert categorise_file("data.tar.gz") == "Archive"
        assert categorise_file("pkg.tar.bz2") == "Archive"

    def test_other(self) -> None:
        assert categorise_file("unknown.xyz") == "Other"
        assert categorise_file("noext") == "Other"

    def test_case_insensitive(self) -> None:
        assert categorise_file("VIDEO.MP4") == "Video"
        assert categorise_file("Photo.JPG") == "Image"

    def test_full_path(self) -> None:
        assert categorise_file("/downloads/video/movie.mp4") == "Video"


class TestCategoriseByMime:
    def test_video_mime(self) -> None:
        assert categorise_by_mime("video/mp4") == "Video"
        assert categorise_by_mime("video/x-matroska") == "Video"

    def test_audio_mime(self) -> None:
        assert categorise_by_mime("audio/mpeg") == "Audio"

    def test_image_mime(self) -> None:
        assert categorise_by_mime("image/png") == "Image"

    def test_document_mime(self) -> None:
        assert categorise_by_mime("application/pdf") == "Document"

    def test_archive_mime(self) -> None:
        assert categorise_by_mime("application/zip") == "Archive"

    def test_with_params(self) -> None:
        assert categorise_by_mime("text/html; charset=utf-8") == "Document"

    def test_empty(self) -> None:
        assert categorise_by_mime("") == "Other"

    def test_unknown(self) -> None:
        assert categorise_by_mime("application/octet-stream") == "Other"


class TestCategoriseCombined:
    def test_extension_takes_precedence(self) -> None:
        result = categorise("file.mp4", "application/octet-stream")
        assert result == "Video"

    def test_falls_back_to_mime(self) -> None:
        result = categorise("file.unknown", "video/mp4")
        assert result == "Video"

    def test_both_unknown(self) -> None:
        result = categorise("file.xyz", "application/octet-stream")
        assert result == "Other"


class TestCategoryDirectory:
    def test_default_directory(self) -> None:
        config = {"general": {"download_directory": "/dl"}}
        path = get_category_directory(config, "Video")
        assert path == Path("/dl/Video")

    def test_custom_directory(self) -> None:
        config = {
            "general": {"download_directory": "/dl"},
            "categories": {"video": {"directory": "/custom/videos"}},
        }
        path = get_category_directory(config, "Video")
        assert path == Path("/custom/videos")


class TestGetAllExtensions:
    def test_video(self) -> None:
        exts = get_all_extensions("Video")
        assert ".mp4" in exts
        assert ".mkv" in exts

    def test_unknown(self) -> None:
        assert get_all_extensions("Unknown") == frozenset()

    def test_all_categories_defined(self) -> None:
        assert len(ALL_CATEGORIES) == 7


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

class TestScheduleRule:
    def test_normal_window_active(self) -> None:
        rule = ScheduleRule(
            start_time=dt_time(9, 0),
            end_time=dt_time(17, 0),
        )
        noon = datetime(2026, 3, 18, 12, 0)
        assert rule.is_active(noon) is True

    def test_normal_window_inactive(self) -> None:
        rule = ScheduleRule(
            start_time=dt_time(9, 0),
            end_time=dt_time(17, 0),
        )
        late = datetime(2026, 3, 18, 22, 0)
        assert rule.is_active(late) is False

    def test_overnight_window(self) -> None:
        rule = ScheduleRule(
            start_time=dt_time(22, 0),
            end_time=dt_time(6, 0),
        )
        assert rule.is_overnight is True
        # 23:00 should be active
        assert rule.is_active(datetime(2026, 3, 18, 23, 0)) is True
        # 3:00 should be active
        assert rule.is_active(datetime(2026, 3, 18, 3, 0)) is True
        # 12:00 should be inactive
        assert rule.is_active(datetime(2026, 3, 18, 12, 0)) is False

    def test_weekday_filter(self) -> None:
        rule = ScheduleRule(
            start_time=dt_time(0, 0),
            end_time=dt_time(23, 59),
            days=[0, 1, 2, 3, 4],  # Mon–Fri
        )
        monday = datetime(2026, 3, 16, 12, 0)     # Monday
        saturday = datetime(2026, 3, 21, 12, 0)    # Saturday
        assert rule.is_active(monday) is True
        assert rule.is_active(saturday) is False

    def test_disabled_rule(self) -> None:
        rule = ScheduleRule(
            start_time=dt_time(0, 0),
            end_time=dt_time(23, 59),
            enabled=False,
        )
        assert rule.is_active() is False

    def test_from_dict(self) -> None:
        rule = ScheduleRule.from_dict({
            "start_time": "02:00",
            "end_time": "06:30",
            "days": [5, 6],
            "enabled": True,
        })
        assert rule.start_time == dt_time(2, 0)
        assert rule.end_time == dt_time(6, 30)
        assert rule.days == [5, 6]


class TestDownloadScheduler:
    def test_disabled_always_allows(self) -> None:
        scheduler = DownloadScheduler({"scheduler": {"enabled": False}})
        assert scheduler.is_within_window() is True

    def test_enabled_with_matching_rule(self) -> None:
        scheduler = DownloadScheduler({
            "scheduler": {
                "enabled": True,
                "rules": [
                    {"start_time": "00:00", "end_time": "23:59", "enabled": True}
                ],
            }
        })
        assert scheduler.is_within_window() is True

    def test_override_allows(self) -> None:
        scheduler = DownloadScheduler({
            "scheduler": {
                "enabled": True,
                "rules": [
                    {"start_time": "03:00", "end_time": "04:00", "enabled": True}
                ],
            }
        })
        scheduler.set_override(True)
        assert scheduler.is_within_window() is True

    def test_override_blocks(self) -> None:
        scheduler = DownloadScheduler({"scheduler": {"enabled": False}})
        scheduler.set_override(False)
        assert scheduler.is_within_window() is False

    def test_clear_override(self) -> None:
        scheduler = DownloadScheduler({"scheduler": {"enabled": False}})
        scheduler.set_override(False)
        assert scheduler.is_within_window() is False
        scheduler.set_override(None)
        assert scheduler.is_within_window() is True  # disabled = always allow

    def test_add_and_clear_rules(self) -> None:
        scheduler = DownloadScheduler({"scheduler": {"enabled": True}})
        scheduler.add_rule(ScheduleRule())
        assert len(scheduler.rules) > 0
        scheduler.clear_rules()
        assert len(scheduler.rules) == 0

    def test_enable_disable(self) -> None:
        scheduler = DownloadScheduler({"scheduler": {"enabled": False}})
        assert scheduler.enabled is False
        scheduler.enabled = True
        assert scheduler.enabled is True

    def test_time_until_window_rolls_to_next_day(self) -> None:
        scheduler = DownloadScheduler({
            "scheduler": {
                "enabled": True,
                "rules": [
                    {"start_time": "01:00", "end_time": "02:00", "enabled": True}
                ],
            }
        })
        now = datetime(2026, 3, 18, 23, 30)
        seconds = scheduler.time_until_window(now)
        assert seconds is not None
        assert seconds == 5400

    def test_time_until_window_handles_month_end(self) -> None:
        scheduler = DownloadScheduler({
            "scheduler": {
                "enabled": True,
                "rules": [
                    {"start_time": "00:10", "end_time": "01:00", "enabled": True}
                ],
            }
        })
        now = datetime(2026, 1, 31, 23, 50)
        seconds = scheduler.time_until_window(now)
        assert seconds is not None
        assert seconds == 1200

    @pytest.mark.asyncio
    async def test_apply_schedule_state_resumes_only_managed_pauses(self) -> None:
        scheduler = DownloadScheduler({"scheduler": {"enabled": True}})
        engine = MagicMock()
        engine.pause_all = AsyncMock(return_value={"dl-a", "dl-b"})
        engine.resume_downloads = AsyncMock()

        scheduler._was_active = True
        scheduler.is_within_window = lambda now=None: False  # type: ignore[assignment]
        await scheduler._apply_schedule_state(engine)

        assert scheduler._scheduler_paused_ids == {"dl-a", "dl-b"}
        engine.pause_all.assert_awaited_once()

        scheduler.is_within_window = lambda now=None: True  # type: ignore[assignment]
        await scheduler._apply_schedule_state(engine)

        engine.resume_downloads.assert_awaited_once_with({"dl-a", "dl-b"})
        assert scheduler._scheduler_paused_ids == set()

    @pytest.mark.asyncio
    async def test_stop_resumes_managed_downloads(self) -> None:
        scheduler = DownloadScheduler({"scheduler": {"enabled": True}})
        engine = MagicMock()
        engine.resume_downloads = AsyncMock()

        scheduler._engine = engine
        scheduler._scheduler_paused_ids = {"dl-1"}

        await scheduler.stop()

        engine.resume_downloads.assert_awaited_once_with({"dl-1"})
        assert scheduler._scheduler_paused_ids == set()


# ══════════════════════════════════════════════════════════════════════════════
#  CLIPBOARD MONITOR
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractUrls:
    def test_http_url(self) -> None:
        urls = extract_urls("check https://example.com/file.zip here")
        assert urls == ["https://example.com/file.zip"]

    def test_multiple_urls(self) -> None:
        text = "http://a.com/1.mp4 and https://b.com/2.pdf"
        urls = extract_urls(text)
        assert len(urls) == 2

    def test_ftp_url(self) -> None:
        urls = extract_urls("ftp://server.com/data.bin")
        assert len(urls) == 1

    def test_magnet_link(self) -> None:
        urls = extract_urls("magnet:?xt=urn:btih:abc123")
        assert len(urls) == 1
        assert urls[0].startswith("magnet:")

    def test_no_urls(self) -> None:
        assert extract_urls("just plain text") == []

    def test_empty(self) -> None:
        assert extract_urls("") == []

    def test_deduplication(self) -> None:
        text = "https://x.com/f.zip and https://x.com/f.zip"
        assert len(extract_urls(text)) == 1

    def test_strips_trailing_punctuation(self) -> None:
        urls = extract_urls("Visit https://example.com/file.zip.")
        assert urls[0] == "https://example.com/file.zip"


class TestIsDownloadableUrl:
    def test_downloadable_extension(self) -> None:
        assert is_downloadable_url("https://x.com/file.zip") is True
        assert is_downloadable_url("https://x.com/video.mp4") is True

    def test_not_downloadable(self) -> None:
        assert is_downloadable_url("https://x.com/page.html") is False

    def test_magnet_always_downloadable(self) -> None:
        assert is_downloadable_url("magnet:?xt=urn:btih:abc") is True

    def test_ftp_always_downloadable(self) -> None:
        assert is_downloadable_url("ftp://x.com/anything") is True

    def test_download_pattern_in_url(self) -> None:
        assert is_downloadable_url("https://x.com/download/abc") is True
        assert is_downloadable_url("https://x.com/dl/file") is True


class TestClipboardMonitor:
    def test_creation(self) -> None:
        config = {"clipboard": {"monitor_enabled": True}}
        monitor = ClipboardMonitor(config)
        assert monitor.enabled is True

    def test_disabled(self) -> None:
        config = {"clipboard": {"monitor_enabled": False}}
        monitor = ClipboardMonitor(config)
        assert monitor.enabled is False

    def test_clear_history(self) -> None:
        monitor = ClipboardMonitor({})
        monitor._seen_urls.add("http://x.com/f.zip")
        assert monitor.seen_count == 1
        monitor.clear_history()
        assert monitor.seen_count == 0

    def test_custom_extensions(self) -> None:
        config = {
            "clipboard": {
                "monitored_extensions": ["mp4", ".mkv", "pdf"],
            }
        }
        monitor = ClipboardMonitor(config)
        assert ".mp4" in monitor._extensions
        assert ".mkv" in monitor._extensions
        assert ".pdf" in monitor._extensions

    @pytest.mark.asyncio
    async def test_check_text_detects_url(self) -> None:
        detected: list[str] = []
        monitor = ClipboardMonitor(
            {},
            on_url_detected=lambda url: detected.append(url),
        )
        await monitor._check_text("Download https://x.com/file.zip now")
        assert len(detected) == 1
        assert detected[0] == "https://x.com/file.zip"

    @pytest.mark.asyncio
    async def test_duplicate_suppression(self) -> None:
        detected: list[str] = []
        monitor = ClipboardMonitor(
            {},
            on_url_detected=lambda url: detected.append(url),
        )
        await monitor._check_text("https://x.com/file.zip")
        await monitor._check_text("https://x.com/file.zip")
        assert len(detected) == 1  # second time suppressed

    @pytest.mark.asyncio
    async def test_async_callback(self) -> None:
        detected: list[str] = []

        async def callback(url: str) -> None:
            detected.append(url)

        monitor = ClipboardMonitor({}, on_url_detected=callback)
        await monitor._check_text("https://x.com/file.mp4")
        assert len(detected) == 1

    @pytest.mark.asyncio
    async def test_start_ignores_existing_clipboard_content(self) -> None:
        monitor = ClipboardMonitor({})
        monitor._read_clipboard = AsyncMock(return_value="https://x.com/file.zip")  # type: ignore[method-assign]
        monitor._poll_loop = AsyncMock()  # type: ignore[method-assign]

        await monitor.start()

        assert monitor._last_text == "https://x.com/file.zip"
        monitor._task.cancel()


# ══════════════════════════════════════════════════════════════════════════════
#  MEDIA EXTRACTOR CACHE
# ══════════════════════════════════════════════════════════════════════════════

class TestMediaExtractorCache:
    @pytest.mark.asyncio
    async def test_get_info_uses_cache(self) -> None:
        from utils.media_extractor import MediaExtractor

        config = {
            "performance": {
                "media_info_cache_ttl_seconds": 3600,
                "media_info_cache_max_entries": 16,
            }
        }
        extractor = MediaExtractor(config)

        calls = {"count": 0}

        def fake_extract(url: str, _opts: dict[str, Any]) -> dict[str, Any]:
            calls["count"] += 1
            return {"title": "cached", "formats": [], "url": url}

        extractor._extract_info = fake_extract  # type: ignore[method-assign]

        url = "https://example.com/video"
        first = await extractor.get_info(url)
        second = await extractor.get_info(url)

        assert first is not None
        assert second is not None
        assert calls["count"] == 1

    @pytest.mark.asyncio
    async def test_get_info_cache_expiry(self) -> None:
        from utils.media_extractor import MediaExtractor

        config = {
            "performance": {
                "media_info_cache_ttl_seconds": 1,
                "media_info_cache_max_entries": 16,
            }
        }
        extractor = MediaExtractor(config)

        calls = {"count": 0}

        def fake_extract(url: str, _opts: dict[str, Any]) -> dict[str, Any]:
            calls["count"] += 1
            return {"title": "cached", "formats": [], "url": url}

        extractor._extract_info = fake_extract  # type: ignore[method-assign]

        url = "https://example.com/video2"
        await extractor.get_info(url)

        # Force cache expiry without sleeping.
        if url in extractor._info_cache:
            _, payload = extractor._info_cache[url]
            extractor._info_cache[url] = (0.0, payload)

        await extractor.get_info(url)
        assert calls["count"] == 2
