// IDM v2.0 — background.js — audited 2026-03-28

// Debug mode: set to true to enable detailed console logging
const DEBUG_MODE = false;

const DEFAULT_SETTINGS = {
  autoCaptureDownloads: false,
  capture: {
    auto: false,
    click_intercept: false,
    inline_buttons: true,
    right_click: true,
    streams: true,
    batch: true,
  },
  fileTypes: [
    "zip", "rar", "7z", "tar", "gz", "exe", "msi", "dmg", "pkg", "deb", "rpm",
    "pdf", "mp4", "mp3", "mkv", "avi", "mov", "flac", "wav", "iso", "apk", "webm", "m3u8", "mpd"
  ],
  minFileSizeKb: 0,
  showInlineIdmButtons: true,
  videoStreamDetection: true,
  showBadgeStreamCount: true,
  defaultSaveCategory: "Auto",
  bridgePort: 6800,
  downloadScoreThreshold: 60,
  showUncertainLinks: false,
  scorerDebugMode: false,
};

const CONTEXT_IDS = {
  link: "idm-download-link",
  media: "idm-download-media",
  image: "idm-download-image",
  batch: "idm-batch-page",
  browserBypass: "idm-browser-download-bypass",
};

const BRIDGE_POLL_INTERVAL_MS = 10000;
const BRIDGE_STATUS_TIMEOUT_MS = 1500;
const IDM_CONFIRMATION_TIMEOUT_MS = 3000;
const CAPTURE_PAUSE_DURATION_MS = 5 * 60 * 1000;
const BROWSER_PREFERRED_URL_TTL_MS = 30000;
const REPAIR_NOTIFICATION_ID = "idm-repair-needed";
const CONTEXT_MENU_SCHEMA_VERSION = 2;
const NEVER_INTERCEPT_TYPES = [
  "xmlhttprequest",
  "fetch",
  "websocket",
  "ping",
  "csp_report",
  "stylesheet",
  "script",
  "image",
  "font",
  "media",
];
const ALLOW_INTERCEPT_TYPES = ["main_frame", "sub_frame"];
const STRICT_BINARY_MIMES = [
  "application/octet-stream",
  "application/zip",
  "application/x-zip",
  "application/x-rar",
  "application/x-7z-compressed",
  "application/pdf",
  "application/x-msdownload",
  "application/x-executable",
  "video/",
  "audio/",
  "image/tiff",
  "image/x-raw",
];
const KNOWN_DIRECT_FILE_HOSTS = [
  "video-downloads.googleusercontent.com",
  "doc-downloads.googleusercontent.com",
  "drive-downloads.googleusercontent.com",
  "storage.googleapis.com",
  "s3.amazonaws.com",
  "blob.core.windows.net",
  "cdn.discordapp.com",
  "media.githubusercontent.com",
  "objects.githubusercontent.com",
  "releases.github.com",
];
const IDM_SENT_TTL_MS = 15000;

const pendingBrowserDownloads = new Set();
const capturedStreams = new Map();
const recentlyCapturedUrls = new Map();
const recentlySentToIDM = new Map();
const pendingCaptures = new Map();
const browserPreferredUrls = new Map();
let bridgeOffline = false;
let capturePausedUntil = 0;
let capturePauseLoaded = false;
let bridgeDegradedUntil = 0;

const LOCAL_CAPTURE_PAUSED_UNTIL_KEY = "capturePausedUntil";

function timeoutSignal(timeoutMs) {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return AbortSignal.timeout(timeoutMs);
  }
  const controller = new AbortController();
  setTimeout(() => controller.abort(), timeoutMs);
  return controller.signal;
}

function logBlockDecision(url, reason) {
  if (DEBUG_MODE) {
    console.debug(`[IDM Block] ${url} — ${reason}`);
  }
}

function isBridgeUrl(url) {
  const value = String(url || "").toLowerCase();
  return value.startsWith("http://localhost:") || value.startsWith("http://127.0.0.1:");
}

function isKnownDirectFileHost(url) {
  try {
    const parsed = new URL(String(url || ""));
    const hostname = String(parsed.hostname || "").toLowerCase();
    return KNOWN_DIRECT_FILE_HOSTS.some((host) => hostname === host || hostname.endsWith(`.${host}`));
  } catch (_) {
    return false;
  }
}

function markSentToIDM(url, finalUrl = "") {
  const expiry = Date.now() + IDM_SENT_TTL_MS;
  const original = normalizedCaptureUrl(url || "");
  const resolved = normalizedCaptureUrl(finalUrl || "");
  if (original) {
    recentlySentToIDM.set(original, expiry);
  }
  if (resolved) {
    recentlySentToIDM.set(resolved, expiry);
  }
}

function wasSentToIDM(url) {
  const key = normalizedCaptureUrl(url || "");
  if (!key) {
    return false;
  }
  const expiry = Number(recentlySentToIDM.get(key) || 0);
  if (!expiry) {
    return false;
  }
  if (Date.now() > expiry) {
    recentlySentToIDM.delete(key);
    return false;
  }
  return true;
}

function isIgnoredDownloadUrl(url) {
  const value = String(url || "").toLowerCase();
  if (!value) return true;
  if (value.startsWith("chrome://") || value.startsWith("chrome-extension://")) return true;
  if (value.startsWith("edge://") || value.startsWith("about:")) return true;
  if (value.startsWith("file://") || value.startsWith("blob:")) return true;
  return false;
}

function isBrowserManagedDownloadItem(item) {
  const filename = String(item?.filename || "").toLowerCase();
  const byName = filename.endsWith(".crx") || filename.endsWith(".appx") || filename.endsWith(".msix");
  const byMime = String(item?.mime || "").toLowerCase().includes("x-chrome-extension");
  const byUrl = String(item?.url || "").toLowerCase().includes("clients2.google.com/service/update2/crx");
  return byName || byMime || byUrl;
}

