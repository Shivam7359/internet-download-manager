# IDM Bridge Extension Analysis

Date: 2026-03-25
Scope: `extension/` (Manifest V3 browser extension)

## Executive Summary

The extension is functional and reasonably well-structured for an MVP. Core download capture, popup controls, diagnostics, and retry queue logic are already in place.

Primary improvement areas are:
1. Permission and host scope hardening.
2. Cookie forwarding privacy controls.
3. Packaging/release polish and browser-store readiness.

## What Is Working Well

- MV3 structure is correct (`manifest.json`, service worker, content scripts, popup UI).
- Retry queue exists for transient app outages (`chrome.storage.local` + `chrome.alarms`).
- Multiple capture paths are implemented (context menu, click interception, browser downloads API).
- Popup includes diagnostics and active download status visibility.

## Priority Findings

### P1 - Overly broad permission scope

Current manifest grants:
- Host access to all `http://*/*`, `https://*/*`, `ftp://*/*`.
- `cookies` permission globally.

Impact:
- Increases review friction for browser store submission.
- Raises user trust and privacy concerns, especially combined with cookie access.

Recommendation:
- Reduce host scope where possible.
- Consider `optional_host_permissions` and request at runtime.
- Keep localhost bridge endpoints explicit (`127.0.0.1`, `localhost`) as required.

### P1 - Cookie forwarding should be opt-in

Current behavior:
- Worker may gather cookies for a target URL and send them to local bridge API.

Impact:
- Potentially forwards sensitive session cookies to local app without explicit per-site consent.

Recommendation:
- Add setting: "Forward browser cookies" (default OFF).
- Add domain allowlist for cookie forwarding.
- Log when cookies are sent, with counts only (not values), unless debug mode is explicitly enabled.

### P2 - Content script runs on all pages at document start

Current behavior:
- Global content script execution across all HTTP/HTTPS/FTP pages.

Impact:
- Unnecessary page overhead on sites where capture is never used.
- Increased chance of site compatibility issues.

Recommendation:
- Consider narrowing match patterns or using lazy activation via user action where feasible.
- Keep critical listeners lightweight and avoid aggressive DOM mutation by default.

### P2 - Manifest packaging polish

Current behavior:
- No explicit `icons` field in manifest.
- No `minimum_chrome_version`.

Impact:
- Store listing quality and compatibility signaling are weaker than expected.

Recommendation:
- Add `icons` in manifest and action icons.
- Set `minimum_chrome_version` based on tested feature set.

### P3 - Release engineering enhancements

Recommendation:
- Add extension-only lint step and small regression checks for message contracts.
- Add a release checklist (permission audit, manual QA script, version bump policy, changelog).

## Suggested Update Plan

1. Security hardening pass
- Trim host permissions and define runtime-requested hosts where possible.
- Add cookie forwarding toggle + allowlist.

2. UX/compatibility pass
- Tune content script activation strategy.
- Improve popup wording around privacy-sensitive settings.

3. Packaging pass
- Add icons + minimum version metadata.
- Prepare a store submission note describing localhost-only bridge communication.

4. Verification pass
- Manual smoke tests:
  - Context-menu capture
  - Auto-capture for file download
  - Queue/retry when desktop app is down
  - Popup health check and active download refresh

## Optional Next Changes I Can Apply

If you want, I can implement these immediately in a focused patch:
- Add manifest icons and minimum browser version.
- Add cookie-forwarding opt-in setting end-to-end (manifest/service worker/popup UI).
- Reduce effective host exposure with optional host permissions strategy.
