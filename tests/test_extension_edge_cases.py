"""Static edge-case tests for browser extension assets."""

from __future__ import annotations

import json
from pathlib import Path


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_manifest_has_localhost_bridge_permissions() -> None:
    manifest_path = Path("extension") / "manifest.json"
    manifest = json.loads(_read_text(manifest_path))

    host_permissions = manifest.get("host_permissions", [])
    assert "http://localhost:6677/*" in host_permissions


def test_service_worker_queue_limits_and_retry_alarm_defined() -> None:
    sw = _read_text(Path("extension") / "background" / "service_worker.js")

    assert "MAX_PENDING_QUEUE_ITEMS" in sw
    assert "RETRY_ALARM_NAME" in sw
    assert "RETRY_ALARM_DELAY_MINUTES" in sw


def test_service_worker_cookie_allowlist_supports_wildcard_and_subdomains() -> None:
    sw = _read_text(Path("extension") / "background" / "service_worker.js")

    # Ensure wildcard and subdomain allowlist logic remains in place.
    assert "entry === \"*\"" in sw
    assert "host.endsWith(`.${normalized}`)" in sw


def test_popup_uses_runtime_messages_and_opens_options_for_pairing() -> None:
    popup = _read_text(Path("extension") / "popup" / "popup.js")

    assert "chrome.runtime.sendMessage" in popup
    assert "chrome.runtime.openOptionsPage" in popup
    assert "IDM not running" in popup


def test_content_badge_states_for_resolve_flow() -> None:
    content = _read_text(Path("extension") / "content.js")

    # Pending state while async resolve is in-flight.
    assert "state.resolveStatus = \"pending\"" in content
    assert "setButtonBadgeText(state, \"...\")" in content

    # Verified binary state.
    assert "state.resolveStatus = \"verified\"" in content
    assert "setButtonBadgeText(state, \"+ IDM\")" in content

    # Unverified fallback state.
    assert "state.resolveStatus = \"unverified\"" in content
    assert "setButtonBadgeText(state, \"+ IDM ?\")" in content

    # HTML/error/non-binary resolution removes inline button.
    assert "state.resolveStatus = \"rejected\"" in content
    assert "removeInlineButton(link)" in content