function shouldNeverBlock(downloadItem) {
  const url = String(downloadItem?.finalUrl || downloadItem?.url || "").toLowerCase();
  if (!url) return true;
  if (downloadItem?.byExtensionId) return true;
  if (url.endsWith(".crx")) return true;
  if (url.startsWith("chrome://") || url.startsWith("chrome-extension://")) return true;
  if (url.startsWith("edge://") || url.startsWith("about:")) return true;
  if (url.startsWith("data:")) return true;
  if (url.startsWith("blob:")) return true;
  return isBrowserManagedDownloadItem(downloadItem);
}

function setBrowserPreferredUrl(url, ttlMs = BROWSER_PREFERRED_URL_TTL_MS) {
  const key = normalizedCaptureUrl(url);
  if (!key) return;
  browserPreferredUrls.set(key, Date.now() + Math.max(1000, Number(ttlMs) || BROWSER_PREFERRED_URL_TTL_MS));
}

function isBrowserPreferredUrl(url) {
  const key = normalizedCaptureUrl(url);
  if (!key) return false;
  const expiresAt = Number(browserPreferredUrls.get(key) || 0);
  if (!expiresAt) return false;
  if (expiresAt < Date.now()) {
    browserPreferredUrls.delete(key);
    return false;
  }
  return true;
}

async function loadCapturePauseState() {
  if (capturePauseLoaded) {
    return;
  }
  capturePauseLoaded = true;
  try {
    const data = await chrome.storage.local.get(LOCAL_CAPTURE_PAUSED_UNTIL_KEY);
    const ts = Number(data?.[LOCAL_CAPTURE_PAUSED_UNTIL_KEY] || 0);
    capturePausedUntil = Number.isFinite(ts) ? ts : 0;
  } catch (_) {
    capturePausedUntil = 0;
  }
}

async function setCapturePausedUntil(ts) {
  capturePausedUntil = Number(ts || 0);
  capturePauseLoaded = true;
  await chrome.storage.local.set({ [LOCAL_CAPTURE_PAUSED_UNTIL_KEY]: capturePausedUntil });
}

function isCapturePausedNow() {
  return capturePausedUntil > Date.now();
}

function capturePauseRemainingMs() {
  return Math.max(0, capturePausedUntil - Date.now());
}

function showBadgeNotification(text, color = "#2ecc71", durationMs = 2000) {
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text: String(text || "") });
  setTimeout(() => {
    updateStreamBadge().catch(() => {});
  }, Math.max(500, Number(durationMs) || 2000));
}

function setBridgeState(state) {
  if (state === "degraded") {
    bridgeDegradedUntil = Date.now() + 60 * 1000;
    return;
  }
  bridgeDegradedUntil = 0;
}

function isBridgeDegraded() {
  if (!bridgeDegradedUntil) return false;
  if (bridgeDegradedUntil < Date.now()) {
    bridgeDegradedUntil = 0;
    return false;
  }
  return true;
}

const BridgeStatus = {
  state: "unknown",
  lastChecked: 0,
  cacheTTL: 10000,

  async isOnline(force = false) {
    const now = Date.now();
    if (!force && now - this.lastChecked < this.cacheTTL && this.state !== "unknown") {
      return this.state === "online";
    }

    const auth = await getLocalAuth();
    const host = auth.host || "localhost";
    const port = auth.port || DEFAULT_SETTINGS.bridgePort;
    const healthUrl = `http://${host}:${port}/api/health`;
    try {
      const res = await fetch(healthUrl, {
        method: "GET",
        signal: timeoutSignal(BRIDGE_STATUS_TIMEOUT_MS),
      });
      this.state = res.ok ? "online" : "offline";
    } catch (_) {
      this.state = "offline";
    }

    this.lastChecked = now;
    return this.state === "online";
  },
};

function normalizeFileTypes(list) {
  const values = Array.isArray(list) ? list : [];
  const cleaned = [];
  const seen = new Set();
  for (const raw of values) {
    const value = String(raw || "").trim().toLowerCase().replace(/^\./, "");
    if (!value || !/^[a-z0-9]+$/.test(value) || seen.has(value)) {
      continue;
    }
    seen.add(value);
    cleaned.push(value);
  }
  return cleaned.length ? cleaned : [...DEFAULT_SETTINGS.fileTypes];
}

function normalizeScoreThreshold(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return DEFAULT_SETTINGS.downloadScoreThreshold;
  }
  return Math.max(40, Math.min(80, Math.round(parsed)));
}

async function getSyncSettings() {
  const res = await chrome.storage.sync.get("settings");
  const current = res.settings || {};
  const capture = {
    ...DEFAULT_SETTINGS.capture,
    ...(current.capture || {}),
  };
  delete capture.hover_button;
  const autoCaptureDownloads = typeof current.autoCaptureDownloads === "boolean"
    ? current.autoCaptureDownloads
    : Boolean(capture.auto);
  return {
    ...DEFAULT_SETTINGS,
    ...current,
    capture,
    autoCaptureDownloads,
    fileTypes: normalizeFileTypes(current.fileTypes || DEFAULT_SETTINGS.fileTypes),
    downloadScoreThreshold: normalizeScoreThreshold(current.downloadScoreThreshold ?? DEFAULT_SETTINGS.downloadScoreThreshold),
    showUncertainLinks: Boolean(current.showUncertainLinks),
    scorerDebugMode: Boolean(current.scorerDebugMode),
  };
}

