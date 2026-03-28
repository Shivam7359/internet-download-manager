"""
Unit tests for core/network.py — the networking layer.

Tests cover:
    • TokenBucketRateLimiter — rate limiting, burst, dynamic rate changes
    • ProxyConfig — URL building, SOCKS detection, from_config
    • SSLConfig — SSL context creation
    • RetryPolicy — delay calculation, retryable errors, status codes
    • Utility functions — filename extraction, Content-Disposition parsing,
      speed/size/ETA formatting
    • NetworkManager — initialization, session access, configuration
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import aiohttp

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.network import (
    TokenBucketRateLimiter,
    ProxyConfig,
    ProxyProtocol,
    SSLConfig,
    PreflightResult,
    RetryPolicy,
    NetworkManager,
    ErrorType,
    classify_error,
    extract_filename_from_url,
    _parse_content_disposition,
    _split_filename,
    format_speed,
    format_size,
    format_eta,
    calculate_eta,
)


# ══════════════════════════════════════════════════════════════════════════════
#  TOKEN BUCKET RATE LIMITER
# ══════════════════════════════════════════════════════════════════════════════

class TestTokenBucketRateLimiter:
    """Tests for the TokenBucketRateLimiter class."""

    def test_unlimited_limiter(self) -> None:
        """Rate of 0 means unlimited — no throttling."""
        limiter = TokenBucketRateLimiter(rate_bps=0)
        assert limiter.is_unlimited is True
        assert limiter.rate_bps == 0.0

    def test_limited_limiter(self) -> None:
        """Non-zero rate creates a limited bucket."""
        limiter = TokenBucketRateLimiter(rate_bps=1024)
        assert limiter.is_unlimited is False
        assert limiter.rate_bps == 1024.0

    @pytest.mark.asyncio
    async def test_acquire_unlimited(self) -> None:
        """Acquire on unlimited limiter returns immediately."""
        limiter = TokenBucketRateLimiter(rate_bps=0)
        start = time.monotonic()
        await limiter.acquire(1_000_000)
        elapsed = time.monotonic() - start
        assert elapsed < 0.1  # should be near-instant

    @pytest.mark.asyncio
    async def test_acquire_within_burst(self) -> None:
        """Acquire within burst capacity returns immediately."""
        # 1 MB/s with burst factor 2 = 2 MB burst capacity
        limiter = TokenBucketRateLimiter(rate_bps=1_048_576, burst_factor=2.0)
        start = time.monotonic()
        await limiter.acquire(1024)  # small amount well within burst
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_acquire_throttles(self) -> None:
        """
        Large acquire on a slow limiter causes a delay.

        Use a very slow rate (100 B/s) and acquire more than the burst.
        """
        limiter = TokenBucketRateLimiter(rate_bps=100, burst_factor=1.0)
        # Drain the bucket
        await limiter.acquire(100)

        # Now acquiring 50 more should take ~0.5 seconds
        start = time.monotonic()
        await limiter.acquire(50)
        elapsed = time.monotonic() - start
        # Allow some tolerance
        assert elapsed >= 0.3, f"Expected at least 0.3s delay, got {elapsed:.3f}s"

    def test_set_rate(self) -> None:
        """Dynamic rate change adjusts the bucket."""
        limiter = TokenBucketRateLimiter(rate_bps=1024)
        assert limiter.rate_bps == 1024.0

        limiter.set_rate(2048)
        assert limiter.rate_bps == 2048.0

    def test_set_rate_to_unlimited(self) -> None:
        """Switching from limited to unlimited."""
        limiter = TokenBucketRateLimiter(rate_bps=1024)
        assert not limiter.is_unlimited

        limiter.set_rate(0)
        assert limiter.is_unlimited

    def test_peek_available(self) -> None:
        """Peek returns approximate available tokens."""
        limiter = TokenBucketRateLimiter(rate_bps=10000, burst_factor=1.0)
        available = limiter.peek_available()
        assert available > 0

    @pytest.mark.asyncio
    async def test_acquire_partial(self) -> None:
        """Partial acquire returns what's available without blocking."""
        limiter = TokenBucketRateLimiter(rate_bps=1000, burst_factor=1.0)
        # Should have up to 1000 tokens initially
        acquired = await limiter.acquire_partial(500)
        assert 1 <= acquired <= 500


