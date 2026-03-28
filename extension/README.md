# IDM Browser Extension (MVP)

This extension captures source download URLs from pages and sends them to the local IDM desktop app.

## Features

- Context-menu item: `Download with IDM`
- Auto-capture of browser downloads (send to IDM automatically)
- HLS stream detection from live network requests (`.m3u8` / MPEGURL response types)
- Quality parsing from HLS master playlists (360p/720p/1080p and bitrate variants)
- Popup stream panel with per-stream quality picker
- `Export URL` action for external playback tools (VLC/IDM/manual)
- Popup with:
  - Scan current page source to IDM
  - Connection test
  - Host, port, and auth token settings
  - Auto-capture toggle
  - Auto-capture mode: all / strict / balanced / aggressive
- Talks to local bridge API at `http://127.0.0.1:6800` by default

## Install (unpacked)

1. Open your browser extensions page.
2. Enable Developer Mode.
3. Load unpacked extension folder: `extension/`.

## Configure with desktop app

- Ensure desktop app bridge server is enabled in `config.json`:
  - `server.enabled = true`
  - `server.host = "127.0.0.1"`
  - `server.port = 6800`
  - Optional: `server.auth_token = "your-token"`
- In popup settings, enter same host/port/token.
- Keep `Auto-capture browser downloads` enabled.
- Use mode `all` to force every browser download through IDM.
- Optional: enable `Forward browser cookies` only when needed for authenticated downloads. Add domains to the allowlist to permit forwarding; an empty allowlist blocks cookie forwarding.

## Notes about auto-capture

- Requires extension `downloads` permission (already included in manifest).
- Uses click interception for download-style buttons/links so IDM receives the URL before the browser save dialog.
- Adds an inline `IDM` mini-button next to detected download links; click it to force that specific link through IDM.
- For image/video/audio right-click, extension prioritizes real media source URL (`srcUrl`) instead of wrapper page links.
- Popup `Scan Page Source to IDM` crawls page links/media/meta tags and sends the best direct source candidate.
- Modes:
  - `all`: capture every browser download URL (recommended for IDM-first behavior)
  - `strict`: only obvious downloadable files (archives/installers)
  - `balanced`: strict + common media/docs (recommended)
  - `aggressive`: balanced + query/path download hints
- If auto-capture is enabled and IDM accepts the URL, browser download is cancelled.
- If IDM is unreachable or rejects the URL, browser download falls back automatically.

## HLS stream detection and selection

- The background service worker listens to `chrome.webRequest` for:
  - URL patterns that look like HLS playlists (`.m3u8`, `manifest`, `playlist`, `format=m3u8`)
  - Response `content-type` headers such as `application/vnd.apple.mpegurl`
- Requests are filtered to reduce noise:
  - Ignores analytics/ad/tracking endpoints
  - Ignores static assets like css/js/images/fonts
  - Uses request dedupe windows to avoid re-parsing duplicate captures
- For each detected playlist, the extension:
  - Fetches and parses the manifest
  - Extracts stream variants (`#EXT-X-STREAM-INF`) with resolution and bandwidth
  - Extracts segments and metadata from media playlists
  - Flags encrypted streams when `#EXT-X-KEY` exists
- Failed parse attempts are retried with exponential backoff.

### Blob-player compatibility

- Blob players usually build `blob:` URLs from playlists already fetched by JavaScript.
- The extension captures those network playlist URLs before blob conversion, then exposes them in popup.
- Works for many modern sites including lazy-loaded/AJAX-loaded players, as long as playlist fetch is visible to browser network stack.

### Popup workflow

1. Open target video page and start playback.
2. Open extension popup.
3. In `Detected Video Streams`:
   - Use `Refresh` to resync active-tab detections.
   - Pick desired quality from dropdown.
   - Use `Export URL` to copy selected stream URL.
   - Use `Download` to send selected stream to IDM bridge.

## Testing checklist

### Functional

- Verify `.m3u8` detections appear in popup on multiple sites.
- Verify quality options match expected variants.
- Verify exported URLs play in VLC or can be queued in IDM.

### Integration

- Confirm permissions in `manifest.json` include `webRequest`, `webRequestBlocking`, `activeTab`, `scripting`, and host access.
- Confirm popup actions call background message handlers without errors.
- Confirm service worker can fetch and parse detected manifests.

### Edge cases

- Blob-based players where only blob URL is visible in page source (network capture should still detect playlist URL).
- Multiple simultaneous streams in one tab.
- Encrypted playlists (`#EXT-X-KEY`) and sites with aggressive request patterns.

### Performance

- Confirm browsing remains responsive while playback is active.
- Confirm stream store is bounded/TTL-pruned and deduped.
- Confirm retries are limited and backoff-based.

## Endpoints used

- `GET /api/health`
- `POST /api/add`
