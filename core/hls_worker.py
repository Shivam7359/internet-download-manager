"""
Authorized HLS worker for backend use.

This module implements an IDM-like HLS download worker that mimics real
playback behavior by repeatedly polling media playlists, downloading new
segments quickly, and preserving request/session headers.

Capabilities:
- Master playlist fetch + best/specified variant selection
- Media playlist polling using persistent headers/cookies/session auth
- New-segment detection via EXT-X-MEDIA-SEQUENCE
- Concurrent segment downloading with queueing + retries + timeouts
- AES-128-CBC decryption via EXT-X-KEY (with IV handling)
- Ordered segment merge into a single output file
- Optional FFmpeg remux into MP4
- Live stream stop conditions (ENDLIST or inactivity timeout)

Use only with streams you are authorized to access and record.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

import aiofiles
import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

log = logging.getLogger("idm.core.hls_worker")

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


# ----------------------------- Data Models ------------------------------------

@dataclass(slots=True)
class HLSKey:
    method: str
    uri: str
    iv: bytes | None = None


@dataclass(slots=True)
class HLSSegment:
    seq: int
    uri: str
    duration: float
    key: HLSKey | None = None


@dataclass(slots=True)
class HLSVariant:
    uri: str
    bandwidth: int
    resolution: str | None = None
    codecs: str | None = None


@dataclass(slots=True)
class MediaPlaylist:
    target_duration: float
    media_sequence: int
    endlist: bool
    segments: list[HLSSegment]


@dataclass(slots=True)
class HLSWorkerConfig:
    master_or_media_url: str
    output_path: str
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    referer: str | None = None
    origin: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    authorization: str | None = None

    # Variant selection
    variant: str = "best"  # best | worst | index:<n> | bandwidth:<bps>

    # Polling and live behavior
    playlist_refresh_factor: float = 0.6
    min_refresh_seconds: float = 1.0
    max_refresh_seconds: float = 8.0
    live_inactivity_timeout_seconds: float = 45.0

    # Segment worker behavior
    max_concurrent_segments: int = 8
    queue_maxsize: int = 300
    request_timeout_seconds: float = 20.0
    max_retries: int = 5
    retry_base_delay_seconds: float = 0.8
    retry_max_delay_seconds: float = 8.0

    # Output behavior
    merge_mode: str = "auto"  # auto | ts_concat | ffmpeg_mp4
    ffmpeg_path: str = "ffmpeg"
    keep_temp_files: bool = False

    # Optional runtime progress hook.
    progress_callback: ProgressCallback | None = None


@dataclass(slots=True)
class HLSDownloadResult:
    success: bool
    output_path: str
    selected_variant_url: str
    total_segments: int
    downloaded_segments: int
    elapsed_seconds: float
    reason: str


# ----------------------------- Parsing Helpers --------------------------------

ATTR_RE = re.compile(r"([A-Z0-9-]+)=((?:\"[^\"]*\")|[^,]*)")


def _parse_attribute_list(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key, value in ATTR_RE.findall(raw):
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        attrs[key] = value
    return attrs


def _parse_hex_iv(value: str) -> bytes:
    v = value.strip()
    if v.startswith("0x") or v.startswith("0X"):
        v = v[2:]
    if len(v) % 2 == 1:
        v = "0" + v
    return bytes.fromhex(v)


def _iv_from_media_sequence(seq: int) -> bytes:
    return int(seq).to_bytes(16, byteorder="big", signed=False)


def _is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master_playlist(text: str, base_url: str) -> list[HLSVariant]:
    variants: list[HLSVariant] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _parse_attribute_list(line.split(":", 1)[1])
            bandwidth = int(attrs.get("BANDWIDTH", "0") or "0")
            resolution = attrs.get("RESOLUTION")
            codecs = attrs.get("CODECS")

            i += 1
            while i < len(lines) and lines[i].startswith("#"):
                i += 1
            if i < len(lines):
                uri = urljoin(base_url, lines[i])
                variants.append(
                    HLSVariant(
                        uri=uri,
                        bandwidth=bandwidth,
                        resolution=resolution,
                        codecs=codecs,
                    )
                )
        i += 1

    return variants


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    target_duration = 4.0
    media_sequence = 0
    endlist = False

    current_key: HLSKey | None = None
    current_duration = 0.0
    segments: list[HLSSegment] = []

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for line in lines:
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = float(line.split(":", 1)[1].strip())
            continue

        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1].strip())
            continue

        if line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attribute_list(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE")
            if method == "NONE":
                current_key = None
                continue

            key_uri = attrs.get("URI", "")
            iv_raw = attrs.get("IV")
            iv = _parse_hex_iv(iv_raw) if iv_raw else None

            if key_uri:
                current_key = HLSKey(
                    method=method,
                    uri=urljoin(base_url, key_uri),
                    iv=iv,
                )
            continue

        if line.startswith("#EXTINF:"):
            raw_dur = line.split(":", 1)[1].split(",", 1)[0]
            current_duration = float(raw_dur)
            continue

        if line.startswith("#EXT-X-ENDLIST"):
            endlist = True
            continue

        if line.startswith("#"):
            continue

        seq = media_sequence + len(segments)
        segments.append(
            HLSSegment(
                seq=seq,
                uri=urljoin(base_url, line),
                duration=current_duration,
                key=current_key,
            )
        )

    return MediaPlaylist(
        target_duration=target_duration,
        media_sequence=media_sequence,
        endlist=endlist,
        segments=segments,
    )


def select_variant(variants: list[HLSVariant], variant_rule: str) -> HLSVariant:
    if not variants:
        raise ValueError("No variants found in master playlist")

    rule = (variant_rule or "best").strip().lower()

    if rule == "best":
        return max(variants, key=lambda v: v.bandwidth)
    if rule == "worst":
        return min(variants, key=lambda v: v.bandwidth)

    if rule.startswith("index:"):
        idx = int(rule.split(":", 1)[1])
        if idx < 0 or idx >= len(variants):
            raise ValueError(f"Variant index out of range: {idx}")
        return variants[idx]

    if rule.startswith("bandwidth:"):
        target = int(rule.split(":", 1)[1])
        return min(variants, key=lambda v: abs(v.bandwidth - target))

    raise ValueError(f"Unknown variant selection rule: {variant_rule}")


# --------------------------- AES Decryption -----------------------------------

class AES128Decryptor:
    def __init__(self) -> None:
        self._cache: dict[str, bytes] = {}

    async def get_key(
        self,
        session: aiohttp.ClientSession,
        key_url: str,
        timeout: aiohttp.ClientTimeout,
        retries: int,
        retry_base_delay_seconds: float,
        retry_max_delay_seconds: float,
    ) -> bytes:
        if key_url in self._cache:
            return self._cache[key_url]

        attempt = 0
        while True:
            attempt += 1
            try:
                async with session.get(key_url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    key = await resp.read()
                if len(key) != 16:
                    raise ValueError(f"AES-128 key must be 16 bytes, got {len(key)}")
                self._cache[key_url] = key
                return key
            except Exception:
                if attempt >= retries:
                    raise
                delay = min(retry_max_delay_seconds, retry_base_delay_seconds * (2 ** (attempt - 1)))
                await asyncio.sleep(delay)

    @staticmethod
    def decrypt_aes128_cbc(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        unpadder = PKCS7(algorithms.AES.block_size).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        return plaintext


# ------------------------------ Worker ----------------------------------------

class HLSDownloadWorker:
    def __init__(self, config: HLSWorkerConfig) -> None:
        self.config = config

        self._segment_queue: asyncio.PriorityQueue[tuple[int, HLSSegment]] = asyncio.PriorityQueue(
            maxsize=max(10, self.config.queue_maxsize)
        )
        self._enqueued_sequences: set[int] = set()
        self._downloaded_sequences: set[int] = set()
        self._failed_sequences: set[int] = set()
        self._segment_attempts: dict[int, int] = {}

        self._segments_dir = Path(f"{self.config.output_path}.segments")
        self._partial_output = Path(f"{self.config.output_path}.part.ts")
        self._stop = asyncio.Event()
        self._endlist_seen = False
        self._last_new_segment_at = time.monotonic()
        self._selected_media_playlist_url = ""

        self._session: aiohttp.ClientSession | None = None
        self._decryptor = AES128Decryptor()
        self._started_at = time.monotonic()
        self._bytes_downloaded = 0

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
        if self.config.referer:
            headers["Referer"] = self.config.referer
        if self.config.origin:
            headers["Origin"] = self.config.origin
        if self.config.authorization:
            headers["Authorization"] = self.config.authorization

        headers.update(self.config.headers)
        return headers

    def _build_timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            total=self.config.request_timeout_seconds,
            connect=min(10.0, self.config.request_timeout_seconds),
            sock_read=self.config.request_timeout_seconds,
            sock_connect=min(10.0, self.config.request_timeout_seconds),
        )

    async def _fetch_text(self, url: str) -> str:
        assert self._session is not None

        last_exc: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                async with self._session.get(url, timeout=self._build_timeout()) as resp:
                    resp.raise_for_status()
                    return await resp.text()
            except Exception as exc:
                last_exc = exc
                if attempt >= self.config.max_retries:
                    break
                delay = min(
                    self.config.retry_max_delay_seconds,
                    self.config.retry_base_delay_seconds * (2 ** (attempt - 1)),
                )
                log.warning(
                    "Fetch text failed (attempt %d/%d): %s",
                    attempt,
                    self.config.max_retries,
                    exc,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"Failed fetching URL after retries: {url}") from last_exc

    async def _fetch_bytes(self, url: str) -> bytes:
        assert self._session is not None

        last_exc: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                async with self._session.get(url, timeout=self._build_timeout()) as resp:
                    resp.raise_for_status()
                    return await resp.read()
            except Exception as exc:
                last_exc = exc
                if attempt >= self.config.max_retries:
                    break
                delay = min(
                    self.config.retry_max_delay_seconds,
                    self.config.retry_base_delay_seconds * (2 ** (attempt - 1)),
                )
                log.warning(
                    "Fetch bytes failed (attempt %d/%d): %s",
                    attempt,
                    self.config.max_retries,
                    exc,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"Failed fetching bytes after retries: {url}") from last_exc

    async def _resolve_media_playlist_url(self, entry_url: str) -> str:
        text = await self._fetch_text(entry_url)
        if not _is_master_playlist(text):
            return entry_url

        variants = parse_master_playlist(text, entry_url)
        selected = select_variant(variants, self.config.variant)
        log.info(
            "Selected variant bandwidth=%s resolution=%s url=%s",
            selected.bandwidth,
            selected.resolution,
            selected.uri,
        )
        return selected.uri

    async def _poll_media_playlist(self, media_url: str) -> None:
        while not self._stop.is_set():
            text = await self._fetch_text(media_url)
            playlist = parse_media_playlist(text, media_url)

            new_count = 0
            for seg in playlist.segments:
                if seg.seq in self._enqueued_sequences or seg.seq in self._downloaded_sequences:
                    continue

                await self._segment_queue.put((seg.seq, seg))
                self._enqueued_sequences.add(seg.seq)
                new_count += 1

            if new_count > 0:
                self._last_new_segment_at = time.monotonic()
                log.info("Enqueued %d new segments, queue=%d", new_count, self._segment_queue.qsize())
                await self._emit_progress()

            self._endlist_seen = playlist.endlist

            if self._endlist_seen:
                log.info("EXT-X-ENDLIST seen, waiting for queue to drain")
                await self._emit_progress()
                return

            inactive_for = time.monotonic() - self._last_new_segment_at
            if inactive_for >= self.config.live_inactivity_timeout_seconds:
                log.warning(
                    "No new segments for %.1f seconds; stopping live capture",
                    inactive_for,
                )
                self._stop.set()
                await self._emit_progress()
                return

            refresh_after = min(
                self.config.max_refresh_seconds,
                max(
                    self.config.min_refresh_seconds,
                    playlist.target_duration * self.config.playlist_refresh_factor,
                ),
            )
            await asyncio.sleep(refresh_after)

    async def _download_one_segment(self, seg: HLSSegment) -> tuple[Path, int]:
        seg_path = self._segments_dir / f"{seg.seq:012d}.seg"

        # Basic resume support: reuse already-downloaded segment files.
        if seg_path.exists() and seg_path.stat().st_size > 0:
            return seg_path, int(seg_path.stat().st_size)

        payload = await self._fetch_bytes(seg.uri)

        if seg.key is not None:
            if seg.key.method != "AES-128":
                raise RuntimeError(f"Unsupported encryption method: {seg.key.method}")

            assert self._session is not None
            key = await self._decryptor.get_key(
                session=self._session,
                key_url=seg.key.uri,
                timeout=self._build_timeout(),
                retries=self.config.max_retries,
                retry_base_delay_seconds=self.config.retry_base_delay_seconds,
                retry_max_delay_seconds=self.config.retry_max_delay_seconds,
            )
            iv = seg.key.iv or _iv_from_media_sequence(seg.seq)
            payload = self._decryptor.decrypt_aes128_cbc(payload, key, iv)

        async with aiofiles.open(seg_path, "wb") as f:
            await f.write(payload)

        return seg_path, len(payload)

    async def _emit_progress(self) -> None:
        callback = self.config.progress_callback
        if callback is None:
            return

        elapsed = max(0.001, time.monotonic() - self._started_at)
        payload = {
            "downloaded_bytes": int(self._bytes_downloaded),
            "downloaded_segments": len(self._downloaded_sequences),
            "total_segments": len(self._enqueued_sequences),
            "failed_segments": len(self._failed_sequences),
            "queue_size": self._segment_queue.qsize(),
            "speed_bps": float(self._bytes_downloaded / elapsed),
            "endlist_seen": bool(self._endlist_seen),
        }

        maybe_coro = callback(payload)
        if asyncio.iscoroutine(maybe_coro):
            await maybe_coro

    async def _segment_worker(self, worker_id: int) -> None:
        while not self._stop.is_set() or not self._segment_queue.empty():
            try:
                seq, seg = await asyncio.wait_for(self._segment_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if self._endlist_seen and self._segment_queue.empty():
                    return
                continue

            try:
                _, bytes_written = await self._download_one_segment(seg)
                self._downloaded_sequences.add(seq)
                self._bytes_downloaded += int(bytes_written)
                self._segment_attempts.pop(seq, None)
                await self._emit_progress()
            except Exception as exc:
                attempts = self._segment_attempts.get(seq, 0) + 1
                self._segment_attempts[seq] = attempts

                if attempts < self.config.max_retries:
                    delay = min(
                        self.config.retry_max_delay_seconds,
                        self.config.retry_base_delay_seconds * (2 ** (attempts - 1)),
                    )
                    log.warning(
                        "Worker %d retrying segment %d attempt %d/%d in %.2fs (%s)",
                        worker_id,
                        seq,
                        attempts,
                        self.config.max_retries,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    await self._segment_queue.put((seg.seq, seg))
                else:
                    log.error(
                        "Worker %d failed segment %d after %d attempts (%s): %s",
                        worker_id,
                        seq,
                        attempts,
                        seg.uri,
                        exc,
                    )
                    self._failed_sequences.add(seq)
                    await self._emit_progress()
            finally:
                self._segment_queue.task_done()

    async def _merge_segments_to_ts(self) -> str:
        self._partial_output.parent.mkdir(parents=True, exist_ok=True)

        ordered = sorted(self._segments_dir.glob("*.seg"))
        if not ordered:
            raise RuntimeError("No segment files downloaded")

        async with aiofiles.open(self._partial_output, "wb") as out:
            for seg_path in ordered:
                async with aiofiles.open(seg_path, "rb") as src:
                    while True:
                        chunk = await src.read(1024 * 1024)
                        if not chunk:
                            break
                        await out.write(chunk)

        final_path = Path(self.config.output_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        mode = self.config.merge_mode
        if mode == "auto":
            suffix = Path(self.config.output_path).suffix.lower()
            mode = "ffmpeg_mp4" if suffix == ".mp4" else "ts_concat"

        if mode == "ts_concat":
            self._partial_output.replace(final_path)
            return str(final_path)

        # ffmpeg_mp4 path
        return await self._remux_with_ffmpeg(str(self._partial_output), str(final_path))

    async def _remux_with_ffmpeg(self, input_ts_path: str, output_path: str) -> str:
        out = Path(output_path)
        if out.suffix.lower() != ".mp4":
            out = out.with_suffix(".mp4")

        cmd = [
            self.config.ffmpeg_path,
            "-y",
            "-i",
            input_ts_path,
            "-c",
            "copy",
            str(out),
        ]
        log.info("Running ffmpeg remux: %s", " ".join(shlex.quote(c) for c in cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg remux failed: {stderr.decode(errors='ignore')}")

        return str(out)

    async def run(self) -> HLSDownloadResult:
        started = time.monotonic()
        self._segments_dir.mkdir(parents=True, exist_ok=True)

        connector = aiohttp.TCPConnector(limit=max(20, self.config.max_concurrent_segments * 4))
        timeout = self._build_timeout()

        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=self._build_headers(),
            cookies=self.config.cookies,
            raise_for_status=False,
        ) as session:
            self._session = session

            media_url = await self._resolve_media_playlist_url(self.config.master_or_media_url)
            self._selected_media_playlist_url = media_url

            poll_task = asyncio.create_task(self._poll_media_playlist(media_url), name="hls-poll")
            workers = [
                asyncio.create_task(self._segment_worker(i + 1), name=f"hls-seg-{i+1}")
                for i in range(max(1, self.config.max_concurrent_segments))
            ]

            reason = "completed"
            try:
                await poll_task

                # Wait until all currently enqueued segments are consumed.
                await self._segment_queue.join()

                # Signal workers to exit once queue is drained.
                self._stop.set()
                await asyncio.gather(*workers, return_exceptions=False)
                await self._emit_progress()

                if self._failed_sequences:
                    reason = f"completed_with_failures:{len(self._failed_sequences)}"
            except Exception as exc:
                reason = f"failed:{exc}"
                self._stop.set()
                poll_task.cancel()
                for w in workers:
                    w.cancel()
                raise

        merged_output = await self._merge_segments_to_ts()

        if not self.config.keep_temp_files:
            for p in self._segments_dir.glob("*.seg"):
                with suppress(Exception):
                    p.unlink(missing_ok=True)
            with suppress(Exception):
                self._segments_dir.rmdir()
            if str(merged_output).lower().endswith(".mp4"):
                with suppress(Exception):
                    self._partial_output.unlink(missing_ok=True)

        elapsed = time.monotonic() - started
        return HLSDownloadResult(
            success=True,
            output_path=merged_output,
            selected_variant_url=self._selected_media_playlist_url,
            total_segments=len(self._enqueued_sequences),
            downloaded_segments=len(self._downloaded_sequences),
            elapsed_seconds=elapsed,
            reason=reason,
        )


# ------------------------------ Utilities -------------------------------------

def _load_json_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return {str(k): str(v) for k, v in data.items()}


async def run_hls_download(config: HLSWorkerConfig) -> HLSDownloadResult:
    worker = HLSDownloadWorker(config)
    return await worker.run()


def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Authorized HLS backend worker")
    p.add_argument("--url", required=True, help="Master or media playlist URL")
    p.add_argument("--out", required=True, help="Output path (.ts or .mp4)")
    p.add_argument("--variant", default="best", help="best|worst|index:N|bandwidth:BPS")
    p.add_argument("--referer", default=None)
    p.add_argument("--origin", default=None)
    p.add_argument("--authorization", default=None)
    p.add_argument("--headers-json", default=None, help="Path to JSON object with headers")
    p.add_argument("--cookies-json", default=None, help="Path to JSON object with cookies")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--inactivity-timeout", type=float, default=45.0)
    p.add_argument("--merge-mode", default="auto", choices=["auto", "ts_concat", "ffmpeg_mp4"])
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--keep-temp", action="store_true")
    p.add_argument("--log-level", default="INFO")
    return p


def _ensure_parent_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def main() -> int:
    args = _build_cli().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    headers = _load_json_file(args.headers_json)
    cookies = _load_json_file(args.cookies_json)

    _ensure_parent_dir(args.out)

    cfg = HLSWorkerConfig(
        master_or_media_url=args.url,
        output_path=args.out,
        referer=args.referer,
        origin=args.origin,
        authorization=args.authorization,
        headers=headers,
        cookies=cookies,
        variant=args.variant,
        max_concurrent_segments=max(1, args.concurrency),
        live_inactivity_timeout_seconds=max(5.0, args.inactivity_timeout),
        merge_mode=args.merge_mode,
        ffmpeg_path=args.ffmpeg,
        keep_temp_files=bool(args.keep_temp),
    )

    try:
        result = asyncio.run(run_hls_download(cfg))
    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        return 130
    except Exception as exc:
        log.error("HLS worker failed: %s", exc, exc_info=True)
        return 1

    log.info(
        "Done: success=%s output=%s downloaded=%d total=%d elapsed=%.2fs reason=%s",
        result.success,
        result.output_path,
        result.downloaded_segments,
        result.total_segments,
        result.elapsed_seconds,
        result.reason,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