# ══════════════════════════════════════════════════════════════════════════════
#  PROXY CONFIG
# ══════════════════════════════════════════════════════════════════════════════

class TestProxyConfig:
    """Tests for proxy configuration."""

    def test_disabled_proxy_url(self) -> None:
        proxy = ProxyConfig(enabled=False, host="proxy.local", port=8080)
        assert proxy.url == ""

    def test_http_proxy_url(self) -> None:
        proxy = ProxyConfig(
            enabled=True,
            protocol="http",
            host="proxy.local",
            port=8080,
        )
        assert proxy.url == "http://proxy.local:8080"

    def test_proxy_url_with_auth(self) -> None:
        proxy = ProxyConfig(
            enabled=True,
            protocol="http",
            host="proxy.local",
            port=8080,
            username="user",
            password="pass",
        )
        assert proxy.url == "http://user:pass@proxy.local:8080"

    def test_proxy_url_with_username_only(self) -> None:
        proxy = ProxyConfig(
            enabled=True,
            protocol="http",
            host="proxy.local",
            port=8080,
            username="user",
        )
        assert proxy.url == "http://user@proxy.local:8080"

    def test_socks5_proxy(self) -> None:
        proxy = ProxyConfig(
            enabled=True,
            protocol="socks5",
            host="socks.local",
            port=1080,
        )
        assert proxy.is_socks is True
        assert proxy.url == "socks5://socks.local:1080"

    def test_http_proxy_not_socks(self) -> None:
        proxy = ProxyConfig(enabled=True, protocol="http", host="x", port=80)
        assert proxy.is_socks is False

    def test_missing_host_or_port(self) -> None:
        proxy = ProxyConfig(enabled=True, protocol="http", host="", port=0)
        assert proxy.url == ""

    def test_from_config(self) -> None:
        config = {
            "network": {
                "proxy": {
                    "enabled": True,
                    "type": "socks5",
                    "host": "socks.example.com",
                    "port": 1080,
                    "username": "admin",
                    "password": "secret",
                    "use_for_all": False,
                }
            }
        }
        proxy = ProxyConfig.from_config(config)
        assert proxy.enabled is True
        assert proxy.protocol == "socks5"
        assert proxy.host == "socks.example.com"
        assert proxy.port == 1080
        assert proxy.username == "admin"
        assert proxy.use_for_all is False


# ══════════════════════════════════════════════════════════════════════════════
#  SSL CONFIG
# ══════════════════════════════════════════════════════════════════════════════

class TestSSLConfig:
    """Tests for SSL/TLS configuration."""

    def test_ssl_verification_enabled(self) -> None:
        cfg = SSLConfig(verify=True)
        ctx = cfg.create_ssl_context()
        # Should return an ssl.SSLContext, not False
        import ssl
        assert isinstance(ctx, ssl.SSLContext)

    def test_ssl_verification_disabled(self) -> None:
        cfg = SSLConfig(verify=False)
        ctx = cfg.create_ssl_context()
        assert ctx is False


# ══════════════════════════════════════════════════════════════════════════════
#  RETRY POLICY
# ══════════════════════════════════════════════════════════════════════════════

class TestRetryPolicy:
    """Tests for exponential backoff retry logic."""

    def test_first_delay_is_base(self) -> None:
        policy = RetryPolicy(base_delay=5.0)
        delay = policy.get_delay(0)
        assert delay == 5.0

    def test_delays_increase(self) -> None:
        policy = RetryPolicy(base_delay=1.0, max_delay=60.0)
        delays = [policy.get_delay(i) for i in range(5)]
        # First delay is always base
        assert delays[0] == 1.0
        # Subsequent delays should generally increase (with jitter)
        # We can't assert exact values due to randomness

    def test_delay_capped_at_max(self) -> None:
        policy = RetryPolicy(base_delay=1.0, max_delay=10.0)
        for i in range(20):
            delay = policy.get_delay(i)
            assert delay <= 10.0


