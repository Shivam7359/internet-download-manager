"""
IDM Core — Network Layer
=========================
Centralized networking utilities for the download engine.

This module provides:

    • **TokenBucketRateLimiter** — Bandwidth throttling using the token-bucket
      algorithm.  Supports both per-download and global rate limits.
    • **ProxyConfig** — Typed proxy configuration (HTTP / HTTPS / SOCKS5).
    • **SSLConfig** — TLS settings, certificate verification, custom CA bundles.
    • **NetworkManager** — Factory for ``aiohttp.ClientSession`` instances with
      proper proxy, SSL, timeout, and User-Agent configuration.
    • **Smart retry** — Exponential backoff with jitter for transient failures.
    • **Pre-flight HEAD** — Probe URLs for size, resume support, and filename.

Threading model:
    All async methods are designed to run on the ``EngineThread``'s asyncio
    event loop.  The ``NetworkManager`` owns and manages the aiohttp
    ``TCPConnector`` and sessions.

Usage::

        network = NetworkManager(config)
        await network.initialize()

        info = await network.preflight("https://example.com/file.zip")
        print(info.file_size, info.resume_supported, info.filename)

        session = network.create_session(rate_limiter=limiter)
        async with session.get(url) as resp:
            ...

        await network.close()
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, unquote

import aiohttp
import certifi

try:
    from aiohttp_socks import ProxyConnector, ProxyType
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

# ── Module Logger ──────────────────────────────────────────────────────────────
log = logging.getLogger("idm.core.network")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CONSTANTS                                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# Default connection pool limits
DEFAULT_POOL_LIMIT: int = 100            # total connections
DEFAULT_POOL_LIMIT_PER_HOST: int = 10    # per-host connections

# Retry defaults
DEFAULT_MAX_RETRIES: int = 5
DEFAULT_RETRY_BASE_DELAY: float = 3.0    # seconds
DEFAULT_RETRY_MAX_DELAY: float = 120.0   # seconds

# Rate limiter defaults
DEFAULT_BURST_FACTOR: float = 2.0        # burst = rate × factor

# Minimum meaningful rate limit (1 KB/s)
MIN_RATE_LIMIT_BPS: int = 1024


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ENUMS                                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class ProxyProtocol(str, Enum):
    """Supported proxy protocols."""
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


class ErrorType(str, Enum):
    """
    Classification of network errors for intelligent retry and logging.
    
    Helps distinguish between:
        • DNS resolution failures (transient, retry recommended)
        • TLS/SSL certificate issues (permanent, fail fast)
        • Connection timeouts (transient, retry with backoff)
        • Range request rejections (requires fallback logic)
        • Rate limiting (transient, backoff heavily)
        • Temporary server errors (transient, retry with backoff)
    """
    DNS_RESOLUTION = "dns_resolution"           # Cannot resolve hostname
    TLS_CERTIFICATE = "tls_certificate"         # SSL/TLS cert error
    CONNECTION_TIMEOUT = "connection_timeout"   # Connection failed due to timeout
    SOCKET_ERROR = "socket_error"               # General socket error (unreachable, etc.)
    PROXY_ERROR = "proxy_error"                 # Proxy connection failed
    RATE_LIMITED = "rate_limited"               # 429 Too Many Requests
    RANGE_NOT_SUPPORTED = "range_not_supported" # 416 Range Not Satisfiable, or no Accept-Ranges
    SERVER_ERROR = "server_error"               # 5xx errors
    CLIENT_ERROR = "client_error"               # 4xx errors (not retryable)
    TIMEOUT = "timeout"                         # Request timeout
    CANCELLED = "cancelled"                     # Operation was cancelled
    UNKNOWN = "unknown"                         # Unknown error type


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DATA CLASSES                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class ProxyConfig:
    """
    Proxy configuration for network connections.

    Supports HTTP, HTTPS, SOCKS4, and SOCKS5 proxies with optional
    authentication.
    """
    enabled: bool = False
    protocol: str = ProxyProtocol.HTTP.value
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    use_for_all: bool = True              # apply globally or per-download

    @property
    def url(self) -> str:
        """
        Build the proxy URL string.

        Returns:
            Proxy URL in the format ``protocol://[user:pass@]host:port``.
        """
        if not self.enabled or not self.host or not self.port:
            return ""

        auth = ""
        if self.username:
            auth = f"{self.username}"
            if self.password:
                auth += f":{self.password}"
            auth += "@"

        return f"{self.protocol}://{auth}{self.host}:{self.port}"

    @property
    def is_socks(self) -> bool:
        """Return True if this is a SOCKS proxy."""
        return self.protocol in (ProxyProtocol.SOCKS4.value, ProxyProtocol.SOCKS5.value)

    @classmethod
    def from_config(cls, config_dict: dict[str, Any]) -> ProxyConfig:
        """Create a ProxyConfig from a configuration dictionary."""
        proxy_cfg = config_dict.get("network", {}).get("proxy", {})
        return cls(
            enabled=proxy_cfg.get("enabled", False),
            protocol=proxy_cfg.get("type", "http"),
            host=proxy_cfg.get("host", ""),
            port=proxy_cfg.get("port", 0),
            username=proxy_cfg.get("username", ""),
            password=proxy_cfg.get("password", ""),
            use_for_all=proxy_cfg.get("use_for_all", True),
        )


@dataclass
class SSLConfig:
    """
    TLS/SSL configuration.

    Controls certificate verification and custom CA bundle usage.
    """
    verify: bool = True
    ca_bundle_path: Optional[str] = None   # path to custom CA bundle

    def create_ssl_context(self) -> ssl.SSLContext | bool:
        """
        Create an SSL context based on configuration.

        Returns:
            An ``ssl.SSLContext`` for verified connections, or ``False``
            to disable verification entirely.
        """
        if not self.verify:
            log.error(
                "SSL/TLS certificate verification is disabled; use only for trusted testing."
            )
            return False
        ctx = ssl.create_default_context(
            cafile=self.ca_bundle_path or certifi.where()
        )
        # Enable hostname checking
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx


@dataclass
class PreflightResult:
    """
    Result of a HEAD request probe on a download URL.

    Contains metadata needed before starting the actual download:
    file size, resume support, content type, and suggested filename.
    """
    url: str                                        # final URL (after redirects)
    file_size: int = -1                             # -1 = unknown
    resume_supported: bool = False                  # server accepts Range
    content_type: str = ""                          # MIME type
    filename: str = ""                              # suggested filename
    etag: Optional[str] = None                      # ETag for conditional requests
    last_modified: Optional[str] = None             # Last-Modified header
    headers: dict[str, str] = field(default_factory=dict)
    status_code: int = 0
    redirected: bool = False                        # was the URL redirected?
    error: Optional[str] = None


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  TOKEN BUCKET RATE LIMITER                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class TokenBucketRateLimiter:
    """
    Bandwidth throttle using the token-bucket algorithm.

    Each "token" represents one byte.  The bucket is refilled at a
    constant rate (bytes per second).  Consumers ``acquire()`` tokens
    before writing data; if the bucket is empty, they await until
    enough tokens are available.

    Features:
        • Burst support — bucket can hold ``rate × burst_factor`` tokens.
        • Async-safe — uses ``asyncio.Event`` for non-busy waiting.
        • Dynamic rate — ``set_rate()`` changes the limit on the fly.
        • Zero-rate = unlimited — ``rate_bps == 0`` disables throttling.

    Args:
        rate_bps: Bytes per second (0 = unlimited).
        burst_factor: Multiplier for burst capacity.

    Usage::

        limiter = TokenBucketRateLimiter(rate_bps=1_048_576)  # 1 MB/s
        await limiter.acquire(65536)  # request 64 KB
    """

    def __init__(
        self,
        rate_bps: int = 0,
        burst_factor: float = DEFAULT_BURST_FACTOR,
    ) -> None:
        self._rate_bps: float = float(rate_bps)
        self._burst_factor: float = burst_factor
        self._max_tokens: float = self._calculate_max_tokens()
        self._tokens: float = self._max_tokens
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    def _calculate_max_tokens(self) -> float:
        """
        Calculate the maximum bucket capacity.

        When rate is 0 (unlimited), return infinity so ``acquire()``
        never waits.
        """
        if self._rate_bps <= 0:
            return float("inf")
        return self._rate_bps * self._burst_factor

    @property
    def rate_bps(self) -> float:
        """Current rate limit in bytes per second (0 = unlimited)."""
        return self._rate_bps

    @property
    def is_unlimited(self) -> bool:
        """Return True if no rate limit is active."""
        return self._rate_bps <= 0

    def set_rate(self, rate_bps: int) -> None:
        """
        Dynamically change the rate limit.

        The bucket capacity is recalculated.  Current tokens are clamped
        to the new maximum.

        Args:
            rate_bps: New rate in bytes per second (0 = unlimited).
        """
        self._rate_bps = float(rate_bps)
        self._max_tokens = self._calculate_max_tokens()
        self._tokens = min(self._tokens, self._max_tokens)
        log.debug("Rate limiter updated: %d B/s", rate_bps)

    def _refill(self) -> None:
        """
        Add tokens based on elapsed time since last refill.

        Called internally before each acquire.  Tokens are capped at
        ``_max_tokens`` to prevent unbounded accumulation during idle
        periods.
        """
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now

        if self._rate_bps <= 0:
            # Unlimited — always full
            self._tokens = self._max_tokens
            return

        self._tokens = min(
            self._max_tokens,
            self._tokens + elapsed * self._rate_bps,
        )

    async def acquire(self, amount: int) -> None:
        """
        Consume *amount* tokens (bytes), waiting if necessary.

        If the bucket doesn't have enough tokens, this coroutine sleeps
        in small increments until sufficient tokens have accumulated.
        Each sleep is at most 50 ms, keeping the download responsive
        to cancellation.

        Args:
            amount: Number of bytes to acquire permission for.
        """
        if self._rate_bps <= 0:
            return  # unlimited — no throttling

        if amount <= 0:
            return

        async with self._lock:
            while True:
                self._refill()

                if self._tokens >= amount:
                    self._tokens -= amount
                    return

                # Calculate wait time for needed tokens
                deficit = amount - self._tokens
                wait_time = deficit / self._rate_bps

                # Cap individual wait to 50ms for responsiveness
                wait_time = min(wait_time, 0.05)

                # Release lock while sleeping so other coroutines can proceed
                # (we'll re-check after waking)
                self._lock.release()
                try:
                    await asyncio.sleep(wait_time)
                finally:
                    await self._lock.acquire()

    async def acquire_partial(self, desired: int) -> int:
        """
        Acquire as many tokens as currently available, up to *desired*.

        Unlike ``acquire()``, this never waits — it returns immediately
        with however many tokens are available (minimum 1 if rate > 0,
        or *desired* if unlimited).

        This is useful when you want to write whatever you can right now
        without blocking.

        Args:
            desired: Maximum bytes you'd like to acquire.

        Returns:
            Number of bytes actually acquired (1 ≤ result ≤ desired).
        """
        if self._rate_bps <= 0:
            return desired

        async with self._lock:
            self._refill()
            available = max(1, min(desired, int(self._tokens)))
            self._tokens -= available
            return available

    def peek_available(self) -> int:
        """
        Non-async peek at currently available tokens.

        Useful for UI display.  Not perfectly accurate (no lock).

        Returns:
            Approximate bytes available right now.
        """
        self._refill()
        return max(0, int(self._tokens))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ERROR CLASSIFICATION                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def classify_error(error: Exception) -> ErrorType:
    """
    Classify a network error to determine retry strategy and logging level.
    
    Args:
        error: The exception that occurred.
    
    Returns:
        An ErrorType enum value categorizing the error.
    """
    # Cancellation errors
    if isinstance(error, asyncio.CancelledError):
        return ErrorType.CANCELLED
    
    # Timeout errors
    if isinstance(error, asyncio.TimeoutError):
        return ErrorType.TIMEOUT
    
    # aiohttp-specific errors
    if isinstance(error, aiohttp.ClientSSLError):
        return ErrorType.TLS_CERTIFICATE

    if isinstance(error, aiohttp.ClientProxyConnectionError):
        return ErrorType.PROXY_ERROR

    if isinstance(error, aiohttp.ClientResponseError):
        if error.status == 416:
            return ErrorType.RANGE_NOT_SUPPORTED
        if error.status == 429:
            return ErrorType.RATE_LIMITED
        if 500 <= error.status:
            return ErrorType.SERVER_ERROR
        if 400 <= error.status:
            return ErrorType.CLIENT_ERROR

    if isinstance(error, aiohttp.ClientConnectorError):
        # Check for specific OSError causes
        if hasattr(error, '__cause__') and error.__cause__:
            cause_type = type(error.__cause__).__name__
            if 'gaierror' in cause_type or 'DNS' in cause_type:
                return ErrorType.DNS_RESOLUTION
            if 'timeout' in cause_type.lower():
                return ErrorType.CONNECTION_TIMEOUT
        return ErrorType.SOCKET_ERROR

    if isinstance(error, (aiohttp.ServerTimeoutError, socket.timeout, TimeoutError)):
        return ErrorType.TIMEOUT

    if isinstance(error, OSError):
        return ErrorType.SOCKET_ERROR
    
    return ErrorType.UNKNOWN

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  EXPONENTIAL BACKOFF                                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class RetryPolicy:
    """
    Smart retry logic with exponential backoff and jitter.

    Implements the "decorrelated jitter" strategy, which is superior to
    simple exponential backoff in high-concurrency scenarios.

    Reference:
        AWS Architecture Blog — "Exponential Backoff And Jitter"

    Args:
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay cap in seconds.

    Usage::

        policy = RetryPolicy(max_retries=5)
        for attempt in range(policy.max_retries):
            try:
                await do_something()
                break
            except TransientError:
                delay = policy.get_delay(attempt)
                await asyncio.sleep(delay)
    """

    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_RETRY_BASE_DELAY,
        max_delay: float = DEFAULT_RETRY_MAX_DELAY,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._last_delay: float = base_delay

    def get_delay(self, attempt: int) -> float:
        """
        Calculate the delay for the given retry attempt.

        Uses decorrelated jitter:
            delay = min(max_delay, random(base_delay, last_delay × 3))

        Args:
            attempt: Zero-based attempt number.

        Returns:
            Delay in seconds (float).
        """
        if attempt <= 0:
            self._last_delay = self.base_delay
            return self.base_delay

        # Decorrelated jitter
        self._last_delay = min(
            self.max_delay,
            random.uniform(self.base_delay, self._last_delay * 3),
        )
        return self._last_delay

    def should_retry(self, attempt: int, error: Exception) -> bool:
        """
        Determine whether to retry based on the attempt number and error type.

        Retries on:
            • ``aiohttp.ClientError`` (connection errors, timeouts)
            • ``asyncio.TimeoutError``
            • ``ConnectionError``
            • ``OSError`` (network unreachable, etc.)

        Does NOT retry on:
            • HTTP 4xx errors (client errors — except 408, 429)
            • ``asyncio.CancelledError``
            • ``KeyboardInterrupt``

        Args:
            attempt: Current attempt number (0-based).
            error: The exception that occurred.

        Returns:
            True if the request should be retried.
        """
        if attempt >= self.max_retries:
            return False

        # Never retry cancellation
        if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
            return False

        # Retryable error types
        retryable_types = (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ConnectionError,
            OSError,
        )

        return isinstance(error, retryable_types)

    def classify_error(self, error: Exception) -> ErrorType:
        """
        Classify a network error for logging and analytics.
        
        Args:
            error: The exception that occurred.
        
        Returns:
            An ErrorType enum value categorizing the error.
        """
        return classify_error(error)

    def get_retry_delay(self, attempt: int, error: Exception) -> Optional[float]:
        """
        Get the delay for retrying after an error, or None if should not retry.
        
        Certain error types get special treatment:
        • PROXY_ERROR: Up to 10x base delay (proxy issues need time to recover)
        • RATE_LIMITED: Up to 3x base delay (respect backoff signals)
        • RANGE_NOT_SUPPORTED: Do not retry (requires fallback strategy)
        
        Args:
            attempt: Current attempt number (0-based).
            error: The exception that occurred.
        
        Returns:
            Delay in seconds, or None if should not retry.
        """
        if not self.should_retry(attempt, error):
            return None
        
        error_type = self.classify_error(error)
        
        # Proxy errors get aggressive backoff (up to 10x) — proxy infrastructure
        # needs extra recovery time, especially after configuration changes
        if error_type == ErrorType.PROXY_ERROR:
            # Scale up more aggressively for proxy errors
            return min(self.max_delay, self.get_delay(attempt) * 10)
        
        # Rate-limited errors get extra-long backoff (up to 3x)
        if error_type == ErrorType.RATE_LIMITED:
            return min(self.max_delay, self.get_delay(attempt) * 3)
        
        # Range not supported errors should not retry (requires fallback)
        if error_type == ErrorType.RANGE_NOT_SUPPORTED:
            return None
        
        return self.get_delay(attempt)
    def is_retryable_status(self, status_code: int) -> bool:
        """
        Check if an HTTP status code indicates a retryable condition.

        Retryable statuses:
            • 408 Request Timeout
            • 429 Too Many Requests
            • 500 Internal Server Error
            • 502 Bad Gateway
            • 503 Service Unavailable
            • 504 Gateway Timeout

        Args:
            status_code: HTTP response status code.

        Returns:
            True if the request should be retried.
        """
        return status_code in (408, 429, 500, 502, 503, 504)

    def classify_http_status(self, status_code: int) -> ErrorType:
        """
        Classify an HTTP status code error.
        
        Args:
            status_code: HTTP response status code.
        
        Returns:
            An ErrorType enum value.
        """
        if status_code == 416:
            return ErrorType.RANGE_NOT_SUPPORTED
        elif status_code == 429:
            return ErrorType.RATE_LIMITED
        elif 500 <= status_code < 600:
            return ErrorType.SERVER_ERROR
        elif 400 <= status_code < 500:
            return ErrorType.CLIENT_ERROR
        else:
            return ErrorType.UNKNOWN

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> RetryPolicy:
        """Create a RetryPolicy from a configuration dictionary."""
        net = config.get("network", {})
        return cls(
            max_retries=net.get("max_retries", DEFAULT_MAX_RETRIES),
            base_delay=net.get("retry_base_delay_seconds", DEFAULT_RETRY_BASE_DELAY),
            max_delay=net.get("retry_max_delay_seconds", DEFAULT_RETRY_MAX_DELAY),
        )


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  NETWORK MANAGER                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class NetworkManager:
    """
    Central networking facility for the IDM download engine.

    Manages:
        • aiohttp session creation with proxy and SSL configuration
        • Global and per-download rate limiters
        • Connection pooling via ``TCPConnector``
        • Pre-flight URL probing (HEAD requests)
        • Retry policy

    The ``NetworkManager`` is a singleton-like object that lives on the
    ``EngineThread``'s asyncio loop for the lifetime of the application.

    Args:
        config: The full application configuration dictionary.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._net_config: dict[str, Any] = config.get("network", {})

        # Proxy
        self._proxy = ProxyConfig.from_config(config)

        # SSL
        self._ssl_config = SSLConfig(
            verify=self._net_config.get("verify_ssl", True),
        )
        self._ssl_context = self._ssl_config.create_ssl_context()

        # Timeouts
        self._connect_timeout: float = float(
            self._net_config.get("connection_timeout_seconds", 30)
        )
        self._read_timeout: float = float(
            self._net_config.get("read_timeout_seconds", 60)
        )

        # User-Agent
        self._user_agent: str = self._net_config.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        # IPv6
        self._ipv6_enabled: bool = self._net_config.get("ipv6_enabled", True)

        # Global rate limiter (0 = unlimited)
        global_rate = self._net_config.get("bandwidth_limit_kbps", 0) * 1024
        self._global_limiter = TokenBucketRateLimiter(rate_bps=int(global_rate))

        # Per-download rate limiters (download_id → limiter)
        self._download_limiters: dict[str, TokenBucketRateLimiter] = {}

        # Retry policy
        self._retry_policy = RetryPolicy.from_config(config)

        # Connector and sessions (created in initialize())
        self._connector: Optional[aiohttp.TCPConnector] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._initialized: bool = False

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """
        Create the connection pool and default session.

        Must be called from the engine's asyncio loop before any
        network operations.
        """
        if self._initialized:
            return

        # Choose the appropriate connector based on proxy type
        if self._proxy.enabled and self._proxy.is_socks:
            if not HAS_SOCKS:
                log.error(
                    "SOCKS proxy requested but aiohttp-socks is not installed. "
                    "Install with: pip install aiohttp-socks"
                )
                raise ImportError("aiohttp-socks is required for SOCKS proxy support")

            socks_type = (
                ProxyType.SOCKS5 if self._proxy.protocol == ProxyProtocol.SOCKS5.value
                else ProxyType.SOCKS4
            )
            self._connector = ProxyConnector(
                proxy_type=socks_type,
                host=self._proxy.host,
                port=self._proxy.port,
                username=self._proxy.username or None,
                password=self._proxy.password or None,
                ssl=self._ssl_context,
                limit=DEFAULT_POOL_LIMIT,
                limit_per_host=DEFAULT_POOL_LIMIT_PER_HOST,
                enable_cleanup_closed=True,
                force_close=False,
            )
            log.info(
                "SOCKS proxy connector: %s:%d", self._proxy.host, self._proxy.port
            )
        else:
            # Standard TCP connector (also used for HTTP/HTTPS proxy via session)
            family = socket.AF_UNSPEC if self._ipv6_enabled else socket.AF_INET
            self._connector = aiohttp.TCPConnector(
                ssl=self._ssl_context,
                limit=DEFAULT_POOL_LIMIT,
                limit_per_host=DEFAULT_POOL_LIMIT_PER_HOST,
                enable_cleanup_closed=True,
                force_close=False,
                family=family,
            )

        # Default timeout
        timeout = aiohttp.ClientTimeout(
            total=None,                          # no total timeout for downloads
            connect=self._connect_timeout,
            sock_read=self._read_timeout,
        )

        # Default headers
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }

        self._session = aiohttp.ClientSession(
            connector=self._connector,
            timeout=timeout,
            headers=headers,
            trust_env=False,                     # don't read system proxy env vars
        )

        self._initialized = True
        log.info(
            "NetworkManager initialized (proxy=%s, ipv6=%s, ssl_verify=%s, "
            "global_limit=%s)",
            self._proxy.url if self._proxy.enabled else "none",
            self._ipv6_enabled,
            self._ssl_config.verify,
            f"{self._global_limiter.rate_bps:.0f} B/s"
            if not self._global_limiter.is_unlimited
            else "unlimited",
        )

    async def close(self) -> None:
        """Close all sessions and the connection pool."""
        if self._session:
            await self._session.close()
            self._session = None
        if self._connector:
            await self._connector.close()
            self._connector = None
        self._download_limiters.clear()
        self._initialized = False
        log.info("NetworkManager closed")

    async def apply_runtime_config(
        self,
        config: dict[str, Any],
        *,
        reinitialize_session: bool = False,
    ) -> None:
        """
        Apply updated network settings at runtime.

        Args:
            config: Full application configuration.
            reinitialize_session: If True, recreate connector/session to apply
                proxy/SSL/timeout changes immediately.
        """
        self._config = config
        self._net_config = config.get("network", {})
        self._proxy = ProxyConfig.from_config(config)
        self._ssl_config = SSLConfig(verify=self._net_config.get("verify_ssl", True))
        self._ssl_context = self._ssl_config.create_ssl_context()
        self._connect_timeout = float(self._net_config.get("connection_timeout_seconds", 30))
        self._read_timeout = float(self._net_config.get("read_timeout_seconds", 60))
        self._user_agent = self._net_config.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        self._ipv6_enabled = bool(self._net_config.get("ipv6_enabled", True))
        self._retry_policy = RetryPolicy.from_config(config)

        rate_kbps = int(self._net_config.get("bandwidth_limit_kbps", 0))
        self.set_global_rate(rate_kbps)

        if reinitialize_session and self._initialized:
            await self.close()
            await self.initialize()

        log.info(
            "Runtime network config applied (verify_ssl=%s, proxy=%s, rate=%s)",
            self._ssl_config.verify,
            "enabled" if self._proxy.enabled else "disabled",
            f"{rate_kbps} KB/s" if rate_kbps > 0 else "unlimited",
        )

    @property
    def is_initialized(self) -> bool:
        """Return True if the manager has been initialized."""
        return self._initialized

    def _ensure_initialized(self) -> aiohttp.ClientSession:
        """Raise RuntimeError if not initialized."""
        if not self._initialized or self._session is None:
            raise RuntimeError(
                "NetworkManager is not initialized. Call initialize() first."
            )
        return self._session

    # ── Session Access ─────────────────────────────────────────────────────

    @property
    def session(self) -> aiohttp.ClientSession:
        """
        The default aiohttp session.

        Raises:
            RuntimeError: If the manager is not initialized.
        """
        return self._ensure_initialized()

    @property
    def proxy_url(self) -> Optional[str]:
        """
        The HTTP/HTTPS proxy URL, or None if proxy is disabled or is SOCKS.

        SOCKS proxies are handled at the connector level, so they don't
        need a per-request proxy URL.
        """
        if self._proxy.enabled and not self._proxy.is_socks:
            return self._proxy.url
        return None

    @property
    def retry_policy(self) -> RetryPolicy:
        """The configured retry policy."""
        return self._retry_policy

    def get_pool_stats(self) -> dict[str, Any]:
        """Return best-effort connection pool and limiter statistics."""
        connector = self._connector
        session = self._session

        acquired = 0
        free = 0
        if connector is not None:
            acquired = len(getattr(connector, "_acquired", []))
            conns = getattr(connector, "_conns", {}) or {}
            free = sum(len(v) for v in conns.values())

        return {
            "initialized": self._initialized,
            "session_open": bool(session and not session.closed),
            "proxy_enabled": self._proxy.enabled,
            "proxy_type": self._proxy.protocol if self._proxy.enabled else "none",
            "ssl_verify": self._ssl_config.verify,
            "ipv6_enabled": self._ipv6_enabled,
            "connector": {
                "limit_total": DEFAULT_POOL_LIMIT,
                "limit_per_host": DEFAULT_POOL_LIMIT_PER_HOST,
                "acquired": acquired,
                "idle": free,
            },
            "rate_limiters": {
                "global_unlimited": self._global_limiter.is_unlimited,
                "active_per_download": len(self._download_limiters),
            },
            "retry": {
                "max_retries": self._retry_policy.max_retries,
                "base_delay": self._retry_policy.base_delay,
                "max_delay": self._retry_policy.max_delay,
            },
        }

    # ── Rate Limiters ──────────────────────────────────────────────────────

    @property
    def global_limiter(self) -> TokenBucketRateLimiter:
        """The global bandwidth rate limiter."""
        return self._global_limiter

    def set_global_rate(self, rate_kbps: int) -> None:
        """
        Change the global bandwidth limit.

        Args:
            rate_kbps: Rate in kilobytes per second (0 = unlimited).
        """
        self._global_limiter.set_rate(rate_kbps * 1024)
        log.info(
            "Global rate limit changed: %s",
            f"{rate_kbps} KB/s" if rate_kbps > 0 else "unlimited",
        )

    def create_download_limiter(
        self,
        download_id: str,
        rate_bps: Optional[int] = None,
    ) -> TokenBucketRateLimiter:
        """
        Create or retrieve a per-download rate limiter.

        Args:
            download_id: The download UUID.
            rate_bps: Optional rate in bytes per second. If omitted and an
                existing limiter is present, its current rate is preserved.

        Returns:
            The rate limiter for this download.
        """
        if download_id in self._download_limiters:
            limiter = self._download_limiters[download_id]
            if rate_bps is not None:
                limiter.set_rate(max(0, int(rate_bps)))
            return limiter

        limiter = TokenBucketRateLimiter(rate_bps=max(0, int(rate_bps or 0)))
        self._download_limiters[download_id] = limiter
        return limiter

    def remove_download_limiter(self, download_id: str) -> None:
        """Remove a per-download rate limiter (cleanup after download completes)."""
        self._download_limiters.pop(download_id, None)

    async def throttle(
        self,
        amount: int,
        download_id: Optional[str] = None,
    ) -> None:
        """
        Apply bandwidth throttling for a data chunk.

        Checks both the global limiter and the per-download limiter
        (if one exists).  Both limiters must grant tokens before the
        data is released.

        Args:
            amount: Number of bytes to throttle.
            download_id: Optional download UUID for per-download limits.
        """
        # Global throttle
        if not self._global_limiter.is_unlimited:
            await self._global_limiter.acquire(amount)

        # Per-download throttle
        if download_id and download_id in self._download_limiters:
            limiter = self._download_limiters[download_id]
            if not limiter.is_unlimited:
                await limiter.acquire(amount)

    # ── Pre-flight Probe ───────────────────────────────────────────────────

    async def preflight(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
        cookies: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> PreflightResult:
        """
        Probe a URL with a HEAD request to gather download metadata.

        Determines:
            • File size (``Content-Length``)
            • Resume support (``Accept-Ranges: bytes``)
            • Content type
            • Suggested filename (from ``Content-Disposition`` or URL path)
            • Final URL after redirects
            • ETag and Last-Modified for conditional requests

        If the HEAD request fails, falls back to a partial GET (Range: 0-0).

        Args:
            url: The URL to probe.
            referer: Optional Referer header.
            cookies: Optional cookie string.
            headers: Additional headers to send.

        Returns:
            A ``PreflightResult`` with the gathered metadata.
        """
        session = self._ensure_initialized()
        result = PreflightResult(url=url)

        request_headers: dict[str, str] = {}
        if referer:
            request_headers["Referer"] = referer
        if cookies:
            request_headers["Cookie"] = cookies
        if headers:
            request_headers.update(headers)

        preflight_timeout = self._build_preflight_timeout()

        try:
            # First try HEAD
            async with session.head(
                url,
                headers=request_headers,
                allow_redirects=True,
                proxy=self.proxy_url,
                timeout=preflight_timeout,
            ) as resp:
                result.status_code = resp.status
                result.url = str(resp.url)
                result.redirected = str(resp.url) != url
                result.headers = dict(resp.headers)

                if resp.status < 400:
                    self._parse_preflight_headers(resp, result)
                    # Some servers support byte ranges but omit/obfuscate
                    # Accept-Ranges in HEAD responses. Probe with a tiny GET.
                    if not result.resume_supported:
                        await self._probe_range_support(
                            session=session,
                            url=result.url or url,
                            base_headers=request_headers,
                            result=result,
                        )
                    return result

            # HEAD failed (some servers don't support it) — try GET with Range: 0-0
            log.debug("HEAD failed for %s, trying GET Range: 0-0", url)
            request_headers["Range"] = "bytes=0-0"
            async with session.get(
                url,
                headers=request_headers,
                allow_redirects=True,
                proxy=self.proxy_url,
                timeout=preflight_timeout,
            ) as resp:
                result.status_code = resp.status
                result.url = str(resp.url)
                result.redirected = str(resp.url) != url
                result.headers = dict(resp.headers)
                self._parse_preflight_headers(resp, result)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Preflight failed for %s: %s", url, exc)
            result.error = str(exc)

        # Fallback filename from URL if not found in headers
        if not result.filename:
            result.filename = extract_filename_from_url(url)

        return result

    async def _probe_range_support(
        self,
        *,
        session: aiohttp.ClientSession,
        url: str,
        base_headers: dict[str, str],
        result: PreflightResult,
    ) -> None:
        """Lightweight Range probe for servers with ambiguous HEAD metadata."""
        probe_headers = dict(base_headers)
        probe_headers["Range"] = "bytes=0-0"
        probe_headers.setdefault("Accept-Encoding", "identity")
        probe_timeout = self._build_preflight_timeout(is_probe=True)

        try:
            async with session.get(
                url,
                headers=probe_headers,
                allow_redirects=True,
                proxy=self.proxy_url,
                timeout=probe_timeout,
            ) as probe_resp:
                self._parse_preflight_headers(probe_resp, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Range probe skipped/failed for %s: %s", url, exc)

    def _build_preflight_timeout(self, *, is_probe: bool = False) -> aiohttp.ClientTimeout:
        """Build bounded preflight/probe timeout from active network settings."""
        connect_timeout = max(1.0, float(self._connect_timeout))
        read_timeout = max(1.0, float(self._read_timeout))

        if is_probe:
            probe_read = min(5.0, read_timeout)
            total = connect_timeout + probe_read
            return aiohttp.ClientTimeout(
                total=total,
                connect=connect_timeout,
                sock_connect=connect_timeout,
                sock_read=probe_read,
            )

        total = connect_timeout + read_timeout
        return aiohttp.ClientTimeout(
            total=total,
            connect=connect_timeout,
            sock_connect=connect_timeout,
            sock_read=read_timeout,
        )

    def _parse_preflight_headers(
        self,
        resp: aiohttp.ClientResponse,
        result: PreflightResult,
    ) -> None:
        """
        Extract metadata from HTTP response headers.

        Handles:
            • Content-Length → file_size
            • Accept-Ranges → resume_supported
            • Content-Disposition → filename
            • Content-Type → content_type
            • ETag → etag
            • Last-Modified → last_modified
            • Content-Range (from partial GET) → file_size
        """
        headers = resp.headers

        # ── File size ──────────────────────────────────────────────────
        content_length = headers.get("Content-Length")
        if content_length:
            try:
                result.file_size = int(content_length)
            except ValueError:
                pass

        # Content-Range: bytes 0-0/TOTAL (from Range: 0-0 request)
        content_range = headers.get("Content-Range", "")
        if content_range:
            match = re.search(r"/(\d+)", content_range)
            if match:
                result.file_size = int(match.group(1))
            # If server responds to Range request, it supports resume
            if resp.status == 206:
                result.resume_supported = True

        # ── Resume support ─────────────────────────────────────────────
        accept_ranges = headers.get("Accept-Ranges", "").lower()
        if "bytes" in accept_ranges:
            result.resume_supported = True

        # ── Content type ───────────────────────────────────────────────
        result.content_type = headers.get("Content-Type", "")

        # ── Filename from Content-Disposition ──────────────────────────
        disposition = headers.get("Content-Disposition", "")
        if disposition:
            result.filename = _parse_content_disposition(disposition)

        # Fallback: extract from URL
        if not result.filename:
            result.filename = extract_filename_from_url(str(resp.url))

        # ── Caching headers ────────────────────────────────────────────
        result.etag = headers.get("ETag")
        result.last_modified = headers.get("Last-Modified")

    # ── Helper: Build Request Headers ──────────────────────────────────────

    def build_chunk_headers(
        self,
        start_byte: int,
        end_byte: int,
        *,
        referer: Optional[str] = None,
        cookies: Optional[str] = None,
        etag: Optional[str] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        """
        Build HTTP headers for a chunk download request.

        Args:
            start_byte: First byte of the range (inclusive).
            end_byte: Last byte of the range (inclusive).
            referer: Optional Referer header.
            cookies: Optional cookie string.
            etag: Optional ETag for conditional request.
            extra_headers: Any additional headers.

        Returns:
            Complete headers dict ready for ``session.get()``.
        """
        headers: dict[str, str] = {
            "Range": f"bytes={start_byte}-{end_byte}",
        }

        if referer:
            headers["Referer"] = referer
        if cookies:
            headers["Cookie"] = cookies
        if etag:
            headers["If-Range"] = etag
        if extra_headers:
            headers.update(extra_headers)

        return headers

    # ── Configuration Updates ──────────────────────────────────────────────

    def update_user_agent(self, user_agent: str) -> None:
        """Change the User-Agent for subsequent requests."""
        self._user_agent = user_agent
        if self._session:
            self._session.headers.update({"User-Agent": user_agent})
        log.debug("User-Agent updated")

    def update_proxy(self, proxy_config: ProxyConfig) -> None:
        """
        Update the proxy configuration.

        Note: For SOCKS proxies, this requires re-initialization since
        the proxy is bound to the connector, not per-request.
        """
        self._proxy = proxy_config
        if proxy_config.is_socks:
            log.warning(
                "SOCKS proxy change requires reinitialization. "
                "Call close() then initialize() to apply."
            )
        else:
            log.info("Proxy updated: %s", proxy_config.url or "disabled")

    @property
    def connection_stats(self) -> dict[str, Any]:
        """
        Return current connection pool statistics.

        Useful for the UI status bar and diagnostics.
        """
        if not self._connector:
            return {"active": 0, "idle": 0, "limit": 0}

        # Not all connector types expose these, so we use getattr safely
        return {
            "limit": getattr(self._connector, "_limit", DEFAULT_POOL_LIMIT),
            "limit_per_host": getattr(
                self._connector, "_limit_per_host", DEFAULT_POOL_LIMIT_PER_HOST
            ),
        }


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  UTILITY FUNCTIONS                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def extract_filename_from_url(url: str) -> str:
    """
    Extract a filename from a URL path.

    Handles URL-encoded characters and strips query strings.

    Args:
        url: The URL to extract the filename from.

    Returns:
        The extracted filename, or "download" if no filename can be
        determined.

    Examples:
        >>> extract_filename_from_url("https://example.com/files/report.pdf")
        'report.pdf'
        >>> extract_filename_from_url("https://cdn.example.com/video%20file.mp4?token=abc")
        'video file.mp4'
    """
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)

        # Get the last path segment
        filename = Path(path).name

        # Strip any remaining query fragments
        if not filename or filename == "/" or filename == ".":
            filename = "download"

        # Sanitize: remove characters that are invalid in filenames
        # Windows: <>:"/\|?*  Unix: /
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)

        # Limit length
        if len(filename) > 255:
            name, ext = _split_filename(filename)
            filename = name[:255 - len(ext)] + ext

        return filename

    except Exception:
        return "download"


def _parse_content_disposition(header: str) -> str:
    """
    Parse the ``Content-Disposition`` header to extract the filename.

    Handles both ASCII and RFC 5987 encoded filenames::

        Content-Disposition: attachment; filename="file.zip"
        Content-Disposition: attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.zip

    Args:
        header: The full Content-Disposition header value.

    Returns:
        The extracted filename, or empty string if not found.
    """
    # Try RFC 5987 encoded filename first (filename*=encoding''value)
    match = re.search(
        r"filename\*\s*=\s*(?:UTF-8|utf-8)?'[^']*'(.+?)(?:;|$)",
        header,
        re.IGNORECASE,
    )
    if match:
        return unquote(match.group(1).strip().strip('"'))

    # Try standard filename="value" (with quotes)
    match = re.search(r'filename\s*=\s*"([^"]+)"', header, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Try filename=value (without quotes)
    match = re.search(r"filename\s*=\s*([^\s;]+)", header, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"')

    return ""


def _split_filename(filename: str) -> tuple[str, str]:
    """
    Split a filename into name and extension (including the dot).

    Handles compound extensions like .tar.gz.

    Args:
        filename: The filename to split.

    Returns:
        Tuple of (name, extension) where extension includes the dot.
    """
    # Check for compound extensions
    compound_exts = {
        ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz",
    }
    lower = filename.lower()
    for ext in compound_exts:
        if lower.endswith(ext):
            return filename[: -len(ext)], ext

    p = Path(filename)
    return p.stem, p.suffix


def format_speed(bytes_per_sec: float) -> str:
    """
    Format a speed value for display.

    Args:
        bytes_per_sec: Speed in bytes per second.

    Returns:
        Human-readable speed string (e.g. "1.5 MB/s").
    """
    if bytes_per_sec <= 0:
        return "0 B/s"

    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    unit_index = 0
    speed = float(bytes_per_sec)

    while speed >= 1024.0 and unit_index < len(units) - 1:
        speed /= 1024.0
        unit_index += 1

    if unit_index == 0:
        return f"{speed:.0f} {units[unit_index]}"
    return f"{speed:.1f} {units[unit_index]}"


def format_size(size_bytes: int) -> str:
    """
    Format a file size for display.

    Args:
        size_bytes: Size in bytes.

    Returns:
        Human-readable size string (e.g. "1.5 GB").
    """
    if size_bytes < 0:
        return "Unknown"
    if size_bytes == 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    size = float(size_bytes)

    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1

    if unit_index == 0:
        return f"{size:.0f} {units[unit_index]}"
    return f"{size:.2f} {units[unit_index]}"


def format_eta(seconds: float) -> str:
    """
    Format an ETA (estimated time of arrival) for display.

    Args:
        seconds: Remaining seconds.

    Returns:
        Human-readable ETA string (e.g. "2h 15m", "45s", "∞").
    """
    if seconds <= 0 or math.isinf(seconds) or math.isnan(seconds):
        return "∞"

    seconds = int(seconds)

    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs:02d}s"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes:02d}m"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}d {hours:02d}h"


def calculate_eta(
    remaining_bytes: int,
    speed_bps: float,
) -> float:
    """
    Calculate estimated time remaining.

    Args:
        remaining_bytes: Bytes left to download.
        speed_bps: Current speed in bytes per second.

    Returns:
        Estimated seconds remaining (float('inf') if speed is 0).
    """
    if speed_bps <= 0:
        return float("inf")
    return remaining_bytes / speed_bps