async function saveSyncSettings(patch) {
  const current = await getSyncSettings();
  const next = {
    ...current,
    ...(patch || {}),
  };
  next.capture = {
    ...DEFAULT_SETTINGS.capture,
    ...(next.capture || {}),
  };
  delete next.capture.hover_button;
  if (typeof next.autoCaptureDownloads === "boolean") {
    next.capture.auto = Boolean(next.autoCaptureDownloads);
  } else {
    next.autoCaptureDownloads = Boolean(next.capture.auto);
  }
  next.fileTypes = normalizeFileTypes(next.fileTypes);
  next.downloadScoreThreshold = normalizeScoreThreshold(next.downloadScoreThreshold);
  next.showUncertainLinks = Boolean(next.showUncertainLinks);
  next.scorerDebugMode = Boolean(next.scorerDebugMode);
  await chrome.storage.sync.set({ settings: next });
  return next;
}

async function getLocalAuth() {
  const [localAuth, legacySettings] = await Promise.all([
    chrome.storage.local.get("bridgeAuth"),
    chrome.storage.local.get("settings"),
  ]);
  const auth = localAuth.bridgeAuth || {};
  const legacy = legacySettings.settings || {};
  return {
    host: String(auth.host || legacy.host || "localhost").trim() || "localhost",
    port: Number(auth.port || legacy.port || DEFAULT_SETTINGS.bridgePort) || DEFAULT_SETTINGS.bridgePort,
    token: String(auth.token || legacy.token || "").trim(),
  };
}

async function setLocalAuth(auth) {
  const current = await getLocalAuth();
  const next = {
    host: String(auth?.host || current.host || "localhost").trim() || "localhost",
    port: Number(auth?.port || current.port || DEFAULT_SETTINGS.bridgePort) || DEFAULT_SETTINGS.bridgePort,
    token: String(auth?.token || "").trim(),
  };
  await chrome.storage.local.set({ bridgeAuth: next });
  return next;
}

function clearStreamBadge() {
  chrome.action.setBadgeText({ text: "" });
}

function setOfflineBadge() {
  bridgeOffline = true;
  chrome.action.setBadgeBackgroundColor({ color: "#b42318" });
  chrome.action.setBadgeText({ text: "OFF" });
}

async function clearOfflineBadge() {
  bridgeOffline = false;
  await updateStreamBadge();
}

async function updateStreamBadge() {
  if (bridgeOffline) {
    setOfflineBadge();
    return;
  }
  const settings = await getSyncSettings();
  if (!settings.showBadgeStreamCount) {
    clearStreamBadge();
    return;
  }
  const count = capturedStreams.size;
  chrome.action.setBadgeBackgroundColor({ color: "#1f6feb" });
  chrome.action.setBadgeText({ text: count > 0 ? String(Math.min(99, count)) : "" });
}

function getUrlExt(url) {
  try {
    const parsed = new URL(url);
    const last = (parsed.pathname.split("/").pop() || "").toLowerCase();
    const idx = last.lastIndexOf(".");
    if (idx === -1) {
      return "";
    }
    return last.slice(idx + 1);
  } catch (_) {
    return "";
  }
}

function extractHeader(headers, name) {
  const needle = String(name || "").toLowerCase();
  const list = Array.isArray(headers) ? headers : [];
  const found = list.find((h) => String(h?.name || "").toLowerCase() === needle);
  return String(found?.value || "");
}

async function notify(message) {
  try {
    await chrome.notifications.create(`idm-${Date.now()}`, {
      type: "basic",
      iconUrl: "icons/icon128.png",
      title: "IDM Bridge",
      message,
      priority: 1,
    });
  } catch (_) {
    // ignore
  }
}

async function notifyRepairNeeded() {
  try {
    await chrome.notifications.create(REPAIR_NOTIFICATION_ID, {
      type: "basic",
      iconUrl: "icons/icon128.png",
      title: "IDM Bridge",
      message: "IDM: Re-pairing needed. Click to pair.",
      priority: 1,
    });
  } catch (_) {
    // ignore
  }
}

async function checkBridgeReachability() {
  try {
    const online = await BridgeStatus.isOnline(true);
    if (!online) {
      setOfflineBadge();
      return false;
    }
    await clearOfflineBadge();
    return true;
  } catch (_) {
    setOfflineBadge();
    return false;
  }
}

async function validateStoredTokenOnStartup() {
  const auth = await getLocalAuth();
  if (!auth.token) {
    return;
  }

  const statusUrl = `http://${auth.host || "localhost"}:${auth.port || DEFAULT_SETTINGS.bridgePort}/api/auth/status`;
  try {
    const response = await fetch(statusUrl, {
      method: "GET",
      headers: {
        "Accept": "application/json",
        "Authorization": `Bearer ${auth.token}`,
      },
    });

    if (response.status === 401) {
      await setLocalAuth({ ...auth, token: "" });
      return;
    }

    if (response.ok) {
      return;
    }
  } catch (_) {
    // Bridge offline is handled by silent polling; avoid user notification spam.
    setOfflineBadge();
  }
}

async function checkAuthStatusSilently() {
  const auth = await getLocalAuth();
  if (!auth.token) {
    return;
  }

  try {
    const response = await fetch(`http://${auth.host}:${auth.port}/api/auth/status`, {
      method: "GET",
      headers: {
        "Accept": "application/json",
        "Authorization": `Bearer ${auth.token}`,
      },
    });

    if (response.status === 401) {
      await setLocalAuth({ ...auth, token: "" });
    }
  } catch (_) {
    // Keep this silent; bridge availability is handled by offline badge.
  }
}

function ensureBridgePolling() {
  // Use chrome.alarms instead of setInterval for service worker compatibility
  // Create alarm that fires every 10 seconds (minimum 0.5 minutes = 30 seconds, so we use periodInMinutes)
  // For frequent polling, we check on startup and let message handler keep connection warm
  chrome.alarms.create("bridgeHealthCheck", { periodInMinutes: 10/60 }); // ~10 seconds
}

// Handle bridge health check alarm
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bridgeHealthCheck") {
    checkBridgeReachability().catch(() => {});
    checkAuthStatusSilently().catch(() => {});
  }
});

async function initializeBridgeAuthState() {
  await checkBridgeReachability();
  await validateStoredTokenOnStartup();
  ensureBridgePolling();
}