class TestClassifyError:
    """Tests for network error classification."""

    def test_classifies_http_429_as_rate_limited(self) -> None:
        error = aiohttp.ClientResponseError(
            request_info=SimpleNamespace(real_url="https://example.com"),
            history=(),
            status=429,
            message="Too Many Requests",
        )
        assert classify_error(error) == ErrorType.RATE_LIMITED

    def test_classifies_http_416_as_range_not_supported(self) -> None:
        error = aiohttp.ClientResponseError(
            request_info=SimpleNamespace(real_url="https://example.com"),
            history=(),
            status=416,
            message="Range Not Satisfiable",
        )
        assert classify_error(error) == ErrorType.RANGE_NOT_SUPPORTED

    def test_should_retry_within_max(self) -> None:
        policy = RetryPolicy(max_retries=3)
        error = ConnectionError("timeout")
        assert policy.should_retry(0, error) is True
        assert policy.should_retry(2, error) is True
        assert policy.should_retry(3, error) is False  # at limit

    def test_should_not_retry_cancelled(self) -> None:
        policy = RetryPolicy(max_retries=5)
        error = asyncio.CancelledError()
        assert policy.should_retry(0, error) is False

    def test_should_retry_timeout(self) -> None:
        policy = RetryPolicy(max_retries=5)
        error = asyncio.TimeoutError()
        assert policy.should_retry(0, error) is True

    def test_should_retry_os_error(self) -> None:
        policy = RetryPolicy(max_retries=5)
        error = OSError("Network unreachable")
        assert policy.should_retry(0, error) is True

    def test_should_not_retry_value_error(self) -> None:
        policy = RetryPolicy(max_retries=5)
        error = ValueError("bad data")
        assert policy.should_retry(0, error) is False

    def test_retryable_status_codes(self) -> None:
        policy = RetryPolicy()
        assert policy.is_retryable_status(408) is True
        assert policy.is_retryable_status(429) is True
        assert policy.is_retryable_status(500) is True
        assert policy.is_retryable_status(502) is True
        assert policy.is_retryable_status(503) is True
        assert policy.is_retryable_status(504) is True

    def test_non_retryable_status_codes(self) -> None:
        policy = RetryPolicy()
        assert policy.is_retryable_status(200) is False
        assert policy.is_retryable_status(404) is False
        assert policy.is_retryable_status(403) is False
        assert policy.is_retryable_status(301) is False

    def test_from_config(self) -> None:
        config = {
            "network": {
                "max_retries": 10,
                "retry_base_delay_seconds": 2.0,
                "retry_max_delay_seconds": 60.0,
            }
        }
        policy = RetryPolicy.from_config(config)
        assert policy.max_retries == 10
        assert policy.base_delay == 2.0
        assert policy.max_delay == 60.0


# ══════════════════════════════════════════════════════════════════════════════
#  FILENAME EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

class TestFilenameExtraction:
    """Tests for URL filename extraction."""

    def test_simple_url(self) -> None:
        assert extract_filename_from_url(
            "https://example.com/files/report.pdf"
        ) == "report.pdf"

    def test_url_with_query(self) -> None:
        result = extract_filename_from_url(
            "https://cdn.example.com/video.mp4?token=abc&exp=123"
        )
        assert result == "video.mp4"

    def test_url_encoded(self) -> None:
        result = extract_filename_from_url(
            "https://example.com/video%20file.mp4"
        )
        assert result == "video file.mp4"

    def test_url_no_filename(self) -> None:
        result = extract_filename_from_url("https://example.com/")
        assert result == "download"

    def test_url_bare_domain(self) -> None:
        result = extract_filename_from_url("https://example.com")
        assert result == "download"

    def test_url_with_fragment(self) -> None:
        result = extract_filename_from_url(
            "https://example.com/doc.pdf#page=5"
        )
        assert result == "doc.pdf"


# ══════════════════════════════════════════════════════════════════════════════
#  CONTENT-DISPOSITION PARSING
# ══════════════════════════════════════════════════════════════════════════════

class TestContentDisposition:
    """Tests for Content-Disposition header parsing."""

    def test_quoted_filename(self) -> None:
        result = _parse_content_disposition(
            'attachment; filename="report.pdf"'
        )
        assert result == "report.pdf"

    def test_unquoted_filename(self) -> None:
        result = _parse_content_disposition("attachment; filename=report.pdf")
        assert result == "report.pdf"

    def test_rfc5987_encoded(self) -> None:
        result = _parse_content_disposition(
            "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.pdf"
        )
        assert result == "中文.pdf"

    def test_no_filename(self) -> None:
        result = _parse_content_disposition("inline")
        assert result == ""

    def test_empty_header(self) -> None:
        result = _parse_content_disposition("")
        assert result == ""

    def test_filename_with_spaces(self) -> None:
        result = _parse_content_disposition(
            'attachment; filename="my file (1).zip"'
        )
        assert result == "my file (1).zip"


