# IDM v2.0 — api.py — audited 2026-03-28
"""
IDM Server — REST API
======================
FastAPI endpoints for the browser extension bridge.

Endpoints:
    POST /api/add        — Add a new download from the extension
    GET  /api/status     — Get status of a specific download
    GET  /api/downloads  — List all downloads (with filters)
    POST /api/pause      — Pause a download
    POST /api/resume     — Resume a download
    POST /api/cancel     — Cancel a download
    GET  /api/config     — Get current configuration
    GET  /api/health     — Health check / ping

All responses follow a consistent JSON envelope:
    { "success": bool, "data": ..., "error": str|null }
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable, Optional
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import secrets
from utils.credentials import get_credential_store
try:
    import keyring
except Exception:
    keyring = None

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.responses import Response
from core.downloader import resolve_url_metadata

log = logging.getLogger("idm.server.api")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  RATE LIMITING                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class RateLimiterState:
    """Token bucket state per client IP."""
    tokens: float
    last_refill: float
    request_count: int = 0


class PerClientRateLimiter:
    """
    Token bucket rate limiter per client IP.
    
    Prevents extension from spamming API endpoints.
    Configurable via server.rate_limit config.
    """
    
    def __init__(self, 
                 requests_per_second: float = 10.0,
                 burst_size: int = 20,
                 max_clients: int = 4096,
                 entry_ttl_seconds: float = 900.0):
        """
        Args:
            requests_per_second: Token refill rate (default 10 req/s)
            burst_size: Maximum burst tokens (default 20, ~2 seconds of requests)
        """
        self.rate = requests_per_second
        self.burst_size = burst_size
        self.max_clients = max(128, int(max_clients))
        self.entry_ttl_seconds = max(60.0, float(entry_ttl_seconds))
        self.states: OrderedDict[str, RateLimiterState] = OrderedDict()

    def _get_or_create_state(self, client_ip: str, now: float) -> RateLimiterState:
        self._evict_expired(now)

        state = self.states.pop(client_ip, None)
        if state is None:
            if len(self.states) >= self.max_clients:
                self.states.popitem(last=False)
            state = RateLimiterState(tokens=float(self.burst_size), last_refill=now)
        self.states[client_ip] = state
        return state

    def _evict_expired(self, now: float) -> None:
        # OrderedDict keeps insertion/access order; prune oldest entries first.
        stale: list[str] = []
        for key, state in self.states.items():
            if now - state.last_refill > self.entry_ttl_seconds:
                stale.append(key)
            else:
                # Stop early once we reach recent entries.
                break
        for key in stale:
            self.states.pop(key, None)
    
    def is_allowed(self, client_ip: str) -> bool:
        """
        Check if client can make a request.
        
        Returns True if under limit, False if rate limited.
        """
        now = time.time()
        state = self._get_or_create_state(client_ip, now)
        
        # Refill tokens based on elapsed time
        elapsed = now - state.last_refill
        state.tokens = min(
            float(self.burst_size),
            state.tokens + elapsed * self.rate
        )
        state.last_refill = now
        
        # Check if we have a token
        if state.tokens >= 1.0:
            state.tokens -= 1.0
            state.request_count += 1
            return True
        
        return False
    
    def get_stats(self, client_ip: str) -> dict[str, Any]:
        """Get rate limiter stats for a client."""
        now = time.time()
        state = self._get_or_create_state(client_ip, now)
        return {
            "tokens": float(f"{state.tokens:.2f}"),
            "requests_total": state.request_count,
            "rate_limit": self.rate,
            "burst_size": self.burst_size,
            "tracked_clients": len(self.states),
            "max_clients": self.max_clients,
        }


def _generate_pairing_code() -> str:
    """Generate short one-time pairing code visible to the desktop user."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _normalize_pairing_code(value: str | None) -> str:
    """Normalize pairing code by stripping separators and uppercasing."""
    raw = str(value or "").strip().upper()
    return re.sub(r"[^A-Z0-9]", "", raw)