async function callBridge(path, options = {}, extras = {}) {
  const auth = await getLocalAuth();
  const host = auth.host || "localhost";
  const port = auth.port || DEFAULT_SETTINGS.bridgePort;
  const base = `http://${host}:${port}`;
  const url = `${base}${path}`;

  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (!extras.allowUnauthed) {
    if (!auth.token) {
      throw new Error("Extension not paired");
    }
    headers.set("Authorization", `Bearer ${auth.token}`);
  }

  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response;
  try {
    response = await fetch(url, {
      ...options,
      headers,
    });
  } catch (_) {
    throw new Error("IDM not running");
  }

  if (response.status === 401 && !extras.allowUnauthed) {
    await setLocalAuth({ ...auth, token: "" });
    if (chrome.runtime.openOptionsPage) {
      chrome.runtime.openOptionsPage();
    }
    throw new Error("Session expired. Pair again in options.");
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }

  if (!response.ok || (payload && payload.success === false)) {
    throw new Error(payload?.error || `HTTP ${response.status}`);
  }

  return payload;
}

async function addDownload(url, data = {}) {
  const settings = await getSyncSettings();
  const body = {
    url,
    filename: data.filename || "",
    filename_hint: data.filename_hint || "",
    referer: data.referer || "",
    cookies: data.cookies || "",
    category: data.category || settings.defaultSaveCategory,
    save_path: data.save_path || "",
    headers: data.headers || undefined,
  };
  return callBridge("/api/download", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

async function resolveUrl(url, data = {}) {
  const body = {
    url,
    referer: data.referer || "",
    cookies: data.cookies || "",
    headers: data.headers || undefined,
  };
  return callBridge("/api/resolve", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

function hasFileExtension(url) {
  return Boolean(getUrlExt(url));
}

async function resolveURLHeaders(url) {
  const targetUrl = String(url || "").trim();
  if (!targetUrl) {
    return { error: true, corsBlocked: true };
  }

  if (isKnownDirectFileHost(targetUrl)) {
    return {
      finalUrl: targetUrl,
      contentType: "application/octet-stream",
      contentDisposition: "",
      contentLength: null,
      isHtmlPage: false,
      isBinaryFile: true,
      isBinary: true,
      verified: true,
      error: false,
      corsBlocked: false,
      knownDirectHost: true,
      resumeSupported: true,
    };
  }

  const settings = await getSyncSettings();

  try {
    const res = await fetch(targetUrl, {
      method: "HEAD",
      redirect: "follow",
      signal: timeoutSignal(4000),
      cache: "no-store",
      credentials: "omit",
    });

    const contentType = String(res.headers.get("content-type") || "");
    const contentDisposition = String(res.headers.get("content-disposition") || "");
    const contentLength = res.headers.get("content-length") || null;
    const finalUrl = String(res.url || targetUrl);

    const isHtmlPage = contentType.toLowerCase().includes("text/html");
    const hasAttachment = contentDisposition.toLowerCase().includes("attachment");
    const isBinaryFile = !isHtmlPage && (
      hasAttachment ||
      isKnownBinaryMime(contentType) ||
      hasDownloadableExtension(finalUrl, settings.fileTypes) ||
      hasFileExtension(finalUrl) ||
      isKnownDirectFileHost(finalUrl)
    );

    return {
      finalUrl,
      contentType,
      contentDisposition,
      contentLength,
      isHtmlPage,
      isBinaryFile,
      isBinary: isBinaryFile,
      verified: true,
      error: false,
      corsBlocked: false,
      resumeSupported: String(res.headers.get("accept-ranges") || "").toLowerCase() === "bytes",
    };
  } catch (_) {
    return {
      finalUrl: targetUrl,
      contentType: "",
      contentDisposition: "",
      contentLength: null,
      isHtmlPage: false,
      isBinaryFile: false,
      isBinary: false,
      verified: false,
      error: true,
      corsBlocked: true,
      resumeSupported: false,
    };
  }
}

async function addStreamDownload(stream) {
  const body = {
    url: stream.url,
    type: stream.type || "direct",
    referer: stream.referer || "",
    cookies: stream.cookies || "",
    title: stream.title || "",
  };
  return callBridge("/api/download/stream", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

async function addBatchDownloads(urls, savePath = "", category = "Auto") {
  return callBridge("/api/download/batch", {
    method: "POST",
    body: JSON.stringify({
      urls,
      save_path: savePath,
      category,
    }),
  });
}

function dedupeKey(url, suffix = "") {
  return `${String(url || "").slice(0, 1024)}|${suffix}`;
}

function normalizePairingCode(value) {
  return String(value || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
}

function normalizedCaptureUrl(url) {
  try {
    const parsed = new URL(String(url || ""));
    parsed.hash = "";
    return parsed.toString();
  } catch (_) {
    return String(url || "").split("#")[0].trim();
  }
}

function wasRecentlyCaptured(url) {
  const key = normalizedCaptureUrl(url);
  if (!key) {
    return false;
  }
  const ts = recentlyCapturedUrls.get(key);
  return typeof ts === "number" && Date.now() - ts <= 10000;
}

function markCaptured(url) {
  const key = normalizedCaptureUrl(url);
  if (!key) {
    return;
  }
  recentlyCapturedUrls.set(key, Date.now());
  setTimeout(() => {
    recentlyCapturedUrls.delete(key);
  }, 10000);
}

function isKnownBinaryMime(contentTypeValue) {
  const contentType = String(contentTypeValue || "").toLowerCase().split(";")[0].trim();
  if (!contentType) {
    return false;
  }
  return STRICT_BINARY_MIMES.some((mime) => contentType.startsWith(mime));
}

function hasDownloadableExtension(url, fileTypes) {
  const ext = getUrlExt(url);
  return Boolean(ext && normalizeFileTypes(fileTypes).includes(ext));
}

function isRealDownload(details, responseHeaders) {
  if (!details || !Array.isArray(responseHeaders)) {
    return false;
  }
  const requestType = String(details.type || "").toLowerCase();
  if (NEVER_INTERCEPT_TYPES.includes(requestType)) {
    return false;
  }
  if (!ALLOW_INTERCEPT_TYPES.includes(requestType)) {
    return false;
  }

  const contentDisposition = extractHeader(responseHeaders, "content-disposition");
  const hasAttachment = contentDisposition.toLowerCase().includes("attachment");
  const contentType = extractHeader(responseHeaders, "content-type").toLowerCase();

  const isHtml = contentType.includes("text/html");
  const isJson = contentType.includes("application/json");
  const isJavascript = contentType.includes("javascript");
  const isCss = contentType.includes("text/css");
  const isXml = contentType.includes("text/xml") || contentType.includes("application/xml");
  if (isHtml || isJson || isJavascript || isCss || isXml) {
    return false;
  }

  if (requestType === "main_frame" && !hasAttachment) {
    return false;
  }

  const initiator = String(details.initiator || "").toLowerCase();
  if (!initiator) {
    return false;
  }
  if (initiator.startsWith("chrome-extension://") || initiator.startsWith("chrome://")) {
    return false;
  }

  const isBinaryMime = isKnownBinaryMime(contentType);
  return hasAttachment || isBinaryMime;
}

async function captureCandidate(url, meta = {}) {
  if (!url) {
    return;
  }
  if (isIgnoredDownloadUrl(url) || isBridgeUrl(url)) {
    return;
  }
  if (wasRecentlyCaptured(url)) {
    return;
  }

  try {
    await addDownload(url, {
      referer: meta.referer || meta.initiator || "",
      category: meta.category || "Auto",
    });
    markCaptured(url);
  } catch (_) {
    // ignore capture failures here
  }
}

async function captureURL(url, context = {}) {
  const normalized = normalizedCaptureUrl(url);
  if (!normalized) {
    return;
  }
  if (pendingCaptures.has(normalized)) {
    return;
  }
  const promise = captureCandidate(normalized, context);
  pendingCaptures.set(normalized, promise);
  try {
    await promise;
  } finally {
    setTimeout(() => {
      pendingCaptures.delete(normalized);
    }, 5000);
  }
}

function extractDownloadId(payload) {
  const direct = String(payload?.download_id || "").trim();
  if (direct) return direct;
  const nested = String(payload?.data?.download_id || "").trim();
  if (nested) return nested;
  return "";
}

async function sendToIDMWithConfirmation(url, item = {}) {
  try {
    const data = await Promise.race([
      addDownload(url, {
        filename: item.filename ? item.filename.split(/[\\/]/).pop() : "",
        referer: "",
      }),
      new Promise((_, reject) => setTimeout(() => reject(new Error("Timed out")), IDM_CONFIRMATION_TIMEOUT_MS)),
    ]);
    const accepted = Boolean(extractDownloadId(data));
    if (accepted) {
      markSentToIDM(url, item.finalUrl || item.url || url);
    }
    return accepted;
  } catch (_) {
    return false;
  }
}

async function getCaptureBlockingStatus() {
  await loadCapturePauseState();
  const settings = await getSyncSettings();
  const autoCaptureEnabled = Boolean(settings.capture?.auto || settings.autoCaptureDownloads);
  if (!autoCaptureEnabled) {
    return {
      mode: "disabled",
      title: "Capture Disabled",
      detail: "Auto-capture is OFF. Browser downloads are not blocked.",
      color: "#9ca3af",
      pausedUntil: 0,
      remainingMs: 0,
    };
  }

  if (isCapturePausedNow()) {
    const remainingMs = capturePauseRemainingMs();
    return {
      mode: "paused",
      title: "IDM Paused",
      detail: "Downloads go to browser while capture is paused.",
      color: "#f59e0b",
      pausedUntil: capturePausedUntil,
      remainingMs,
    };
  }

  const online = await BridgeStatus.isOnline();
  if (!online) {
    return {
      mode: "offline",
      title: "IDM Offline",
      detail: "Bridge not running. Browser downloads continue normally.",
      color: "#ef4444",
      pausedUntil: 0,
      remainingMs: 0,
    };
  }

  return {
    mode: "active",
    title: "IDM Active",
    detail: isBridgeDegraded()
      ? "Eligible downloads are redirected, but recent IDM handoff failures triggered browser fallback."
      : "Eligible browser downloads are redirected to IDM.",
    color: "#2ecc71",
    pausedUntil: 0,
    remainingMs: 0,
  };
}

async function handleHeadersReceived(details) {
  if (!details || isIgnoredDownloadUrl(details.url) || isBridgeUrl(details.url)) {
    return;
  }
  const resourceType = String(details.type || "").toLowerCase();
  if (NEVER_INTERCEPT_TYPES.includes(resourceType)) {
    return;
  }
  if (!ALLOW_INTERCEPT_TYPES.includes(resourceType)) {
    return;
  }

  const settings = await getSyncSettings();
  if (!settings.capture?.auto && !settings.autoCaptureDownloads) {
    return;
  }

  if (!isRealDownload(details, details.responseHeaders || [])) {
    return;
  }

  const url = details.url;
  if (wasRecentlyCaptured(url)) {
    return;
  }

  await captureURL(url, {
    source: "onHeadersReceived",
    referer: details.initiator || "",
  });
}

// Captures browser download items only as a fallback; ignores extension/browser-managed/internal/page URLs.
chrome.downloads.onCreated.addListener(async (item) => {
  if (!item?.url) {
    return;
  }

  const targetUrl = item.finalUrl || item.url;
  if (!targetUrl) {
    return;
  }

  if (wasSentToIDM(targetUrl) || wasSentToIDM(item.url)) {
    try {
      await chrome.downloads.cancel(item.id);
    } catch (_) {
      // ignore cancellation race
    }
    logBlockDecision(targetUrl, "block: duplicate browser download for IDM-captured URL");
    return;
  }

  if (isKnownDirectFileHost(targetUrl)) {
    const online = await BridgeStatus.isOnline();
    if (online) {
      const startedAt = Date.now();
      try {
        await chrome.downloads.pause(item.id);
      } catch (_) {
        // ignore pause race and fall through
      }
      const accepted = await sendToIDMWithConfirmation(targetUrl, item);
      if (accepted) {
        try {
          await chrome.downloads.cancel(item.id);
        } catch (_) {
          // ignore cancel race
        }
        markSentToIDM(targetUrl, item.finalUrl || item.url || targetUrl);
        logBlockDecision(targetUrl, `block: known direct host duplicate prevented in ${Date.now() - startedAt}ms`);
        return;
      }
      try {
        await chrome.downloads.resume(item.id);
      } catch (_) {
        // ignore resume race
      }
      logBlockDecision(targetUrl, "allow: known direct host browser fallback after IDM rejection");
      return;
    }
    logBlockDecision(targetUrl, "allow: known direct host while bridge offline");
    return;
  }

  await loadCapturePauseState();

  if (shouldNeverBlock(item)) {
    logBlockDecision(targetUrl, "allow: browser-managed or internal URL");
    return;
  }

  if (isIgnoredDownloadUrl(targetUrl) || isBridgeUrl(targetUrl)) {
    logBlockDecision(targetUrl, "allow: ignored or bridge URL");
    return;
  }

  if (isBrowserPreferredUrl(targetUrl)) {
    logBlockDecision(targetUrl, "allow: user bypass (shift-click/context menu)");
    return;
  }

  const settings = await getSyncSettings();
  if (!settings.capture?.auto && !settings.autoCaptureDownloads) {
    logBlockDecision(targetUrl, "allow: auto-capture disabled");
    return;
  }

  if (isCapturePausedNow()) {
    logBlockDecision(targetUrl, "allow: capture paused by user");
    return;
  }

  if (!hasDownloadableExtension(targetUrl, settings.fileTypes)) {
    logBlockDecision(targetUrl, "allow: file type not handled by IDM");
    return;
  }

  if (pendingBrowserDownloads.has(item.id)) {
    return;
  }
  pendingBrowserDownloads.add(item.id);

  try {
    const bridgeOnline = await BridgeStatus.isOnline();
    if (!bridgeOnline) {
      logBlockDecision(targetUrl, "allow: bridge offline");
      return;
    }

    try {
      await chrome.downloads.pause(item.id);
    } catch (_) {
      logBlockDecision(targetUrl, "allow: could not pause browser download");
      return;
    }

    const idmAccepted = await sendToIDMWithConfirmation(targetUrl, item);
    if (idmAccepted) {
      try {
        await chrome.downloads.cancel(item.id);
      } catch (_) {
        // Ignore cancel race if browser completed first.
      }
      markCaptured(targetUrl);
      setBridgeState("normal");
      showBadgeNotification("\u2713", "#2ecc71", 2000);
      logBlockDecision(targetUrl, "block: IDM confirmed receipt");
      return;
    }

    setBridgeState("degraded");
    try {
      await chrome.downloads.resume(item.id);
    } catch (_) {
      // Resume may fail if download already completed while waiting.
    }
    logBlockDecision(targetUrl, "allow: IDM did not confirm within 3s");
  } catch (_) {
    setBridgeState("degraded");
    try {
      await chrome.downloads.resume(item.id);
    } catch (_) {
      // ignore resume failure
    }
    logBlockDecision(targetUrl, "allow: IDM handoff failed, browser fallback");
  } finally {
    pendingBrowserDownloads.delete(item.id);
  }
});

function rememberStream(stream) {
  const url = String(stream?.url || "").trim();
  if (!url) {
    return;
  }
  const key = dedupeKey(url, stream.type || "direct");
  capturedStreams.set(key, {
    url,
    type: stream.type || "direct",
    referer: stream.referer || "",
    cookies: stream.cookies || "",
    title: stream.title || "",
    tabId: Number(stream.tabId || -1),
    at: Date.now(),
  });
}

function streamsForTab(tabId) {
  const out = [];
  for (const entry of capturedStreams.values()) {
    if (tabId >= 0 && entry.tabId !== tabId) {
      continue;
    }
    out.push(entry);
  }
  out.sort((a, b) => b.at - a.at);
  return out;
}

async function createContextMenus() {
  return new Promise((resolve) => {
    chrome.contextMenus.removeAll(() => {
      chrome.contextMenus.create({ id: CONTEXT_IDS.link, title: "Download with IDM", contexts: ["link"] });
      chrome.contextMenus.create({ id: CONTEXT_IDS.media, title: "Download video with IDM", contexts: ["video", "audio"] });
      chrome.contextMenus.create({ id: CONTEXT_IDS.image, title: "Download image with IDM", contexts: ["image"] });
      chrome.contextMenus.create({ id: CONTEXT_IDS.batch, title: "Batch download links on this page", contexts: ["page"] });
      chrome.contextMenus.create({ id: CONTEXT_IDS.browserBypass, title: "Download with Browser (bypass IDM)", contexts: ["link"] });
      resolve();
    });
  });
}

async function ensureContextMenus() {
  const stored = await chrome.storage.local.get("contextMenuSchemaVersion");
  if (Number(stored.contextMenuSchemaVersion || 0) === CONTEXT_MENU_SCHEMA_VERSION) {
    return;
  }
  await createContextMenus();
  await chrome.storage.local.set({ contextMenuSchemaVersion: CONTEXT_MENU_SCHEMA_VERSION });
}

// Captures user-initiated right-click commands only; ignores automatic network traffic.
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === CONTEXT_IDS.link && info.linkUrl) {
      await addDownload(info.linkUrl, { referer: info.pageUrl || "" });
      return;
    }
    if (info.menuItemId === CONTEXT_IDS.media && info.srcUrl) {
      await addStreamDownload({ url: info.srcUrl, type: "direct", referer: info.pageUrl || "" });
      return;
    }
    if (info.menuItemId === CONTEXT_IDS.image && info.srcUrl) {
      await addDownload(info.srcUrl, { referer: info.pageUrl || "", category: "Image" });
      return;
    }
    if (info.menuItemId === CONTEXT_IDS.batch && typeof tab?.id === "number") {
      const response = await chrome.tabs.sendMessage(tab.id, { type: "scanPageForBatch" });
      const items = Array.isArray(response?.items) ? response.items : [];
      await chrome.storage.local.set({ pendingBatchItems: items, pendingBatchAt: Date.now() });
      await chrome.tabs.create({ url: chrome.runtime.getURL("popup/popup.html?batch=1") });
      return;
    }
    if (info.menuItemId === CONTEXT_IDS.browserBypass && info.linkUrl) {
      setBrowserPreferredUrl(info.linkUrl);
      await chrome.downloads.download({ url: info.linkUrl });
      logBlockDecision(info.linkUrl, "allow: user selected browser download from context menu");
    }
  } catch (_) {
    // ignore context menu errors
  }
});

// Captures extension install/update lifecycle only; ignores page and download events.
chrome.runtime.onInstalled.addListener(async () => {
  await saveSyncSettings({});
  await createContextMenus();
  await chrome.storage.local.set({ contextMenuSchemaVersion: CONTEXT_MENU_SCHEMA_VERSION });
  clearStreamBadge();
  await loadCapturePauseState();
  await initializeBridgeAuthState();
});

// Captures browser startup lifecycle only; ignores navigation/download requests.
chrome.runtime.onStartup.addListener(async () => {
  await ensureContextMenus();
  await loadCapturePauseState();
  await initializeBridgeAuthState();
});

// Captures user clicks on IDM notifications only; ignores background web requests.
chrome.notifications.onClicked.addListener((notificationId) => {
  if (notificationId === REPAIR_NOTIFICATION_ID && chrome.runtime.openOptionsPage) {
    chrome.runtime.openOptionsPage();
  }
});

// Captures explicit extension messages only; ignores passive web traffic interception.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    const type = String(message?.type || "");

    const tryPairWithCode = async (host, port, pairingCode) => {
      const response = await fetch(`http://${host}:${port}/api/pair`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
        },
        body: JSON.stringify({ pairing_code: pairingCode }),
      });
      let body = null;
      try {
        body = await response.json();
      } catch (_) {
        body = null;
      }
      return { response, body };
    };

    if (type === "pairWithCode") {
      const host = String(message.host || "localhost").trim() || "localhost";
      const port = Number(message.port || DEFAULT_SETTINGS.bridgePort) || DEFAULT_SETTINGS.bridgePort;
      const code = normalizePairingCode(message.pairing_code || "");
      if (!code) {
        sendResponse({ ok: false, error: "Pairing code required" });
        return;
      }

      try {
        let { response: resp, body: payload } = await tryPairWithCode(host, port, code);

        if (!resp.ok || !payload?.success) {
          sendResponse({ ok: false, error: payload?.error || "Pairing failed" });
          return;
        }

        await setLocalAuth({ host, port, token: payload.data.session_token });
        sendResponse({ ok: true, token: payload.data.session_token, expiresAt: payload.data.expires_at });
      } catch (_) {
        sendResponse({ ok: false, error: "IDM not running" });
      }
      return;
    }

    if (type === "IDM_CLICK_CAPTURED") {
      const url = normalizedCaptureUrl(message.url || "");
      if (!url) {
        sendResponse({ ok: false, error: "URL required" });
        return;
      }
      markSentToIDM(url, message.finalUrl || "");
      sendResponse({ ok: true });
      return;
    }

    if (type === "getSettings") {
      const [settings, auth] = await Promise.all([getSyncSettings(), getLocalAuth()]);
      sendResponse({ ok: true, settings: { ...settings, host: auth.host, port: auth.port } });
      return;
    }

    if (type === "saveSettings") {
      const incoming = message.settings || {};
      const saved = await saveSyncSettings(incoming);
      if (incoming.host || incoming.port) {
        const auth = await getLocalAuth();
        await setLocalAuth({ ...auth, host: incoming.host || auth.host, port: incoming.port || auth.port, token: auth.token });
      }
      try {
        const tabs = await chrome.tabs.query({});
        await Promise.allSettled(
          tabs
            .filter((tab) => typeof tab.id === "number")
            .map((tab) => chrome.tabs.sendMessage(tab.id, { type: "refreshFeatureFlags" }))
        );
      } catch (_) {
        // Ignore refresh fan-out errors for restricted pages.
      }
      sendResponse({ ok: true, settings: saved });
      return;
    }

    if (type === "pingBridge") {
      try {
        const online = await BridgeStatus.isOnline(true);
        sendResponse({ ok: online, data: { online } });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || "IDM not running" });
      }
      return;
    }

    if (type === "markBrowserPreferredUrl") {
      const url = String(message.url || "").trim();
      const ttlMs = Number(message.ttlMs || BROWSER_PREFERRED_URL_TTL_MS);
      if (!url) {
        sendResponse({ ok: false, error: "URL required" });
        return;
      }
      setBrowserPreferredUrl(url, ttlMs);
      sendResponse({ ok: true });
      return;
    }

    if (type === "getCaptureBlockingStatus") {
      const status = await getCaptureBlockingStatus();
      sendResponse({ ok: true, status });
      return;
    }

    if (type === "toggleCapturePause") {
      await loadCapturePauseState();
      if (isCapturePausedNow()) {
        await setCapturePausedUntil(0);
      } else {
        await setCapturePausedUntil(Date.now() + CAPTURE_PAUSE_DURATION_MS);
      }
      const status = await getCaptureBlockingStatus();
      sendResponse({ ok: true, status });
      return;
    }

    if (type === "queueGet") {
      try {
        const data = await callBridge("/api/queue", { method: "GET" });
        sendResponse({ ok: true, data: data.data || data });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || "IDM not running" });
      }
      return;
    }

    if (type === "queueAction") {
      try {
        const endpoint = `/api/queue/${encodeURIComponent(String(message.id || ""))}/${encodeURIComponent(String(message.action || ""))}`;
        const data = await callBridge(endpoint, { method: "POST" });
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || "IDM not running" });
      }
      return;
    }

    if (type === "queuePauseAll" || type === "queueResumeAll") {
      try {
        const queue = await callBridge("/api/queue", { method: "GET" });
        const items = Array.isArray(queue?.data?.downloads) ? queue.data.downloads : [];
        const action = type === "queuePauseAll" ? "pause" : "resume";
        const statuses = type === "queuePauseAll" ? ["downloading"] : ["paused", "failed", "queued"];
        const targets = items.filter((d) => statuses.includes(String(d.status || "")));
        await Promise.allSettled(targets.map((d) => callBridge(`/api/queue/${encodeURIComponent(d.id)}/${action}`, { method: "POST" })));
        sendResponse({ ok: true, count: targets.length });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || "IDM not running" });
      }
      return;
    }

    if (type === "setSpeedLimit") {
      try {
        const data = await callBridge("/api/settings/speed_limit", {
          method: "POST",
          body: JSON.stringify({ limit_kbps: Number(message.limit_kbps || 0) }),
        });
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || "IDM not running" });
      }
      return;
    }

    if (type === "addDownload") {
      try {
        const data = await addDownload(String(message.url || ""), {
          referer: message.referer || "",
          filename: message.filename || "",
          category: message.category || "Auto",
          save_path: message.save_path || "",
        });
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || "IDM not running" });
      }
      return;
    }

    if (type === "resolveUrl" || type === "RESOLVE_URL") {
      try {
        const data = await resolveURLHeaders(String(message.url || ""));
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || "Resolve failed" });
      }
      return;
    }

    if (type === "batchDownload") {
      try {
        const data = await addBatchDownloads(message.urls || [], message.save_path || "", message.category || "Auto");
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || "IDM not running" });
      }
      return;
    }

    if (type === "scanActiveTab") {
      try {
        const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
        const tab = tabs[0];
        if (!tab || typeof tab.id !== "number") {
          sendResponse({ ok: false, error: "No active tab" });
          return;
        }
        const res = await chrome.tabs.sendMessage(tab.id, { type: "scanPageForBatch" });
        sendResponse({ ok: true, items: Array.isArray(res?.items) ? res.items : [] });
      } catch (_) {
        sendResponse({ ok: false, error: "Cannot scan this page" });
      }
      return;
    }

    if (type === "getPendingBatch") {
      const data = await chrome.storage.local.get(["pendingBatchItems", "pendingBatchAt"]);
      const age = Date.now() - Number(data.pendingBatchAt || 0);
      if (!Array.isArray(data.pendingBatchItems) || age > 5 * 60 * 1000) {
        sendResponse({ ok: true, items: [] });
        return;
      }
      sendResponse({ ok: true, items: data.pendingBatchItems });
      return;
    }

    if (type === "clearPendingBatch") {
      await chrome.storage.local.remove(["pendingBatchItems", "pendingBatchAt"]);
      sendResponse({ ok: true });
      return;
    }

    if (type === "streamDetected") {
      const settings = await getSyncSettings();
      if (!settings.videoStreamDetection) {
        sendResponse({ ok: true });
        return;
      }
      rememberStream({ ...message.stream, tabId: sender?.tab?.id ?? -1 });
      await updateStreamBadge();
      sendResponse({ ok: true });
      return;
    }

    if (type === "getStreams") {
      const tabId = Number(message.tabId || -1);
      sendResponse({ ok: true, streams: streamsForTab(tabId) });
      return;
    }

    if (type === "clearStreams") {
      capturedStreams.clear();
      await updateStreamBadge();
      sendResponse({ ok: true });
      return;
    }

    if (type === "downloadStream") {
      try {
        const data = await addStreamDownload(message.stream || {});
        sendResponse({ ok: true, data });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || "IDM not running" });
      }
      return;
    }

    if (type === "getFeatureFlags") {
      const settings = await getSyncSettings();
      sendResponse({
        ok: true,
        flags: {
          showInlineIdmButtons: Boolean(settings.showInlineIdmButtons !== false),
          videoStreamDetection: Boolean(settings.videoStreamDetection),
          fileTypes: normalizeFileTypes(settings.fileTypes),
          minFileSizeKb: Number(settings.minFileSizeKb || 0),
          downloadScoreThreshold: normalizeScoreThreshold(settings.downloadScoreThreshold),
          showUncertainLinks: Boolean(settings.showUncertainLinks),
          scorerDebugMode: Boolean(settings.scorerDebugMode),
        },
      });
      return;
    }

    sendResponse({ ok: false, error: "Unknown message" });
  })();

  return true;
});

if (chrome.webRequest?.onHeadersReceived) {
  // Captures only real file-download navigations (main/sub-frame) and ignores xhr/fetch/scripts/media/css/html/json.
  chrome.webRequest.onHeadersReceived.addListener(
    (details) => {
      handleHeadersReceived(details).catch(() => {});
      return {};
    },
    {
      urls: ["<all_urls>"],
      types: ["main_frame", "sub_frame"],
    },
    ["responseHeaders"]
  );
}

initializeBridgeAuthState().catch(() => {});
ensureContextMenus().catch(() => {});