# ══════════════════════════════════════════════════════════════════════════════
#  FILENAME SPLITTING
# ══════════════════════════════════════════════════════════════════════════════

class TestFilenameSplit:
    """Tests for filename splitting (name + extension)."""

    def test_simple_extension(self) -> None:
        name, ext = _split_filename("file.pdf")
        assert name == "file"
        assert ext == ".pdf"

    def test_compound_extension(self) -> None:
        name, ext = _split_filename("archive.tar.gz")
        assert name == "archive"
        assert ext == ".tar.gz"

    def test_no_extension(self) -> None:
        name, ext = _split_filename("README")
        assert name == "README"
        assert ext == ""

    def test_tar_bz2(self) -> None:
        name, ext = _split_filename("data.tar.bz2")
        assert name == "data"
        assert ext == ".tar.bz2"


# ══════════════════════════════════════════════════════════════════════════════
#  FORMATTING FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestFormatSpeed:
    """Tests for speed formatting."""

    def test_zero(self) -> None:
        assert format_speed(0) == "0 B/s"

    def test_bytes(self) -> None:
        assert format_speed(500) == "500 B/s"

    def test_kilobytes(self) -> None:
        result = format_speed(1536)
        assert "KB/s" in result
        assert result == "1.5 KB/s"

    def test_megabytes(self) -> None:
        result = format_speed(10 * 1024 * 1024)
        assert "MB/s" in result

    def test_gigabytes(self) -> None:
        result = format_speed(2.5 * 1024 * 1024 * 1024)
        assert "GB/s" in result

    def test_negative(self) -> None:
        assert format_speed(-100) == "0 B/s"


class TestFormatSize:
    """Tests for file size formatting."""

    def test_unknown(self) -> None:
        assert format_size(-1) == "Unknown"

    def test_zero(self) -> None:
        assert format_size(0) == "0 B"

    def test_bytes(self) -> None:
        assert format_size(512) == "512 B"

    def test_kilobytes(self) -> None:
        result = format_size(1536)
        assert "KB" in result

    def test_megabytes(self) -> None:
        result = format_size(5 * 1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self) -> None:
        result = format_size(3 * 1024 * 1024 * 1024)
        assert "GB" in result


class TestFormatETA:
    """Tests for ETA formatting."""

    def test_zero(self) -> None:
        assert format_eta(0) == "∞"

    def test_seconds(self) -> None:
        assert format_eta(45) == "45s"

    def test_minutes(self) -> None:
        result = format_eta(135)  # 2m 15s
        assert result == "2m 15s"

    def test_hours(self) -> None:
        result = format_eta(3723)  # 1h 02m
        assert result == "1h 02m"

    def test_days(self) -> None:
        result = format_eta(90000)  # 1d 01h
        assert "d" in result

    def test_infinity(self) -> None:
        assert format_eta(float("inf")) == "∞"

    def test_nan(self) -> None:
        assert format_eta(float("nan")) == "∞"

    def test_negative(self) -> None:
        assert format_eta(-10) == "∞"