class SessionTokenManager:
    """In-memory bearer session token store with expiry and pruning."""

    TOKEN_SERVICE_NAME = "IDM_Bridge"
    TOKEN_STORE_KEY = "extension_token"

    def __init__(self, ttl_seconds: int = 30 * 24 * 60 * 60) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._tokens: dict[str, float] = {}
        self._store = None
        try:
            self._store = get_credential_store()
        except Exception:
            self._store = None

    def _prune(self) -> None:
        now = time.time()
        expired = [token for token, exp in self._tokens.items() if exp <= now]
        for token in expired:
            self._tokens.pop(token, None)

    def issue(self) -> tuple[str, float]:
        self._prune()
        token = str(uuid.uuid4())
        expires_at = time.time() + self.ttl_seconds
        self._tokens[token] = expires_at
        self._persist(token, expires_at)
        return token, expires_at

    def issue_specific(self, token: str, expires_at: float) -> tuple[str, float]:
        """Register a known token (used when restoring from keyring)."""
        self._prune()
        token_value = str(token or "").strip()
        if not token_value:
            return self.issue()
        exp = float(expires_at)
        self._tokens[token_value] = exp
        self._persist(token_value, exp)
        return token_value, exp

    def _persist(self, token: str, expires_at: float) -> None:
        payload = json.dumps({"token": token, "expires_at": int(expires_at)})

        if keyring is not None:
            try:
                keyring.set_password(self.TOKEN_SERVICE_NAME, self.TOKEN_STORE_KEY, payload)
                return
            except Exception:
                log.warning("Keyring persist failed; using fallback store", exc_info=True)

        if self._store is None:
            return
        try:
            self._store.store(self.TOKEN_STORE_KEY, payload)
        except Exception:
            log.warning("Failed to persist extension token", exc_info=True)

    def load_persisted(self) -> tuple[Optional[str], float]:
        """Load persisted token from keyring if valid and unexpired."""
        raw = None
        if keyring is not None:
            try:
                raw = keyring.get_password(self.TOKEN_SERVICE_NAME, self.TOKEN_STORE_KEY)
            except Exception:
                raw = None

        if raw:
            try:
                parsed = json.loads(raw)
                token = str(parsed.get("token", "")).strip()
                expires_at = float(parsed.get("expires_at", 0.0))
                if not token or expires_at <= time.time():
                    self.clear(token)
                    return None, 0.0
                self._tokens[token] = expires_at
                return token, expires_at
            except Exception:
                pass

        if self._store is None:
            return None, 0.0
        try:
            raw = self._store.retrieve(self.TOKEN_STORE_KEY)
            if not raw:
                return None, 0.0
            parsed = json.loads(raw)
            token = str(parsed.get("token", "")).strip()
            expires_at = float(parsed.get("expires_at", 0.0))
            if not token or expires_at <= time.time():
                self.clear(token)
                return None, 0.0
            self._tokens[token] = expires_at
            return token, expires_at
        except Exception:
            log.warning("Failed to load persisted extension token", exc_info=True)
            return None, 0.0

    def touch(self, token: str) -> Optional[float]:
        """Refresh token TTL (rolling expiration) and persist updated expiry."""
        self._prune()
        current = self._tokens.get(token)
        if current is None:
            return None
        expires_at = time.time() + self.ttl_seconds
        self._tokens[token] = expires_at
        self._persist(token, expires_at)
        return expires_at

    def clear(self, token: Optional[str] = None) -> None:
        """Clear a specific token or all tokens and remove persisted entry."""
        if token:
            self._tokens.pop(str(token), None)
        else:
            self._tokens.clear()

        if keyring is not None:
            try:
                keyring.delete_password(self.TOKEN_SERVICE_NAME, self.TOKEN_STORE_KEY)
            except Exception:
                pass

        if self._store is not None:
            try:
                self._store.delete(self.TOKEN_STORE_KEY)
            except Exception:
                log.warning("Failed to clear persisted extension token", exc_info=True)

    def is_valid(self, token: str) -> bool:
        self._prune()
        exp = self._tokens.get(token)
        if exp is None:
            return False
        if exp <= time.time():
            self._tokens.pop(token, None)
            return False
        return True


def _get_client_ip(request: Request, server_cfg: dict[str, Any]) -> str:
    """Resolve client IP without trusting spoofable headers by default."""
    trusted_proxy_mode = bool(server_cfg.get("trusted_proxy_mode", False))
    trusted_proxies = {
        str(v).strip() for v in server_cfg.get("trusted_proxy_ips", []) if str(v).strip()
    }

    peer_ip = request.client.host if request.client else "unknown"
    if not trusted_proxy_mode:
        return peer_ip

    if trusted_proxies and peer_ip not in trusted_proxies:
        return peer_ip

    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or peer_ip


def _allowed_save_roots(config: dict[str, Any]) -> list[Path]:
    """Build allowlisted save roots from configured download directory/categories."""
    general = config.get("general", {}) if isinstance(config, dict) else {}
    categories = config.get("categories", {}) if isinstance(config, dict) else {}

    download_root = Path(str(general.get("download_directory", "") or "").strip() or r"D:\idm down")
    roots = {download_root.resolve(strict=False)}

    if isinstance(categories, dict):
        for category in categories.keys():
            roots.add((download_root / str(category)).resolve(strict=False))

    return sorted(roots, key=lambda p: len(str(p)))


def _is_path_within_roots(candidate: Path, roots: list[Path]) -> bool:
    """Return True if candidate resolves inside one of the allowlisted roots."""
    candidate_resolved = candidate.resolve(strict=False)
    for root in roots:
        root_resolved = root.resolve(strict=False)
        try:
            candidate_resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def _canonicalize_save_path(raw_save_path: str, config: dict[str, Any]) -> tuple[Optional[str], bool]:
    """Canonicalize and validate caller-provided save path against allowlisted roots."""
    raw = str(raw_save_path or "").strip()
    if not raw:
        return None, False

    roots = _allowed_save_roots(config)
    base_root = roots[0] if roots else Path(r"D:\idm down").resolve(strict=False)

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (base_root / candidate)

    canonical = candidate.resolve(strict=False)
    in_root = _is_path_within_roots(canonical, roots)
    return str(canonical), in_root


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  REQUEST / RESPONSE MODELS                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class AddDownloadRequest(BaseModel):
    """Request body for POST /api/add."""
    url: str
    filename: Optional[str] = ""
    filename_hint: Optional[str] = None  # Suggested filename from page context
    page_url: Optional[str] = None
    referer: Optional[str] = None
    cookies: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    disabled_sites: Optional[list[str]] = None
    priority: str = "normal"
    category: Optional[str] = None
    hash_expected: Optional[str] = None
    save_path: Optional[str] = None
    confirm_out_of_root: bool = False


class ResolveUrlRequest(BaseModel):
    """Request body for POST /api/resolve."""
    url: str
    referer: Optional[str] = None
    cookies: Optional[str] = None
    headers: Optional[dict[str, str]] = None


class PairRequest(BaseModel):
    pairing_code: str


