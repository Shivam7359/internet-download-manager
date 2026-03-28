"""
IDM Utils — Media Extractor
============================
Detects and extracts video/audio stream links using yt-dlp.

Features:
    • Support for YouTube, Vimeo, Dailymotion, and 1000+ sites
    • Resolves M3U8, DASH, and direct MP4/WebM links
    • Automatic format selection (best quality / best video + audio)
    • Metadata extraction (title, duration, thumbnail)
    • Integration with the main DownloadEngine
"""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from typing import Any, Optional, Dict, List

log = logging.getLogger("idm.utils.media_extractor")

try:
    import yt_dlp
except ImportError:
    yt_dlp = None
    log.warning("yt-dlp not found or failed to load. "
                "Media extraction is disabled.")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  MEDIA EXTRACTOR                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class MediaExtractor:
    """
    Extracts direct download links from video sharing sites.

    Uses yt-dlp to find the best available direct links for a given URL.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._logger = logging.getLogger("yt-dlp")
        self._logger.setLevel(logging.WARNING)
        perf = config.get("performance", {})
        self._cache_ttl_seconds = int(perf.get("media_info_cache_ttl_seconds", 3600))
        self._cache_max_entries = int(perf.get("media_info_cache_max_entries", 256))
        self._info_cache: dict[str, tuple[float, Dict[str, Any]]] = {}

    def _get_cached_info(self, url: str) -> Optional[Dict[str, Any]]:
        if self._cache_ttl_seconds <= 0:
            return None

        cached = self._info_cache.get(url)
        if not cached:
            return None

        expires_at, payload = cached
        if time.time() >= expires_at:
            self._info_cache.pop(url, None)
            return None

        return deepcopy(payload)

    def _set_cached_info(self, url: str, info: Dict[str, Any]) -> None:
        if self._cache_ttl_seconds <= 0:
            return

        if len(self._info_cache) >= self._cache_max_entries:
            # Remove the oldest entry to keep memory bounded.
            oldest_url = min(self._info_cache.items(), key=lambda item: item[1][0])[0]
            self._info_cache.pop(oldest_url, None)

        self._info_cache[url] = (time.time() + self._cache_ttl_seconds, deepcopy(info))

    async def get_info(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Fetch metadata and available formats for a URL.

        Returns:
            A dictionary containing info like 'title', 'formats', 'thumbnail',
            or None if extraction fails.
        """
        loop = asyncio.get_running_loop()
        cached_info = self._get_cached_info(url)
        if cached_info is not None:
            return cached_info

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestvideo+bestaudio/best",
            "logger": self._logger,
        }

        try:
            # yt-dlp info extraction is blocking, so run in executor
            info = await loop.run_in_executor(
                None,
                lambda: self._extract_info(url, ydl_opts)
            )
            if info is not None:
                self._set_cached_info(url, info)
            return info
        except Exception as e:
            log.error(f"Failed to extract info from {url}: {e}")
            return None

    def _extract_info(self, url: str, opts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None

                # Clean up the formats list for easier selection
                formats = []
                for f in info.get("formats", []):
                    # We only care about typical video/audio formats
                    if f.get("vcodec") != "none" or f.get("acodec") != "none":
                        formats.append({
                            "format_id": f.get("format_id"),
                            "ext": f.get("ext"),
                            "resolution": f.get("resolution"),
                            "filesize": f.get("filesize") or f.get("filesize_approx"),
                            "height": f.get("height"),
                            "width": f.get("width"),
                            "fps": f.get("fps"),
                            "tbr": f.get("tbr"),
                            "vcodec": f.get("vcodec"),
                            "acodec": f.get("acodec"),
                            "url": f.get("url"),
                            "note": f.get("format_note"),
                        })

                return {
                    "id": info.get("id"),
                    "title": info.get("title"),
                    "description": info.get("description"),
                    "thumbnail": info.get("thumbnail"),
                    "duration": info.get("duration"),
                    "uploader": info.get("uploader"),
                    "ext": info.get("ext"),
                    "formats": formats,
                    "original_url": url,
                    "webpage_url": info.get("webpage_url"),
                }
            except Exception as e:
                log.error(f"yt-dlp error: {e}")
                return None

    async def get_direct_url(self, url: str) -> Optional[str]:
        """
        Quickly get the best direct download URL.
        """
        info = await self.get_info(url)
        if not info:
            return None

        # Return the 'url' from the base info (usually the best selected format)
        # or find the first format with a URL
        best_url = info.get("url")
        if not best_url and info.get("formats"):
            # If no top-level URL, take the last format in the list (usually best)
            best_url = info["formats"][-1].get("url")

        return best_url

    @staticmethod
    def is_supported(url: str) -> bool:
        """
        Check if the URL is likely supported by yt-dlp.
        Simplified check for common sites.
        """
        common_sites = [
            "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
            "twitter.com", "tiktok.com", "instagram.com", "facebook.com",
            "twitch.tv", "vk.com", "bilibili.com"
        ]
        return any(site in url.lower() for site in common_sites)