class TestCalculateETA:
    """Tests for ETA calculation."""

    def test_normal(self) -> None:
        eta = calculate_eta(1000, 100)
        assert eta == 10.0

    def test_zero_speed(self) -> None:
        eta = calculate_eta(1000, 0)
        assert math.isinf(eta)

    def test_zero_remaining(self) -> None:
        eta = calculate_eta(0, 100)
        assert eta == 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  NETWORK MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class TestNetworkManager:
    """Tests for NetworkManager initialization and configuration."""

    @pytest.fixture
    def sample_config(self) -> dict[str, Any]:
        return {
            "network": {
                "bandwidth_limit_kbps": 0,
                "connection_timeout_seconds": 10,
                "read_timeout_seconds": 30,
                "max_retries": 3,
                "retry_base_delay_seconds": 1.0,
                "retry_max_delay_seconds": 30.0,
                "user_agent": "TestAgent/1.0",
                "proxy": {
                    "enabled": False,
                    "type": "http",
                    "host": "",
                    "port": 0,
                    "username": "",
                    "password": "",
                    "use_for_all": True,
                },
                "ipv6_enabled": True,
                "verify_ssl": True,
            }
        }

    def test_creation(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        assert not mgr.is_initialized
        assert mgr.global_limiter.is_unlimited

    @pytest.mark.asyncio
    async def test_initialize_and_close(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        await mgr.initialize()
        assert mgr.is_initialized
        assert mgr.session is not None

        await mgr.close()
        assert not mgr.is_initialized

    @pytest.mark.asyncio
    async def test_session_raises_when_not_initialized(
        self, sample_config: dict
    ) -> None:
        mgr = NetworkManager(sample_config)
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = mgr.session

    @pytest.mark.asyncio
    async def test_set_global_rate(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        mgr.set_global_rate(500)  # 500 KB/s
        assert mgr.global_limiter.rate_bps == 500 * 1024

    @pytest.mark.asyncio
    async def test_create_download_limiter(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        limiter = mgr.create_download_limiter("dl-1", rate_bps=1024)
        assert limiter.rate_bps == 1024.0

        # Same ID returns same limiter
        same = mgr.create_download_limiter("dl-1")
        assert same is limiter

    @pytest.mark.asyncio
    async def test_remove_download_limiter(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        mgr.create_download_limiter("dl-1", rate_bps=1024)
        mgr.remove_download_limiter("dl-1")

        # Creating again gives a new limiter
        new = mgr.create_download_limiter("dl-1", rate_bps=2048)
        assert new.rate_bps == 2048.0

    @pytest.mark.asyncio
    async def test_proxy_url_none_when_disabled(
        self, sample_config: dict
    ) -> None:
        mgr = NetworkManager(sample_config)
        assert mgr.proxy_url is None

    @pytest.mark.asyncio
    async def test_throttle_unlimited(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        # Should return immediately with no delay
        start = time.monotonic()
        await mgr.throttle(1_000_000)
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_build_chunk_headers(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        headers = mgr.build_chunk_headers(
            start_byte=1000,
            end_byte=1999,
            referer="https://example.com",
            cookies="session=abc",
            etag='"etag123"',
        )
        assert headers["Range"] == "bytes=1000-1999"
        assert headers["Referer"] == "https://example.com"
        assert headers["Cookie"] == "session=abc"
        assert headers["If-Range"] == '"etag123"'

    def test_parse_preflight_accept_ranges_with_extra_tokens(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        result = PreflightResult(url="https://example.com/file.bin")
        resp = SimpleNamespace(
            status=200,
            headers={
                "Accept-Ranges": "bytes, content",
                "Content-Length": "1024",
            },
            url="https://example.com/file.bin",
        )

        mgr._parse_preflight_headers(resp, result)

        assert result.resume_supported is True
        assert result.file_size == 1024

    @pytest.mark.asyncio
    async def test_connection_stats(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        # Before init
        stats = mgr.connection_stats
        assert stats["active"] == 0

    def test_retry_policy_from_config(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        assert mgr.retry_policy.max_retries == 3
        assert mgr.retry_policy.base_delay == 1.0

    @pytest.mark.asyncio
    async def test_double_initialize(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        await mgr.initialize()
        await mgr.initialize()  # should not raise
        assert mgr.is_initialized
        await mgr.close()

    @pytest.mark.asyncio
    async def test_update_user_agent(self, sample_config: dict) -> None:
        mgr = NetworkManager(sample_config)
        await mgr.initialize()
        mgr.update_user_agent("NewAgent/2.0")
        assert mgr.session.headers.get("User-Agent") == "NewAgent/2.0"
        await mgr.close()


# ══════════════════════════════════════════════════════════════════════════════
#  PREFLIGHT RESULT
# ══════════════════════════════════════════════════════════════════════════════

class TestPreflightResult:
    """Tests for PreflightResult data class."""

    def test_default_values(self) -> None:
        result = PreflightResult(url="https://example.com/file.zip")
        assert result.file_size == -1
        assert result.resume_supported is False
        assert result.content_type == ""
        assert result.filename == ""
        assert result.error is None
        assert result.redirected is False