class DownloadIdRequest(BaseModel):
    """Request body for pause/resume/cancel operations."""
    download_id: str


class StreamDownloadRequest(BaseModel):
    """Request body for stream capture downloads."""
    url: str
    type: str = Field(default="direct", pattern="^(hls|dash|direct|blob)$")
    referer: Optional[str] = None
    cookies: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    save_path: Optional[str] = None


class BatchDownloadRequest(BaseModel):
    """Request body for batch URL enqueue."""
    urls: list[str] = Field(default_factory=list)
    save_path: Optional[str] = None
    category: Optional[str] = "Auto"
    confirm_out_of_root: bool = False


class SpeedLimitRequest(BaseModel):
    """Request body for global speed limit update."""
    limit_kbps: int = Field(default=0, ge=0)


class LogLevelRequest(BaseModel):
    """Request body for updating runtime log levels."""
    logger: str = Field(default="idm")
    level: str = Field(pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")


class ApiResponse(BaseModel):
    """Standard API response envelope."""
    success: bool = True
    data: Any = None
    error: Optional[str] = None

    def __init__(self, success: bool = True, data: Any = None, error: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(success=success, data=data, error=error, **kwargs)  # type: ignore[call-arg]


def _is_supported_download_url(url: str) -> bool:
    """Allow only bridge-supported URL schemes."""
    if not url or not isinstance(url, str):
        return False

    if url.startswith("magnet:"):
        return True

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    return parsed.scheme in {"http", "https", "ftp"}


def _error_json(message: str, status_code: int = 400) -> JSONResponse:
    """Return API errors in a stable extension-friendly shape."""
    return JSONResponse(status_code=status_code, content={"error": str(message)})


def _normalize_host(value: Optional[str]) -> str:
    """Normalize hostnames for reliable subdomain matching."""
    host = str(value or "").strip().lower().strip(".")
    return host


def _extract_host(value: Optional[str]) -> str:
    """Extract hostname from a URL or a host-like string."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    try:
        parsed = urlparse(raw)
        if parsed.scheme:
            return _normalize_host(parsed.hostname)
    except Exception:
        return ""

    if "/" in raw:
        raw = raw.split("/", 1)[0]
    if ":" in raw:
        raw = raw.split(":", 1)[0]
    return _normalize_host(raw)


def _normalize_disabled_site_rule(value: Optional[str]) -> str:
    """Normalize disabled-site rule values coming from extension settings."""
    rule = str(value or "").strip().lower()
    if not rule:
        return ""
    if rule.startswith("*."):
        host = _extract_host(rule[2:])
        return f"*.{host}" if host else ""
    return _extract_host(rule)


def _host_matches_rule(host: str, rule: str) -> bool:
    """Match host to exact or wildcard disabled-site rules."""
    host = _normalize_host(host)
    rule = _normalize_disabled_site_rule(rule)
    if not host or not rule:
        return False

    if rule.startswith("*."):
        suffix = rule[2:]
        return host == suffix or host.endswith(f".{suffix}")

    return host == rule or host.endswith(f".{rule}")


def _is_blocked_by_disabled_sites(req: AddDownloadRequest) -> bool:
    """Return True when request context matches extension disabled-site rules."""
    rules = [
        normalized
        for normalized in (_normalize_disabled_site_rule(v) for v in (req.disabled_sites or []))
        if normalized
    ]
    if not rules:
        return False

    candidate_hosts = [
        _extract_host(req.page_url),
        _extract_host(req.referer),
        _extract_host(req.url),
    ]

    for host in candidate_hosts:
        if not host:
            continue
        if any(_host_matches_rule(host, rule) for rule in rules):
            return True
    return False


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  APP FACTORY                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def create_app(
    engine: Any = None,
    storage: Any = None,
    config: Optional[dict[str, Any]] = None,
) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        engine: The DownloadEngine instance (can be None for testing).
        storage: The StorageManager instance (can be None for testing).
        config: Application configuration dict.

    Returns:
        Configured FastAPI application.
    """
    config = config or {}
    server_cfg = config.get("server", {})

    app = FastAPI(
        title="IDM Bridge Server",
        description="Browser extension bridge for Internet Download Manager",
        version="1.0.0",
        docs_url="/docs" if server_cfg.get("enable_docs", False) else None,
    )

    # Store references in app state
    app.state.engine = engine
    app.state.storage = storage
    app.state.config = config
    app.state.add_download_interceptor = None
    app.state.session_tokens = SessionTokenManager(ttl_seconds=30 * 24 * 60 * 60)
    restored_token, restored_expiry = app.state.session_tokens.load_persisted()
    if restored_token:
        app.state.pairing_code = None
        app.state.pairing_code_expires_at = 0.0
        log.info("Restored persisted extension token (expires_at=%s)", int(restored_expiry))
    else:
        app.state.pairing_code = _generate_pairing_code()
        app.state.pairing_code_expires_at = time.time() + 5 * 60
        log.info(
            "Bridge pairing code generated (expires in 5 minutes): %s",
            app.state.pairing_code,
        )

    # ── Rate Limiting ──────────────────────────────────────────────────
    rate_limit_cfg = server_cfg.get("rate_limit", {})
    rate_limiter = PerClientRateLimiter(
        requests_per_second=rate_limit_cfg.get("requests_per_second", 10.0),
        burst_size=rate_limit_cfg.get("burst_size", 20),
        max_clients=rate_limit_cfg.get("max_clients", 4096),
        entry_ttl_seconds=rate_limit_cfg.get("entry_ttl_seconds", 900),
    )
    app.state.rate_limiter = rate_limiter

    @app.middleware("http")
    async def rate_limit_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Rate limiter middleware — throttle per client IP."""
        # Skip rate limiting for health checks
        if request.url.path == "/api/health":
            return await call_next(request)
        
        cfg: dict[str, Any] = app.state.config or {}
        server_cfg_local: dict[str, Any] = cfg.get("server", {})
        client_ip = _get_client_ip(request, server_cfg_local)
        
        # Check rate limit
        if not rate_limiter.is_allowed(client_ip):
            return _error_json(
                "Rate limit exceeded. Max 10 requests/sec per client.",
                status_code=429,
            )
        
        return await call_next(request)

    # ── CORS ───────────────────────────────────────────────────────────
    # Keep backward compatibility with both legacy and current config keys.
    allowed_origins = server_cfg.get("cors_origins") or server_cfg.get("allowed_origins") or [
        "chrome-extension://*",
        "moz-extension://*",
        "http://localhost:*",
        "https://localhost:*",
    ]

    # Normalize wildcard-like entries (e.g. chrome-extension://*) into regex,
    # while preserving exact origins in allow_origins.
    wildcard_entries: list[str] = [str(o) for o in allowed_origins if "*" in str(o)]
    explicit_entries: list[str] = [str(o) for o in allowed_origins if "*" not in str(o)]

    allow_origin_regex = None
    if wildcard_entries:
        escaped_parts: list[str] = []
        for origin in wildcard_entries:
            # Convert simple '*' glob into a regex-safe pattern.
            escaped_parts.append(re.escape(origin).replace(r"\*", ".*"))
        allow_origin_regex = "^(?:" + "|".join(escaped_parts) + ")$"

    # Enhanced CORS patterns for better security:
    # • chrome-extension://[ext-id] only (not wildcard)
    # • moz-extension://[ext-id] only
    # • localhost variants with port ranges
    
    explicit_extensions: list[str] = []  # Specific extension IDs
    wildcard_entries_improved: list[str] = []
    
    for origin in wildcard_entries:
        # Extract extension-specific patterns
        if "chrome-extension://" in origin:
            # Pattern: chrome-extension://knlidmgjddjekmkkkljpkddelceealgi/*
            # Extract extension ID if available
            match = re.match(r"chrome-extension://([a-z]{32})/?\*?", origin)
            if match:
                ext_id = match.group(1)
                # Validate extension ID format (32 lowercase letters)
                if len(ext_id) == 32 and ext_id.islower():
                    explicit_extensions.append(f"chrome-extension://{ext_id}")
                else:
                    # Fall back to pattern allowing any chrome extension
                    wildcard_entries_improved.append("chrome-extension://[a-z]{32}")
            else:
                # Accept any chrome extension (less secure)
                log.warning(
                    "CORS config uses wildcard chrome-extension://* — "
                    "recommend specifying extension ID for better security"
                )
                wildcard_entries_improved.append("chrome-extension://[a-z]{32}")
        
        elif "moz-extension://" in origin:
            # Firefox extension pattern
            match = re.match(r"moz-extension://([a-f0-9-]+)/?\*?", origin)
            if match:
                ext_id = match.group(1)
                explicit_extensions.append(f"moz-extension://{ext_id}")
            else:
                log.warning(
                    "CORS config uses wildcard moz-extension://* — "
                    "recommend specifying extension ID for better security"
                )
                wildcard_entries_improved.append("moz-extension://[a-f0-9-]+")
        
        elif "localhost" in origin or "127.0.0.1" in origin:
            # localhost development patterns
            if "localhost:*" in origin or "127.0.0.1:*" in origin:
                # Allow any port on localhost (safe for development)
                wildcard_entries_improved.append("http?://(?:localhost|127\\.0\\.0\\.1):\\d+")
            else:
                explicit_entries.append(origin)
        else:
            # Generic wildcard patterns
            escaped = re.escape(origin).replace(r"\*", ".*")
            wildcard_entries_improved.append(escaped)
    
    # Build final CORS configuration
    final_explicit = explicit_entries + explicit_extensions
    final_regex_parts = wildcard_entries_improved
    
    allow_origin_regex = None
    if final_regex_parts:
        allow_origin_regex = "^(?:" + "|".join(f"(?:{p})" for p in final_regex_parts) + ")$"

    app.add_middleware(
        CORSMiddleware,
        allow_origins=final_explicit,
        allow_origin_regex=allow_origin_regex,
        # Bridge requests use explicit headers; cookie credentials are unnecessary
        # and increase cross-origin risk on localhost services.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # ── Auth middleware ─────────────────────────────────────────────────
    @app.middleware("http")
    async def auth_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Validate auth token if configured (per-request lookup to get latest config)."""
        public_paths = (
            "/api/health",
            "/docs",
            "/openapi.json",
            "/api/pair",
            "/api/auth/reset",
        )
        if request.url.path in public_paths:
            return await call_next(request)

        auth_header = str(request.headers.get("Authorization", "")).strip()
        if not auth_header.lower().startswith("bearer "):
            return _error_json("Missing bearer token", status_code=401)

        token = auth_header[7:].strip()
        if not token or not app.state.session_tokens.is_valid(token):
            return _error_json("Invalid or expired session token", status_code=401)

        app.state.session_tokens.touch(token)
        return await call_next(request)

    # ── Routes ─────────────────────────────────────────────────────────
    _register_routes(app)

    # ── Exception Handlers ─────────────────────────────────────────────
    @app.exception_handler(HTTPException)
    async def _handle_http_exception(_request: Request, exc: HTTPException) -> JSONResponse:
        message = str(exc.detail or "Request failed")
        return _error_json(message, status_code=int(exc.status_code or 500))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_exception(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        message = str(first.get("msg") or "Invalid request payload")
        return _error_json(message, status_code=422)

    @app.exception_handler(Exception)
    async def _handle_unexpected_exception(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled API exception")
        return _error_json(str(exc), status_code=500)

    return app


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ROUTES                                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _register_routes(app: FastAPI) -> None:
    """Register all API routes."""

    @app.get("/api/health")
    async def health_check() -> ApiResponse:
        """Health check endpoint — always returns OK."""
        cfg: dict[str, Any] = app.state.config or {}
        server_cfg: dict[str, Any] = cfg.get("server", {})
        pairing_expires_in = max(0, int(app.state.pairing_code_expires_at - time.time()))
        return ApiResponse(data={
            "status": "ok",
            "version": "1.0.0",
            "auth_required": True,
            "host": server_cfg.get("host", "127.0.0.1"),
            "port": server_cfg.get("port", 6800),
            "pairing_pending": bool(app.state.pairing_code) and pairing_expires_in > 0,
            "pairing_expires_in_seconds": pairing_expires_in,
        })

    @app.post("/api/pair")
    async def pair_extension(req: PairRequest, request: Request) -> ApiResponse:
        """Exchange one-time desktop pairing code for a 30-day bearer session token."""
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "localhost", "::1"):
            return _error_json("Only localhost pairing is allowed", status_code=403)

        now = time.time()
        code = _normalize_pairing_code(req.pairing_code)
        valid_code = _normalize_pairing_code(app.state.pairing_code)

        if not valid_code or now > float(app.state.pairing_code_expires_at):
            app.state.pairing_code = _generate_pairing_code()
            app.state.pairing_code_expires_at = time.time() + 5 * 60
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Pairing code expired. A new code has been generated in IDM.",
                    "pairing_code": app.state.pairing_code,
                    "pairing_expires_in_seconds": 5 * 60,
                },
            )

        if not code or not secrets.compare_digest(code, valid_code):
            return _error_json("Invalid pairing code", status_code=403)

        token, expires_at = app.state.session_tokens.issue()

        # Rotate to a fresh one-time code so UI can keep showing a valid code.
        app.state.pairing_code = _generate_pairing_code()
        app.state.pairing_code_expires_at = time.time() + 5 * 60

        return ApiResponse(data={
            "session_token": token,
            "expires_at": int(expires_at),
            "expires_in_seconds": int(max(0, expires_at - now)),
            "token_type": "Bearer",
        })

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> ApiResponse:
        """Validate current bearer token and return auth state details."""
        auth_header = str(request.headers.get("Authorization", "")).strip()
        if not auth_header.lower().startswith("bearer "):
            return _error_json("Missing bearer token", status_code=401)

        token = auth_header[7:].strip()
        if not token or not app.state.session_tokens.is_valid(token):
            return _error_json("Invalid or expired session token", status_code=401)

        refreshed = app.state.session_tokens.touch(token)
        return ApiResponse(data={
            "paired": True,
            "expires_at": int(refreshed or 0),
        })

    @app.post("/api/auth/reset")
    async def auth_reset(request: Request) -> ApiResponse:
        """Reset persisted token and issue a new pairing code (localhost only)."""
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "localhost", "::1"):
            return _error_json("Only localhost reset is allowed", status_code=403)

        app.state.session_tokens.clear()
        app.state.pairing_code = _generate_pairing_code()
        app.state.pairing_code_expires_at = time.time() + 5 * 60

        return ApiResponse(data={
            "pairing_code": app.state.pairing_code,
            "pairing_expires_in_seconds": 5 * 60,
        })

    @app.post("/api/download")
    async def add_download_alias(req: AddDownloadRequest) -> ApiResponse:
        """Compatibility alias for /api/add used by inline button capture."""
        result = await add_download(req)
        if isinstance(result, JSONResponse):
            return result

        payload: dict[str, Any] = {}
        if isinstance(result, ApiResponse):
            payload = result.data if isinstance(result.data, dict) else {}
        elif isinstance(result, dict):
            payload = result.get("data") if isinstance(result.get("data"), dict) else result

        download_id = str(payload.get("download_id", "")).strip()
        if not download_id:
            return result

        return {
            "success": True,
            "download_id": download_id,
            "message": "Download added to queue",
            "data": {
                "download_id": download_id,
            },
        }

    @app.post("/api/resolve")
    async def resolve_url(req: ResolveUrlRequest) -> ApiResponse:
        """Resolve redirects and classify final URL as binary/html/unverified."""
        raw_url = str(req.url or "").strip()
        if not raw_url:
            return _error_json("URL is required", status_code=400)

        resolved = await resolve_url_metadata(
            raw_url,
            referer=str(req.referer or "").strip() or None,
            cookies=str(req.cookies or "").strip() or None,
            headers=req.headers if isinstance(req.headers, dict) else None,
            timeout_seconds=3.0,
            max_redirects=5,
        )

        return ApiResponse(data={
            "requestedUrl": resolved.requested_url,
            "finalUrl": resolved.final_url,
            "filename": resolved.filename,
            "contentType": resolved.content_type,
            "contentDisposition": resolved.content_disposition,
            "contentLength": resolved.content_length,
            "resumeSupported": resolved.resume_supported,
            "redirected": resolved.redirected,
            "isHtmlPage": resolved.is_html_page,
            "isBinary": resolved.is_binary,
            "verified": resolved.verified,
            "verificationMethod": resolved.verification_method,
            "warning": resolved.warning,
            "error": resolved.error,
        })

    @app.post("/api/add")
    async def add_download(req: AddDownloadRequest) -> ApiResponse:
        """Add a new download from the browser extension."""
        if _is_blocked_by_disabled_sites(req):
            log.info("Blocked /api/add due to extension disabled-site policy: %s", req.url[:120])
            return _error_json("Blocked: site is disabled in extension settings", status_code=403)

        resolved_url = str(req.url)
        resolved_filename = str(req.filename or "")
        effective_referer = str(req.referer or req.page_url or "").strip() or None
        custom_user_agent = None
        request_headers: dict[str, str] = {}
        if req.headers and isinstance(req.headers, dict):
            custom_user_agent = req.headers.get("User-Agent") or req.headers.get("user-agent")
            for key, value in req.headers.items():
                k = str(key).strip()
                v = str(value).strip()
                if not k or not v:
                    continue
                if k.lower() in {"host", "content-length"}:
                    continue
                request_headers[k] = v

        if effective_referer and not any(k.lower() == "referer" for k in request_headers):
            request_headers["Referer"] = effective_referer

        parsed_resolve = urlparse(str(req.url or ""))
        if parsed_resolve.scheme in {"http", "https"}:
            resolved = await resolve_url_metadata(
                str(req.url),
                referer=effective_referer,
                cookies=req.cookies,
                headers=request_headers,
                timeout_seconds=3.0,
                max_redirects=5,
            )
            if resolved.verified and resolved.is_binary and resolved.final_url:
                resolved_url = resolved.final_url
                if not resolved_filename and resolved.filename:
                    resolved_filename = resolved.filename
            
            # Use filename_hint as fallback if resolve didn't provide a filename
            if not resolved_filename and req.filename_hint:
                resolved_filename = str(req.filename_hint).strip()

        if resolved_url != str(req.url):
            req.url = resolved_url
        if resolved_filename and not str(req.filename or "").strip():
            req.filename = resolved_filename

        interceptor = getattr(app.state, "add_download_interceptor", None)
        if interceptor is not None:
            try:
                intercepted = await interceptor(req)
                download_id = str(intercepted.get("download_id", "")).strip()
                if not download_id:
                    return _error_json(str(intercepted.get("error", "Download was canceled")), status_code=400)
                return ApiResponse(data={"download_id": download_id})
            except Exception as exc:
                log.exception("Intercepted add-download failed")
                return _error_json(str(exc), status_code=500)

        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)

        if not _is_supported_download_url(req.url):
            return _error_json(
                "Unsupported URL scheme. Use http/https/ftp/magnet URLs.",
                status_code=400,
            )

        cfg: dict[str, Any] = app.state.config or {}
        canonical_save_path = None
        if req.save_path:
            canonical_save_path, in_root = _canonicalize_save_path(req.save_path, cfg)
            if not in_root and not bool(req.confirm_out_of_root):
                return _error_json(
                    "Save path is outside allowed download roots. Explicit UI confirmation is required.",
                    status_code=403,
                )

        try:
            metadata_json = None
            if request_headers:
                metadata_json = json.dumps({"request_headers": request_headers})

            download_id = await engine.add_download(
                url=resolved_url,
                filename=resolved_filename,
                save_path=canonical_save_path or "",
                priority=req.priority,
                category=req.category or "Other",
                referer=effective_referer,
                cookies=req.cookies,
                user_agent=custom_user_agent,
                metadata_json=metadata_json,
                hash_expected=req.hash_expected,
                allow_out_of_root=bool(req.confirm_out_of_root),
            )
            log.info("Download added via API: %s → %s", download_id[:8], resolved_url[:60])  # type: ignore
            return ApiResponse(data={"download_id": download_id})

        except Exception as exc:
            log.exception("Failed to add download")
            return _error_json(str(exc), status_code=500)

    @app.post("/api/download/stream")
    async def add_stream_download(req: StreamDownloadRequest) -> ApiResponse:
        """Add captured stream URL as a download job."""
        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)

        if not _is_supported_download_url(req.url):
            return _error_json("Unsupported stream URL", status_code=400)

        cfg: dict[str, Any] = app.state.config or {}
        canonical_save_path = ""
        requested_path = str(req.save_path or "").strip()
        if requested_path:
            resolved_path, in_root = _canonicalize_save_path(requested_path, cfg)
            if not in_root:
                return _error_json(
                    "Save path is outside allowed download roots.",
                    status_code=403,
                )
            canonical_save_path = resolved_path

        metadata_json = json.dumps({"stream_type": req.type})
        filename = str(req.title or "").strip()
        category = str(req.category or "Video").strip() or "Video"

        try:
            download_id = await engine.add_download(
                url=req.url,
                filename=filename,
                category=category,
                save_path=canonical_save_path,
                referer=str(req.referer or "").strip() or None,
                cookies=str(req.cookies or "").strip() or None,
                metadata_json=metadata_json,
            )
            return ApiResponse(data={
                "download_id": download_id,
                "type": req.type,
            })
        except Exception as exc:
            log.exception("Failed to add stream download")
            return _error_json(str(exc), status_code=500)

    @app.post("/api/download/batch")
    async def add_batch_download(req: BatchDownloadRequest) -> ApiResponse:
        """Enqueue a batch of URLs and return assigned IDs."""
        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)

        raw_urls = [str(u or "").strip() for u in req.urls]
        urls = [u for u in raw_urls if u]
        if not urls:
            return _error_json("No URLs provided", status_code=400)

        if len(urls) > 500:
            return _error_json("Batch too large (max 500)", status_code=413)

        category = str(req.category or "Auto").strip() or "Auto"
        save_path = str(req.save_path or "").strip()
        cfg: dict[str, Any] = app.state.config or {}
        canonical_save_path = ""
        if save_path:
            resolved_path, in_root = _canonicalize_save_path(save_path, cfg)
            if not in_root and not bool(req.confirm_out_of_root):
                return _error_json(
                    "Save path is outside allowed download roots. Explicit UI confirmation is required.",
                    status_code=403,
                )
            canonical_save_path = resolved_path

        assigned: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []

        for url in urls:
            if not _is_supported_download_url(url):
                errors.append({"url": url, "error": "Unsupported URL scheme"})
                continue
            try:
                download_id = await engine.add_download(
                    url=url,
                    category=category,
                    save_path=canonical_save_path,
                    allow_out_of_root=bool(req.confirm_out_of_root),
                )
                assigned.append({"url": url, "download_id": download_id})
            except Exception as exc:
                errors.append({"url": url, "error": str(exc)})

        return ApiResponse(data={
            "assigned": assigned,
            "errors": errors,
            "requested": len(urls),
            "queued": len(assigned),
        })

    async def _queue_state(download_id: str) -> dict[str, Any]:
        """Return current queue state snapshot for a single download id."""
        storage = app.state.storage
        engine = app.state.engine
        record = await storage.get_download(download_id) if storage else None
        if not record:
            raise HTTPException(status_code=404, detail="Download not found")
        active_speed = 0.0
        if engine is not None:
            active_speed = float(engine.get_active_speeds().get(download_id, 0.0))
        remaining = max(0, int(record.file_size) - int(record.downloaded_bytes))
        eta_seconds = (remaining / active_speed) if active_speed > 0 and remaining > 0 else None
        return {
            "id": record.id,
            "status": record.status,
            "progress_percent": record.progress_percent,
            "speed": active_speed,
            "eta_seconds": eta_seconds,
        }

    @app.get("/api/queue")
    async def get_queue() -> ApiResponse:
        """Return active and queued download entries for extension queue UI."""
        storage = app.state.storage
        if not storage:
            return _error_json("Storage not available", status_code=503)

        engine = app.state.engine
        speeds = engine.get_active_speeds() if engine else {}

        records = await storage.get_all_downloads(limit=500, offset=0)
        visible_statuses = {"queued", "downloading", "paused", "failed"}
        queue_items: list[dict[str, Any]] = []

        for record in records:
            if record.status not in visible_statuses:
                continue

            speed = float(speeds.get(record.id, 0.0))
            remaining = max(0, int(record.file_size) - int(record.downloaded_bytes))
            eta_seconds = (remaining / speed) if speed > 0 and remaining > 0 else None

            queue_items.append({
                "id": record.id,
                "url": record.url,
                "filename": record.filename,
                "status": record.status,
                "file_size": record.file_size,
                "downloaded_bytes": record.downloaded_bytes,
                "progress_percent": record.progress_percent,
                "speed": speed,
                "eta_seconds": eta_seconds,
            })

        queue_items.sort(key=lambda x: (x.get("status") != "downloading", x.get("filename", "")))
        return ApiResponse(data={"downloads": queue_items})

    @app.post("/api/queue/{download_id}/pause")
    async def queue_pause(download_id: str) -> ApiResponse:
        """Pause a download by ID from queue control UI."""
        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)
        await engine.pause(download_id)
        return ApiResponse(data={"action": "paused", "download": await _queue_state(download_id)})

    @app.post("/api/queue/{download_id}/resume")
    async def queue_resume(download_id: str) -> ApiResponse:
        """Resume a download by ID from queue control UI."""
        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)
        await engine.resume(download_id)
        return ApiResponse(data={"action": "resumed", "download": await _queue_state(download_id)})

    @app.post("/api/queue/{download_id}/cancel")
    async def queue_cancel(download_id: str) -> ApiResponse:
        """Cancel a download by ID from queue control UI."""
        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)
        await engine.cancel(download_id)
        return ApiResponse(data={"action": "cancelled", "download": await _queue_state(download_id)})

    @app.post("/api/settings/speed_limit")
    async def set_speed_limit(req: SpeedLimitRequest) -> ApiResponse:
        """Update global bandwidth limit for running network manager."""
        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)

        cfg: dict[str, Any] = app.state.config or {}
        network_cfg = cfg.setdefault("network", {})
        network_cfg["bandwidth_limit_kbps"] = int(req.limit_kbps)
        app.state.config = cfg

        if hasattr(engine, "network_manager") and engine.network_manager is not None:
            engine.network_manager.set_global_rate(int(req.limit_kbps))

        return ApiResponse(data={
            "limit_kbps": int(req.limit_kbps),
            "unlimited": int(req.limit_kbps) == 0,
        })

    @app.get("/api/status/{download_id}")
    async def get_status(download_id: str) -> ApiResponse:
        """Get the status of a specific download."""
        storage = app.state.storage
        if not storage:
            return _error_json("Storage not available", status_code=503)

        record = await storage.get_download(download_id)
        if not record:
            raise HTTPException(status_code=404, detail="Download not found")

        engine = app.state.engine
        speed = 0.0
        if engine:
            speeds = engine.get_active_speeds()
            speed = speeds.get(download_id, 0.0)

        return ApiResponse(data={
            "id": record.id,
            "url": record.url,
            "filename": record.filename,
            "status": record.status,
            "file_size": record.file_size,
            "downloaded_bytes": record.downloaded_bytes,
            "progress_percent": record.progress_percent,
            "speed": speed,
            "priority": record.priority,
            "category": record.category,
            "date_added": record.date_added,
            "error_message": record.error_message,
        })

    @app.get("/api/downloads")
    async def list_downloads(
        status: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ApiResponse:
        """List downloads with optional filters."""
        storage = app.state.storage
        if not storage:
            return _error_json("Storage not available", status_code=503)

        from core.storage import DownloadStatus as DS  # type: ignore[import-not-found]
        status_filter = None
        if status:
            try:
                status_filter = DS(status)
            except ValueError:
                return _error_json(f"Invalid status: {status}", status_code=400)

        downloads = await storage.get_all_downloads(
            status=status_filter,
            category=category,
            limit=limit,
            offset=offset,
        )

        return ApiResponse(data={
            "downloads": [
                {
                    "id": d.id,
                    "url": d.url,
                    "filename": d.filename,
                    "status": d.status,
                    "file_size": d.file_size,
                    "downloaded_bytes": d.downloaded_bytes,
                    "progress_percent": d.progress_percent,
                    "priority": d.priority,
                    "category": d.category,
                    "date_added": d.date_added,
                    "chunks_count": d.chunks_count,
                }
                for d in downloads
            ],
            "total": await storage.get_download_count(status=status_filter),
        })

    @app.post("/api/pause")
    async def pause_download(req: DownloadIdRequest) -> ApiResponse:
        """Pause an active download."""
        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)

        await engine.pause(req.download_id)
        return ApiResponse(data={"download_id": req.download_id, "action": "paused"})

    @app.post("/api/resume")
    async def resume_download(req: DownloadIdRequest) -> ApiResponse:
        """Resume a paused download."""
        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)

        await engine.resume(req.download_id)
        return ApiResponse(data={"download_id": req.download_id, "action": "resumed"})

    @app.post("/api/cancel")
    async def cancel_download(req: DownloadIdRequest) -> ApiResponse:
        """Cancel a download."""
        engine = app.state.engine
        if not engine:
            return _error_json("Engine not available", status_code=503)

        await engine.cancel(req.download_id)
        return ApiResponse(data={"download_id": req.download_id, "action": "cancelled"})

    @app.get("/api/config")
    async def get_config() -> ApiResponse:
        """Get safe subset of configuration."""
        config: dict[str, Any] = app.state.config or {}
        general: dict[str, Any] = config.get("general", {})
        scheduler: dict[str, Any] = config.get("scheduler", {})
        return ApiResponse(data={
            "max_concurrent": general.get("max_concurrent_downloads", 4),
            "download_directory": general.get("download_directory", ""),
            "categories": list(config.get("categories", {}).keys()),
            "scheduler_enabled": scheduler.get("enabled", False),
        })

    @app.get("/api/stats")
    async def get_stats() -> ApiResponse:
        """Get download statistics."""
        storage = app.state.storage
        if not storage:
            return ApiResponse(success=False, error="Storage not available")

        engine = app.state.engine
        totals = await storage.get_total_statistics()

        return ApiResponse(data={
            **totals,
            "active_downloads": engine.active_count if engine else 0,
            "total_speed": engine.get_total_speed() if engine else 0.0,
        })

    @app.get("/api/debug/rate-limit")
    async def get_rate_limit_stats(request: Request) -> ApiResponse:
        """Get rate limiter stats for this client."""
        rate_limiter = app.state.rate_limiter
        if not rate_limiter:
            return ApiResponse(success=False, error="Rate limiter not available")

        cfg: dict[str, Any] = app.state.config or {}
        server_cfg: dict[str, Any] = cfg.get("server", {})
        client_ip = _get_client_ip(request, server_cfg)

        stats = rate_limiter.get_stats(client_ip)
        return ApiResponse(data=stats)

    @app.get("/api/debug/network")
    async def get_network_debug() -> ApiResponse:
        """Get network connector and limiter diagnostics."""
        engine = app.state.engine
        if not engine or not hasattr(engine, "network_manager"):
            return ApiResponse(success=False, error="Engine/network diagnostics unavailable")

        network = engine.network_manager
        return ApiResponse(data=network.get_pool_stats())

    @app.get("/api/debug/logging")
    async def get_logging_debug() -> ApiResponse:
        """Get effective logger levels for key IDM components."""
        components = [
            "idm",
            "idm.config",
            "idm.core.downloader",
            "idm.core.network",
            "idm.server.api",
        ]

        levels: dict[str, str] = {}
        for name in components:
            logger = logging.getLogger(name)
            level_name = logging.getLevelName(logger.getEffectiveLevel())
            levels[name] = str(level_name)

        return ApiResponse(data={"levels": levels})

    @app.post("/api/debug/logging")
    async def set_logging_level(req: LogLevelRequest) -> ApiResponse:
        """Update logging level for a specific logger at runtime."""
        logger = logging.getLogger(req.logger)
        new_level = getattr(logging, req.level.upper(), None)
        if new_level is None:
            return ApiResponse(success=False, error=f"Invalid level: {req.level}")

        logger.setLevel(new_level)
        return ApiResponse(
            data={
                "logger": req.logger,
                "level": logging.getLevelName(logger.getEffectiveLevel()),
            }
        )
