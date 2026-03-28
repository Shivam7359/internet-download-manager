const DEFAULT_SETTINGS = {
	host: "127.0.0.1",
	port: 6800,
	token: "",
	autoCaptureDownloads: false,
	autoCaptureMode: "all",
	autoCaptureMinBytes: 5 * 1024 * 1024,
	forwardCookies: false,
	cookieForwardAllowlist: "",
	disabledSites: [],
	showNotifications: true,
};

const MENU_ID = "idm-download-link";
const DEBUG_LOG_LIMIT = 40;
const debugEvents = [];
const contextTargetByTab = new Map();
const CONTEXT_TARGET_TTL_MS = 15000;
const PENDING_QUEUE_KEY = "pendingDownloadQueue";
const MAX_PENDING_QUEUE_ITEMS = 200;
const RETRY_ALARM_NAME = "idm-pending-retry";
const RETRY_ALARM_DELAY_MINUTES = 0.25;
const HEALTH_CHECK_ALARM = "idm-health-check";
const HEALTH_CHECK_INTERVAL_MINUTES = 1;
const STATS_STORAGE_KEY = "downloadStats";
const WS_RECONNECT_BASE_MS = 2000;
const WS_RECONNECT_MAX_MS = 60000;
const WS_PING_INTERVAL_MS = 25000;
const NOTIFICATION_ICON_PATH = chrome.runtime.getURL("icons/icon128.png");
const BATCH_MAX_CONCURRENT = 6;
const HLS_STREAM_MAX_ITEMS = 120;
const HLS_STREAM_TTL_MS = 45 * 60 * 1000;
const HLS_REQUEST_DEDUPE_MS = 60000;
const HLS_PARSE_RETRY_LIMIT = 3;
const HLS_PARSE_RETRY_ALARM = "idm-hls-parse-retry";

const hlsStore = {
	streams: new Map(),
	streamOrder: [],
	urlIndex: new Map(),
	identityIndex: new Map(),
	requestSeen: new Map(),
	parseInFlight: new Set(),
};

async function ensureOptionalPermissions(permissions = [], origins = []) {
	if (!chrome.permissions || typeof chrome.permissions.contains !== "function") {
		return false;
	}

	const wanted = {
		permissions: Array.isArray(permissions) ? permissions : [],
		origins: Array.isArray(origins) ? origins : [],
	};

	try {
		const has = await chrome.permissions.contains(wanted);
		if (has) {
			return true;
		}

		if (typeof chrome.permissions.request !== "function") {
			return false;
		}
		return await chrome.permissions.request(wanted);
	} catch (_) {
		return false;
	}
}

// ── WebSocket Manager ──────────────────────────────────────────────────────
const wsManager = {
	socket: null,
	reconnectAttempts: 0,
	reconnectTimer: null,
	pingTimer: null,
	connected: false,
	listeners: new Set(),

	async connect() {
		if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
			return;
		}

		const settings = await getSettings();
		const wsUrl = `ws://${settings.host}:${settings.port}/ws`;

		try {
			this.socket = new WebSocket(wsUrl);

			this.socket.onopen = () => {
				this.connected = true;
				this.reconnectAttempts = 0;
				this._startPing();
				pushDebugEvent({ traceId: createTraceId(), stage: "ws_connected", source: "websocket" });
				this._notify("ws_status", { connected: true });
			};

			this.socket.onmessage = (event) => {
				try {
					const msg = JSON.parse(event.data);
					this._handleMessage(msg);
				} catch (_) { /* ignore parse errors */ }
			};

			this.socket.onclose = () => {
				this.connected = false;
				this._stopPing();
				this._notify("ws_status", { connected: false });
				this._scheduleReconnect();
			};

			this.socket.onerror = () => {
				this.connected = false;
				this._stopPing();
			};
		} catch (err) {
			pushDebugEvent({ traceId: createTraceId(), stage: "ws_error", error: err.message });
			this._scheduleReconnect();
		}
	},

	disconnect() {
		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		this._stopPing();
		if (this.socket) {
			this.socket.onclose = null;
			this.socket.close();
			this.socket = null;
		}
		this.connected = false;
		this.reconnectAttempts = 0;
	},

	_startPing() {
		this._stopPing();
		this.pingTimer = setInterval(() => {
			if (this.socket && this.socket.readyState === WebSocket.OPEN) {
				try { this.socket.send(JSON.stringify({ type: "ping" })); } catch (_) {}
			}
		}, WS_PING_INTERVAL_MS);
	},

	_stopPing() {
		if (this.pingTimer) {
			clearInterval(this.pingTimer);
			this.pingTimer = null;
		}
	},

	_scheduleReconnect() {
		if (this.reconnectTimer) return;
		const delay = Math.min(WS_RECONNECT_BASE_MS * Math.pow(2, this.reconnectAttempts), WS_RECONNECT_MAX_MS);
		this.reconnectAttempts++;
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			this.connect();
		}, delay);
	},

	_handleMessage(msg) {
		const type = msg?.type || "";

		if (type === "progress" || type === "status" || type === "complete" || type === "added") {
			this._notify(type, msg);
		}

		if (type === "complete" && msg.download_id) {
			notificationManager.showDownloadComplete(msg.download_id);
			statsTracker.recordCompletion();
		}

		if (type === "added" && msg.data?.filename) {
			statsTracker.recordAdded();
		}
	},

	addListener(fn) { this.listeners.add(fn); },
	removeListener(fn) { this.listeners.delete(fn); },

	_notify(type, data) {
		for (const fn of this.listeners) {
			try { fn(type, data); } catch (_) {}
		}
	},
};

// ── Notification Manager ───────────────────────────────────────────────────
const notificationManager = {
	enabled: true,

	async showDownloadComplete(downloadId) {
		if (!this.enabled) return;
		try {
			const settings = await getSettings();
			if (!settings.showNotifications) return;

			let filename = "Download";
			try {
				const res = await callApi(`/api/status/${encodeURIComponent(downloadId)}`, { method: "GET" }, 3000);
				if (res.ok && res.data?.success) {
					filename = res.data.data?.filename || filename;
				}
			} catch (_) {}

			const shortName = filename.length > 40 ? filename.slice(0, 37) + "..." : filename;
			chrome.notifications.create(`idm-complete-${downloadId.slice(0, 8)}`, {
				type: "basic",
				iconUrl: NOTIFICATION_ICON_PATH,
				title: "Download Complete",
				message: `${shortName} has finished downloading.`,
				priority: 1,
			});
		} catch (_) { /* Notifications might not be available */ }
	},

	async showDownloadFailed(downloadId, error) {
		if (!this.enabled) return;
		try {
			const shortErr = (error || "Unknown error").slice(0, 60);
			chrome.notifications.create(`idm-fail-${downloadId.slice(0, 8)}`, {
				type: "basic",
				iconUrl: NOTIFICATION_ICON_PATH,
				title: "Download Failed",
				message: shortErr,
				priority: 2,
			});
		} catch (_) {}
	},

	async showBatchComplete(count) {
		try {
			chrome.notifications.create(`idm-batch-${Date.now()}`, {
				type: "basic",
				iconUrl: NOTIFICATION_ICON_PATH,
				title: "Batch Download Queued",
				message: `${count} file(s) sent to IDM for download.`,
				priority: 1,
			});
		} catch (_) {}
	},
};

// ── Download Statistics Tracker ────────────────────────────────────────────
const statsTracker = {
	_cache: null,

	async _load() {
		if (this._cache) return this._cache;
		const res = await chrome.storage.local.get(STATS_STORAGE_KEY);
		this._cache = res[STATS_STORAGE_KEY] || this._defaults();
		return this._cache;
	},

	_defaults() {
		return {
			totalAdded: 0,
			totalCompleted: 0,
			totalFailed: 0,
			todayAdded: 0,
			todayCompleted: 0,
			lastResetDate: new Date().toISOString().slice(0, 10),
			sessionStarted: Date.now(),
		};
	},

	async _save(stats) {
		this._cache = stats;
		await chrome.storage.local.set({ [STATS_STORAGE_KEY]: stats });
	},

	_checkDayReset(stats) {
		const today = new Date().toISOString().slice(0, 10);
		if (stats.lastResetDate !== today) {
			stats.todayAdded = 0;
			stats.todayCompleted = 0;
			stats.lastResetDate = today;
		}
		return stats;
	},

	async recordAdded() {
		const stats = this._checkDayReset(await this._load());
		stats.totalAdded++;
		stats.todayAdded++;
		await this._save(stats);
	},

	async recordCompletion() {
		const stats = this._checkDayReset(await this._load());
		stats.totalCompleted++;
		stats.todayCompleted++;
		await this._save(stats);
	},

	async recordFailure() {
		const stats = this._checkDayReset(await this._load());
		stats.totalFailed++;
		await this._save(stats);
	},

	async getStats() {
		return this._checkDayReset(await this._load());
	},

	async resetStats() {
		await this._save(this._defaults());
	},
};

// ── Connection Health Monitor ──────────────────────────────────────────────
const healthMonitor = {
	lastHealthy: 0,
	consecutiveFailures: 0,
	status: "unknown",

	async check() {
		try {
			const result = await callApi("/api/health", { method: "GET" }, 4000);
			if (result.ok && result.data?.success) {
				this.lastHealthy = Date.now();
				this.consecutiveFailures = 0;
				this.status = "healthy";
				if (!wsManager.connected) {
					wsManager.connect();
				}
				return { ok: true, status: "healthy" };
			}
			this.consecutiveFailures++;
			this.status = "unhealthy";
			return { ok: false, status: "unhealthy", error: result.error };
		} catch (err) {
			this.consecutiveFailures++;
			this.status = "unreachable";
			return { ok: false, status: "unreachable", error: err.message };
		}
	},

	getStatus() {
		return {
			status: this.status,
			lastHealthy: this.lastHealthy,
			consecutiveFailures: this.consecutiveFailures,
			wsConnected: wsManager.connected,
		};
	},
};

// ── Batch Download Support ─────────────────────────────────────────────────
async function addBatchDownloads(urls, metadata = {}) {
	if (!Array.isArray(urls) || !urls.length) {
		return { ok: false, error: "No URLs provided", results: [] };
	}

	const results = [];
	const chunks = [];
	for (let i = 0; i < urls.length; i += BATCH_MAX_CONCURRENT) {
		chunks.push(urls.slice(i, i + BATCH_MAX_CONCURRENT));
	}

	for (const chunk of chunks) {
		const batch = await Promise.allSettled(
			chunk.map((url) =>
				addDownloadWithContext(typeof url === "string" ? url : url?.url || "", {
					referer: metadata.referer || (typeof url === "object" ? url?.referer : null) || null,
					filename: (typeof url === "object" ? url?.filename : "") || "",
					source: "batch_download",
				}, { queueOnFailure: true })
			)
		);

		for (const result of batch) {
			if (result.status === "fulfilled") {
				results.push(result.value);
			} else {
				results.push({ ok: false, error: result.reason?.message || "Failed" });
			}
		}
	}

	const successCount = results.filter((r) => r.ok).length;
	if (successCount > 0) {
		notificationManager.showBatchComplete(successCount);
	}

	return {
		ok: successCount > 0,
		total: urls.length,
		succeeded: successCount,
		failed: urls.length - successCount,
		results,
	};
}

async function clearStoredTokenAndPromptRepair(reason = "") {
	const settings = await getSettings();
	if (settings.token) {
		await saveSettings({ ...settings, token: "" });
	}

	pushDebugEvent({
		traceId: createTraceId(),
		stage: "token_cleared",
		source: "auth",
		error: reason || "Unauthorized",
	});

	try {
		if (chrome.runtime && typeof chrome.runtime.openOptionsPage === "function") {
			chrome.runtime.openOptionsPage();
		}
	} catch (_) {
		// Ignore UI open errors in background contexts.
	}
}

async function pairWithCode(pairingCode, overrides = {}) {
	const settings = await getSettings();
	const host = String(overrides.host || settings.host || "127.0.0.1").trim();
	const port = Number(overrides.port || settings.port || 6800) || 6800;
	const normalizedCode = String(pairingCode || "").trim().toUpperCase();

	if (!normalizedCode) {
		return { ok: false, error: "Pairing code is required" };
	}

	const probeSettings = { ...settings, host, port };
	const result = await callApi(
		"/api/pair",
		{
			method: "POST",
			body: JSON.stringify({ pairing_code: normalizedCode }),
		},
		5000,
		probeSettings,
	);

	if (!result.ok || !result.data?.success) {
		return { ok: false, error: result.error || result.data?.error || "Pairing failed" };
	}

	const token = String(result.data?.data?.session_token || "").trim();
	if (!token) {
		return { ok: false, error: "No session token returned" };
	}

	await saveSettings({ ...probeSettings, token });
	pushDebugEvent({ traceId: createTraceId(), stage: "paired", source: "options" });
	return {
		ok: true,
		token,
		expiresAt: result.data?.data?.expires_at || null,
	};
}

// ── Download Pause/Resume/Cancel from Extension ────────────────────────────
async function controlDownload(downloadId, action) {
	if (!downloadId || !["pause", "resume", "cancel"].includes(action)) {
		return { ok: false, error: "Invalid download ID or action" };
	}

	const result = await callApi(`/api/${action}`, {
		method: "POST",
		body: JSON.stringify({ download_id: downloadId }),
	});

	if (!result.ok) {
		return { ok: false, error: result.error || `Failed to ${action}` };
	}

	if (!result.data?.success) {
		return { ok: false, error: result.data?.error || `${action} failed` };
	}

	return { ok: true, action, download_id: downloadId };
}

// ── Get Full Download History ──────────────────────────────────────────────
async function getDownloadHistory(options = {}) {
	const limit = Math.min(50, Math.max(1, Number(options.limit) || 20));
	const offset = Math.max(0, Number(options.offset) || 0);
	const status = options.status || "";

	let path = `/api/downloads?limit=${limit}&offset=${offset}`;
	if (status) path += `&status=${encodeURIComponent(status)}`;

	const result = await callApi(path, { method: "GET" }, 5000);
	if (!result.ok || !result.data?.success) {
		return { ok: false, error: result.error || result.data?.error || "Failed to fetch history" };
	}

	return {
		ok: true,
		downloads: result.data.data?.downloads || [],
		total: result.data.data?.total || 0,
	};
}

// ── Server Statistics ──────────────────────────────────────────────────────
async function getServerStats() {
	const result = await callApi("/api/stats", { method: "GET" }, 4000);
	if (!result.ok || !result.data?.success) {
		return { ok: false, error: result.error || "Failed to get stats" };
	}
	return { ok: true, data: result.data.data || {} };
}

function createTraceId() {
	return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
}

function summarizeDebugUrl(url) {
	if (!url || typeof url !== "string") {
		return "";
	}

	const trimmed = url.trim();
	if (trimmed.startsWith("data:")) {
		const mime = trimmed.slice(5, trimmed.indexOf(";") > 0 ? trimmed.indexOf(";") : 40);
		return `data:${mime || "unknown"} [omitted]`;
	}

	if (trimmed.length > 240) {
		return `${trimmed.slice(0, 240)}...`;
	}

	return trimmed;
}

function pushDebugEvent(event) {
	debugEvents.unshift({
		ts: new Date().toISOString(),
		...event,
	});
	if (debugEvents.length > DEBUG_LOG_LIMIT) {
		debugEvents.length = DEBUG_LOG_LIMIT;
	}
}

function cookieCount(cookieHeader) {
	if (!cookieHeader) {
		return 0;
	}
	return cookieHeader.split(";").filter((p) => p.trim().length > 0).length;
}

function parseCookieAllowlist(rawAllowlist) {
	if (typeof rawAllowlist !== "string") {
		return [];
	}

	return rawAllowlist
		.split(/[\n,]/g)
		.map((item) => item.trim().toLowerCase())
		.filter((item) => Boolean(item));
}

function isHostAllowedByCookieAllowlist(hostname, rawAllowlist) {
	if (!hostname) {
		return false;
	}

	const host = hostname.toLowerCase();
	const entries = parseCookieAllowlist(rawAllowlist);

	if (!entries.length) {
		// Secure default: empty allowlist means no cookie forwarding.
		return false;
	}

	for (const entry of entries) {
		if (entry === "*") {
			return true;
		}

		const normalized = entry.startsWith("*.")
			? entry.slice(2)
			: entry.startsWith(".")
				? entry.slice(1)
				: entry;

		if (!normalized) {
			continue;
		}

		if (host === normalized || host.endsWith(`.${normalized}`)) {
			return true;
		}
	}

	return false;
}

function getRememberedContextTarget(tabId) {
	if (typeof tabId !== "number") {
		return null;
	}
	const item = contextTargetByTab.get(tabId);
	if (!item) {
		return null;
	}
	if (Date.now() - item.ts > CONTEXT_TARGET_TTL_MS) {
		contextTargetByTab.delete(tabId);
		return null;
	}
	return item;
}

async function getCookieHeaderForUrl(url, settings = null, options = {}) {
	if (!url || typeof url !== "string") {
		return "";
	}
	if (!chrome.cookies || typeof chrome.cookies.getAll !== "function") {
		return "";
	}

	const effectiveSettings = settings || (await getSettings());
	if (!effectiveSettings.forwardCookies) {
		return "";
	}

	const hasCookiesPermission = await ensureOptionalPermissions(
		["cookies"],
		["http://*/*", "https://*/*"]
	);
	if (!hasCookiesPermission) {
		return "";
	}

	let parsed = null;
	try {
		parsed = new URL(url);
	} catch (_) {
		return "";
	}

	const ignoreAllowlist = Boolean(options.ignoreAllowlist);
	if (!ignoreAllowlist && !isHostAllowedByCookieAllowlist(parsed.hostname, effectiveSettings.cookieForwardAllowlist)) {
		return "";
	}

	try {
		const cookies = await chrome.cookies.getAll({ url });
		if (!cookies || !cookies.length) {
			return "";
		}
		return cookies
			.filter((c) => c && c.name)
			.map((c) => `${c.name}=${c.value || ""}`)
			.join("; ");
	} catch (_) {
		return "";
	}
}

function mergeCookieHeaders(primary, secondary) {
	const cookieMap = new Map();
	for (const raw of [primary || "", secondary || ""]) {
		const parts = String(raw)
			.split(";")
			.map((p) => p.trim())
			.filter((p) => Boolean(p));
		for (const part of parts) {
			const idx = part.indexOf("=");
			if (idx <= 0) {
				continue;
			}
			const key = part.slice(0, idx).trim();
			const value = part.slice(idx + 1).trim();
			if (!key) {
				continue;
			}
			cookieMap.set(key, value);
		}
	}

	return Array.from(cookieMap.entries())
		.map(([k, v]) => `${k}=${v}`)
		.join("; ");
}

async function getSettings() {
	const res = await chrome.storage.local.get("settings");
	return { ...DEFAULT_SETTINGS, ...(res.settings || {}) };
}

async function saveSettings(settings) {
	const merged = {
		...DEFAULT_SETTINGS,
		...(settings || {}),
		disabledSites: normalizeDisabledSites(settings?.disabledSites),
	};
	await chrome.storage.local.set({ settings: merged });
	return merged;
}

async function ensureSettingsInitialized() {
	const res = await chrome.storage.local.get("settings");
	if (!res.settings) {
		await chrome.storage.local.set({ settings: { ...DEFAULT_SETTINGS } });
		return { ...DEFAULT_SETTINGS };
	}
	const merged = { ...DEFAULT_SETTINGS, ...(res.settings || {}) };
	merged.disabledSites = normalizeDisabledSites(merged.disabledSites);
	if (JSON.stringify(merged) !== JSON.stringify(res.settings)) {
		await chrome.storage.local.set({ settings: merged });
	}
	return merged;
}

function apiBase(settings) {
	return `http://${settings.host}:${settings.port}`;
}

function normalizeBridgeUrl(raw) {
	if (!raw || typeof raw !== "string") {
		return "";
	}

	try {
		const parsed = new URL(raw);
		const wrapped =
			parsed.searchParams.get("imgurl") ||
			parsed.searchParams.get("mediaurl") ||
			parsed.searchParams.get("url") ||
			parsed.searchParams.get("u") ||
			"";
		if (wrapped && /^(https?|ftp):\/\//i.test(wrapped)) {
			return wrapped;
		}
		return parsed.toString();
	} catch (_) {
		return "";
	}
}

function normalizeBridgeReferer(rawUrl, fallbackReferer = "") {
	const fallback = String(fallbackReferer || "").trim();
	if (!rawUrl || typeof rawUrl !== "string") {
		return fallback;
	}

	try {
		const parsed = new URL(rawUrl);
		const wrappedReferer =
			parsed.searchParams.get("imgrefurl") ||
			parsed.searchParams.get("refurl") ||
			parsed.searchParams.get("pageurl") ||
			"";
		if (wrappedReferer && /^(https?|ftp):\/\//i.test(wrappedReferer)) {
			return wrappedReferer;
		}
	} catch (_) {
		// Fall back to provided referer.
	}

	return fallback;
}

function buildBridgeHeaders(metadata = {}, normalizedReferer = "") {
	const sourceHeaders = metadata && typeof metadata.headers === "object" ? metadata.headers : {};
	const headers = { ...sourceHeaders };

	const hasHeader = (name) => Object.keys(headers).some((k) => String(k).toLowerCase() === name);

	if (!hasHeader("user-agent") && navigator.userAgent) {
		headers["User-Agent"] = navigator.userAgent;
	}
	if (!hasHeader("accept")) {
		headers.Accept = "*/*";
	}
	if (!hasHeader("accept-language") && navigator.language) {
		headers["Accept-Language"] = navigator.language;
	}

	const referer = String(normalizedReferer || metadata.pageUrl || "").trim();
	if (referer && !hasHeader("referer")) {
		headers.Referer = referer;
	}
	if (referer && !hasHeader("origin")) {
		try {
			headers.Origin = new URL(referer).origin;
		} catch (_) {
			// Ignore malformed referer values.
		}
	}

	return Object.keys(headers).length ? headers : null;
}

async function callApi(path, options = {}, timeoutMs = 7000, settingsOverride = null) {
	const settings = settingsOverride || (await getSettings());
	const headers = {
		"Content-Type": "application/json",
		...(options.headers || {}),
	};

	if (settings.token) {
		headers.Authorization = `Bearer ${settings.token}`;
	}

	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), timeoutMs);

	try {
		const resp = await fetch(`${apiBase(settings)}${path}`, {
			...options,
			headers,
			signal: controller.signal,
		});

		let data = null;
		try {
			data = await resp.json();
		} catch (_) {
			data = null;
		}

		if (!resp.ok) {
			if (resp.status === 401) {
				await clearStoredTokenAndPromptRepair("401 Unauthorized");
			}
			return {
				ok: false,
				status: resp.status,
				error: data?.error || `HTTP ${resp.status}`,
			};
		}

		return { ok: true, data };
	} catch (err) {
		return { ok: false, error: err.message || "Connection failed" };
	} finally {
		clearTimeout(timeout);
	}
}

async function autoDetectServer() {
	const current = await getSettings();
	const hosts = [...new Set([current.host || "127.0.0.1", "127.0.0.1", "localhost"])];
	const ports = [...new Set([Number(current.port) || 6800, 6800, 6801, 6802])];

	for (const host of hosts) {
		for (const port of ports) {
			const probeSettings = {
				...current,
				host,
				port,
			};

			const result = await callApi("/api/health", { method: "GET" }, 2500, probeSettings);
			if (result.ok && result.data?.success) {
				const merged = await saveSettings({ ...current, host, port });
				return {
					ok: true,
					settings: merged,
					health: result.data?.data || {},
				};
			}
		}
	}

	return { ok: false, error: "Could not auto-detect IDM bridge server" };
}

async function addDownload(url, metadata = {}, settings = null) {
	const normalizedUrl = normalizeBridgeUrl(url);
	if (!normalizedUrl) {
		return { ok: false, error: "Invalid URL" };
	}
	const normalizedReferer = normalizeBridgeReferer(url, metadata.referer || metadata.pageUrl || "");

	const normalizedSettings = settings || (await getSettings());
	const bridgeHeaders = buildBridgeHeaders(metadata, normalizedReferer);

	const payload = {
		url: normalizedUrl,
		filename: metadata.filename || "",
		referer: normalizedReferer || null,
		page_url: metadata.pageUrl || normalizedReferer || null,
		cookies: metadata.cookies || "",
		headers: bridgeHeaders,
		disabled_sites: normalizeDisabledSites(normalizedSettings?.disabledSites),
	};

	const result = await callApi("/api/add", {
		method: "POST",
		body: JSON.stringify(payload),
	});

	if (!result.ok) {
		return result;
	}

	if (!result.data?.success) {
		return { ok: false, error: result.data?.error || "Failed to add download" };
	}

	return {
		ok: true,
		download_id: result.data?.data?.download_id || "",
	};
}

async function addDownloadWithContext(url, metadata = {}, options = {}) {
	const traceId = metadata.traceId || createTraceId();
	const queueOnFailure = Boolean(options.queueOnFailure);
	const settings = await getSettings();
	const normalizedUrl = normalizeBridgeUrl(url);
	const normalizedReferer = normalizeBridgeReferer(url, metadata.referer || metadata.pageUrl || "");
	const contextPageUrl = metadata.pageUrl || normalizedReferer || "";
	if (isSiteDisabledForUrl(contextPageUrl, settings) || isSiteDisabledForUrl(normalizedUrl, settings)) {
		pushDebugEvent({
			traceId,
			stage: "capture_skipped",
			source: metadata.source || "unknown",
			url: summarizeDebugUrl(normalizedUrl),
			error: "Site disabled by extension settings",
		});
		return { ok: false, error: "Site disabled in extension settings" };
	}

	pushDebugEvent({
		traceId,
		stage: "capture_start",
		source: metadata.source || "unknown",
		url: summarizeDebugUrl(normalizedUrl),
		referer: normalizedReferer || "",
	});

	const protectedHlsFlow = metadata.source === "hls_stream_capture";
	let cookies = metadata.cookies || (await getCookieHeaderForUrl(normalizedUrl, settings, {
		ignoreAllowlist: protectedHlsFlow,
	}));
	if (!cookies && normalizedReferer) {
		const refererCookies = await getCookieHeaderForUrl(normalizedReferer, settings, {
			ignoreAllowlist: protectedHlsFlow,
		});
		cookies = mergeCookieHeaders(cookies, refererCookies);
	}
	if (cookies && metadata.cookies) {
		cookies = mergeCookieHeaders(cookies, metadata.cookies);
	}
	pushDebugEvent({
		traceId,
		stage: cookies ? "cookies_loaded" : "cookies_skipped",
		cookieCount: cookieCount(cookies),
		source: metadata.source || "unknown",
	});

	const result = await addDownload(normalizedUrl, {
		...metadata,
		referer: normalizedReferer,
		cookies,
	}, settings);

	if (result.ok) {
		pushDebugEvent({
			traceId,
			stage: "added_to_idm",
			downloadId: result.download_id || "",
		});
	} else {
		let queued = null;
		if (queueOnFailure && isTransientAddFailure(result)) {
			queued = await enqueuePendingDownload(normalizedUrl, { ...metadata, cookies }, result);
		}

		pushDebugEvent({
			traceId,
			stage: queued?.queued ? "capture_queued" : "capture_failed",
			error: result.error || "Unknown error",
			queueLength: queued?.queueLength || 0,
		});

		if (queued?.queued) {
			return {
				ok: false,
				queued: true,
				queueLength: queued.queueLength,
				error: result.error || "Connection failed",
			};
		}
	}

	return result;
}

function normalizeMinCaptureBytes(rawValue) {
	const value = Number(rawValue);
	if (!Number.isFinite(value) || value < 0) {
		return DEFAULT_SETTINGS.autoCaptureMinBytes;
	}
	return Math.floor(value);
}

function normalizeDisabledSites(rawValue) {
	const items = Array.isArray(rawValue)
		? rawValue
		: String(rawValue || "")
			.split(/[\n,]+/)
			.map((part) => part.trim());

	const out = [];
	const seen = new Set();
	for (const item of items) {
		if (!item) {
			continue;
		}

		let normalized = item.toLowerCase().trim();
		normalized = normalized.replace(/^https?:\/\//, "");
		normalized = normalized.replace(/\/.*/, "");
		normalized = normalized.replace(/^\*\./, "");
		normalized = normalized.replace(/^\./, "");
		normalized = normalized.trim();

		if (!normalized || !/^[a-z0-9.-]+$/.test(normalized)) {
			continue;
		}

		if (seen.has(normalized)) {
			continue;
		}
		seen.add(normalized);
		out.push(normalized);
	}

	return out;
}

function extractHostname(urlOrHost) {
	const raw = String(urlOrHost || "").trim();
	if (!raw) {
		return "";
	}

	if (/^[a-z0-9.-]+$/i.test(raw) && !raw.includes("/")) {
		return raw.toLowerCase();
	}

	try {
		return new URL(raw).hostname.toLowerCase();
	} catch (_) {
		return "";
	}
}

function isSiteDisabledForUrl(urlOrHost, settings = {}) {
	const host = extractHostname(urlOrHost);
	if (!host) {
		return false;
	}

	const rules = normalizeDisabledSites(settings.disabledSites);
	for (const rule of rules) {
		if (host === rule || host.endsWith(`.${rule}`)) {
			return true;
		}
	}

	return false;
}

async function isSiteDisabledForTab(tabId, settings = null) {
	if (typeof tabId !== "number" || tabId < 0 || !chrome.tabs || typeof chrome.tabs.get !== "function") {
		return false;
	}

	try {
		const tab = await chrome.tabs.get(tabId);
		const effectiveSettings = settings || (await getSettings());
		return isSiteDisabledForUrl(tab?.url || "", effectiveSettings);
	} catch (_) {
		return false;
	}
}

async function resolveDownloadContextUrl(item) {
	if (item?.referrer) {
		return String(item.referrer || "");
	}

	const tabId = Number(item?.tabId);
	if (!Number.isFinite(tabId) || tabId < 0 || !chrome.tabs || typeof chrome.tabs.get !== "function") {
		return "";
	}

	try {
		const tab = await chrome.tabs.get(tabId);
		return String(tab?.url || "");
	} catch (_) {
		return "";
	}
}

function isTransientAddFailure(result) {
	if (!result || result.ok) {
		return false;
	}
	const error = (result.error || "").toLowerCase();
	if (
		error.includes("connection failed") ||
		error.includes("failed to fetch") ||
		error.includes("network") ||
		error.includes("timed out")
	) {
		return true;
	}

	const status = Number(result.status || 0);
	return status >= 500;
}

async function loadPendingQueue() {
	const res = await chrome.storage.local.get(PENDING_QUEUE_KEY);
	const queue = Array.isArray(res?.[PENDING_QUEUE_KEY]) ? res[PENDING_QUEUE_KEY] : [];
	return queue;
}

async function savePendingQueue(queue) {
	const bounded = Array.isArray(queue)
		? queue.slice(Math.max(0, queue.length - MAX_PENDING_QUEUE_ITEMS))
		: [];
	await chrome.storage.local.set({ [PENDING_QUEUE_KEY]: bounded });
	return bounded;
}

async function schedulePendingRetryAlarm() {
	if (!chrome.alarms || typeof chrome.alarms.create !== "function") {
		return;
	}
	chrome.alarms.create(RETRY_ALARM_NAME, { delayInMinutes: RETRY_ALARM_DELAY_MINUTES });
}

async function clearPendingRetryAlarm() {
	if (!chrome.alarms || typeof chrome.alarms.clear !== "function") {
		return;
	}
	await chrome.alarms.clear(RETRY_ALARM_NAME);
}

function isQueueItemDisabled(item, settings = {}) {
	if (!item || typeof item !== "object") {
		return false;
	}

	return (
		isSiteDisabledForUrl(item.pageUrl || "", settings) ||
		isSiteDisabledForUrl(item.referer || "", settings) ||
		isSiteDisabledForUrl(item.url || "", settings)
	);
}

async function prunePendingQueueForDisabledSites(settings = null) {
	const effectiveSettings = settings || (await getSettings());
	const queue = await loadPendingQueue();
	if (!queue.length) {
		await clearPendingRetryAlarm();
		return { removed: 0, remaining: 0 };
	}

	const keep = queue.filter((item) => !isQueueItemDisabled(item, effectiveSettings));
	const removed = queue.length - keep.length;
	await savePendingQueue(keep);

	if (keep.length) {
		await schedulePendingRetryAlarm();
	} else {
		await clearPendingRetryAlarm();
	}

	return { removed, remaining: keep.length };
}

async function enqueuePendingDownload(url, metadata = {}, failure = {}) {
	if (!url) {
		return { queued: false, queueLength: 0 };
	}

	const queue = await loadPendingQueue();
	const queuedItem = {
		id: `q${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`,
		url,
		filename: metadata.filename || "",
		referer: metadata.referer || null,
		pageUrl: metadata.pageUrl || metadata.referer || null,
		cookies: metadata.cookies || "",
		source: metadata.source || "unknown",
		attempts: 0,
		lastError: failure.error || "App unavailable",
		queuedAt: Date.now(),
		lastTriedAt: 0,
	};

	queue.push(queuedItem);
	const bounded = await savePendingQueue(queue);
	await schedulePendingRetryAlarm();

	return { queued: true, queueLength: bounded.length };
}

async function processPendingQueue(maxToProcess = 8) {
	const settings = await getSettings();
	const queue = await loadPendingQueue();
	if (!queue.length) {
		await clearPendingRetryAlarm();
		return { processed: 0, remaining: 0 };
	}

	const keep = [];
	let processed = 0;

	for (const item of queue) {
		if (isQueueItemDisabled(item, settings)) {
			continue;
		}

		if (processed >= maxToProcess) {
			keep.push(item);
			continue;
		}

		const result = await addDownloadWithContext(item.url, {
			filename: item.filename || "",
			referer: item.referer || null,
			pageUrl: item.pageUrl || item.referer || null,
			cookies: item.cookies || "",
			source: "pending_retry",
		});
		processed += 1;

		if (result.ok) {
			continue;
		}

		const next = {
			...item,
			attempts: Number(item.attempts || 0) + 1,
			lastError: result.error || item.lastError || "Failed",
			lastTriedAt: Date.now(),
		};

		if (isTransientAddFailure(result)) {
			keep.push(next);
		}
	}

	await savePendingQueue(keep);
	if (keep.length) {
		await schedulePendingRetryAlarm();
	} else {
		await clearPendingRetryAlarm();
	}

	return { processed, remaining: keep.length };
}

async function getPendingQueueStats() {
	const queue = await loadPendingQueue();
	const oldestQueuedAt = queue.length ? Math.min(...queue.map((q) => Number(q.queuedAt || Date.now()))) : 0;
	return {
		count: queue.length,
		oldestQueuedAt,
	};
}

async function getActiveDownloadsSnapshot(limit = 8) {
	const cappedLimit = Math.max(1, Math.min(20, Number(limit) || 8));
	const listResult = await callApi(`/api/downloads?status=downloading&limit=${cappedLimit}&offset=0`, {
		method: "GET",
	}, 4000);

	if (!listResult.ok || !listResult.data?.success) {
		return {
			ok: false,
			error: listResult.error || listResult.data?.error || "Failed to fetch active downloads",
		};
	}

	const downloads = Array.isArray(listResult.data?.data?.downloads)
		? listResult.data.data.downloads
		: [];

	const detailed = await Promise.all(
		downloads.map(async (download) => {
			const id = download?.id || "";
			if (!id) {
				return {
					...download,
					speed: 0,
				};
			}

			const statusResult = await callApi(`/api/status/${encodeURIComponent(id)}`, { method: "GET" }, 2500);
			const speed = statusResult.ok && statusResult.data?.success
				? Number(statusResult.data?.data?.speed || 0)
				: 0;

			return {
				...download,
				speed,
			};
		})
	);

	return {
		ok: true,
		downloads: detailed,
		total: Number(listResult.data?.data?.total || detailed.length),
	};
}

function normalizeHlsUrl(rawUrl) {
	if (!rawUrl || typeof rawUrl !== "string") {
		return "";
	}
	try {
		const parsed = new URL(rawUrl);
		parsed.hash = "";
		return parsed.toString();
	} catch (_) {
		return "";
	}
}

function buildHlsIdentityKey(rawUrl) {
	const normalized = normalizeHlsUrl(rawUrl);
	if (!normalized) {
		return "";
	}
	try {
		const parsed = new URL(normalized);
		const normalizedPath = (parsed.pathname || "/").replace(/\/+$/, "") || "/";
		return `${parsed.origin}${normalizedPath}`.toLowerCase();
	} catch (_) {
		return normalized.toLowerCase();
	}
}

function parseResolutionHeight(value) {
	if (!value || typeof value !== "string") {
		return 0;
	}
	const m = value.trim().match(/^(\d+)x(\d+)$/i);
	if (!m) {
		return 0;
	}
	return Number(m[2] || 0);
}

function parseAttributeList(raw) {
	const attrs = {};
	if (!raw || typeof raw !== "string") {
		return attrs;
	}

	const tokens = raw.split(/,(?=(?:[^"]*"[^"]*")*[^"]*$)/g);
	for (const token of tokens) {
		const idx = token.indexOf("=");
		if (idx <= 0) {
			continue;
		}
		const key = token.slice(0, idx).trim().toUpperCase();
		let value = token.slice(idx + 1).trim();
		if (value.startsWith('"') && value.endsWith('"')) {
			value = value.slice(1, -1);
		}
		attrs[key] = value;
	}

	return attrs;
}

function resolveHlsUri(baseUrl, candidate) {
	if (!candidate || typeof candidate !== "string") {
		return "";
	}
	try {
		return new URL(candidate.trim(), baseUrl).toString();
	} catch (_) {
		return "";
	}
}

function buildQualityLabel(variant, index) {
	const height = Number(variant.height || 0);
	if (height > 0) {
		return `${height}p`;
	}
	const bandwidth = Number(variant.bandwidth || 0);
	if (bandwidth > 0) {
		return `${Math.round(bandwidth / 1000)} kbps`;
	}
	return `Variant ${index + 1}`;
}

function parseM3u8Manifest(manifestText, playlistUrl) {
	const lines = String(manifestText || "")
		.split(/\r?\n/g)
		.map((line) => line.trim())
		.filter((line) => line.length > 0);

	const variants = [];
	const segments = [];
	let encrypted = false;
	let playlistType = "unknown";
	let mediaSequence = 0;
	let targetDuration = 0;
	let declaredDuration = 0;

	for (let i = 0; i < lines.length; i++) {
		const line = lines[i];

		if (line.startsWith("#EXT-X-STREAM-INF:")) {
			const attrs = parseAttributeList(line.slice("#EXT-X-STREAM-INF:".length));
			let nextUri = "";
			for (let j = i + 1; j < lines.length; j++) {
				if (!lines[j].startsWith("#")) {
					nextUri = lines[j];
					i = j;
					break;
				}
			}
			if (nextUri) {
				variants.push({
					url: resolveHlsUri(playlistUrl, nextUri),
					bandwidth: Number(attrs.BANDWIDTH || 0),
					avgBandwidth: Number(attrs["AVERAGE-BANDWIDTH"] || 0),
					resolution: attrs.RESOLUTION || "",
					height: parseResolutionHeight(attrs.RESOLUTION || ""),
					codecs: attrs.CODECS || "",
					frameRate: Number(attrs["FRAME-RATE"] || 0),
					name: attrs.NAME || "",
				});
			}
			continue;
		}

		if (line.startsWith("#EXTINF:")) {
			const durationStr = line.slice("#EXTINF:".length).split(",")[0].trim();
			declaredDuration += Number(durationStr || 0);
			let segUri = "";
			for (let j = i + 1; j < lines.length; j++) {
				if (!lines[j].startsWith("#")) {
					segUri = lines[j];
					break;
				}
			}
			if (segUri) {
				segments.push(resolveHlsUri(playlistUrl, segUri));
			}
			continue;
		}

		if (line.startsWith("#EXT-X-KEY:")) {
			encrypted = true;
			continue;
		}

		if (line.startsWith("#EXT-X-PLAYLIST-TYPE:")) {
			playlistType = line.slice("#EXT-X-PLAYLIST-TYPE:".length).trim().toUpperCase();
			continue;
		}

		if (line.startsWith("#EXT-X-TARGETDURATION:")) {
			targetDuration = Number(line.slice("#EXT-X-TARGETDURATION:".length).trim() || 0);
			continue;
		}

		if (line.startsWith("#EXT-X-MEDIA-SEQUENCE:")) {
			mediaSequence = Number(line.slice("#EXT-X-MEDIA-SEQUENCE:".length).trim() || 0);
		}
	}

	const isMaster = variants.length > 0;
	const normalizedVariants = variants
		.map((v, idx) => ({
			id: `q${idx + 1}`,
			label: buildQualityLabel(v, idx),
			url: v.url,
			bandwidth: Number(v.bandwidth || v.avgBandwidth || 0),
			resolution: v.resolution,
			height: Number(v.height || 0),
			codecs: v.codecs || "",
		}))
		.filter((v) => Boolean(v.url))
		.sort((a, b) => {
			if (b.height !== a.height) {
				return b.height - a.height;
			}
			return (b.bandwidth || 0) - (a.bandwidth || 0);
		});

	return {
		isMaster,
		qualities: isMaster
			? normalizedVariants
			: [{
				id: "q1",
				label: targetDuration ? `Auto (${targetDuration}s target)` : "Auto",
				url: playlistUrl,
				bandwidth: 0,
				resolution: "",
				height: 0,
				codecs: "",
			}],
		segmentCount: segments.length,
		segments: segments.slice(0, 60),
		encrypted,
		playlistType,
		mediaSequence,
		targetDuration,
		totalDuration: Number(declaredDuration || 0),
	};
}

function isLikelyM3u8Url(url) {
	if (!url || typeof url !== "string") {
		return false;
	}
	const lower = url.toLowerCase().trim();
	if (lower.includes(".m3u8")) {
		return true;
	}

	// Query-param based HLS hints (avoid broad words like manifest/playlist alone).
	return /[?&](format|type|ext|output|container)=m3u8(&|$)/i.test(lower);
}

function isNoiseRequestUrl(url) {
	if (!url || typeof url !== "string") {
		return true;
	}
	const lower = url.toLowerCase();
	if (/(analytics|doubleclick|googlesyndication|adservice|pixel|tracking)/i.test(lower)) {
		return true;
	}
	if (/(\.css|\.js|\.png|\.jpg|\.jpeg|\.gif|\.svg|\.woff2?|\.ico)(\?|$)/i.test(lower)) {
		return true;
	}
	return false;
}

function cleanupHlsStore() {
	const now = Date.now();

	for (const [key, ts] of hlsStore.requestSeen.entries()) {
		if (now - Number(ts || 0) > HLS_REQUEST_DEDUPE_MS) {
			hlsStore.requestSeen.delete(key);
		}
	}

	const survivors = [];
	for (const streamId of hlsStore.streamOrder) {
		const item = hlsStore.streams.get(streamId);
		if (!item) {
			continue;
		}
		if (now - Number(item.lastSeenAt || 0) > HLS_STREAM_TTL_MS) {
			hlsStore.streams.delete(streamId);
			hlsStore.urlIndex.delete(item.playlistUrl);
			hlsStore.identityIndex.delete(item.identityKey || "");
			hlsStore.parseInFlight.delete(streamId);
			continue;
		}
		survivors.push(streamId);
	}
	hlsStore.streamOrder = survivors.slice(-HLS_STREAM_MAX_ITEMS);

	while (hlsStore.streamOrder.length > HLS_STREAM_MAX_ITEMS) {
		const dropId = hlsStore.streamOrder.shift();
		if (!dropId) {
			break;
		}
		const dropped = hlsStore.streams.get(dropId);
		if (dropped) {
			hlsStore.urlIndex.delete(dropped.playlistUrl);
			hlsStore.identityIndex.delete(dropped.identityKey || "");
		}
		hlsStore.streams.delete(dropId);
		hlsStore.parseInFlight.delete(dropId);
	}
}

function shouldCaptureHlsRequest(details = {}) {
	if (!details.url || details.method !== "GET") {
		return false;
	}
	if (isNoiseRequestUrl(details.url)) {
		return false;
	}
	if (isLikelyM3u8Url(details.url)) {
		return true;
	}
	return false;
}

function pickRelevantHlsRequestHeaders(requestHeaders = []) {
	const allow = new Set([
		"authorization",
		"x-api-key",
		"x-auth-token",
		"x-csrf-token",
		"x-xsrf-token",
		"accept",
		"accept-language",
		"origin",
		"referer",
		"user-agent",
		"cookie",
	]);

	const headers = {};
	for (const header of requestHeaders || []) {
		const name = String(header?.name || "").trim();
		const value = String(header?.value || "").trim();
		if (!name || !value) {
			continue;
		}
		if (!allow.has(name.toLowerCase())) {
			continue;
		}
		headers[name] = value;
	}

	return headers;
}

function upsertDetectedStream(input = {}) {
	const playlistUrl = normalizeHlsUrl(input.url);
	if (!playlistUrl) {
		return null;
	}
	const identityKey = buildHlsIdentityKey(playlistUrl);

	cleanupHlsStore();

	let streamId = hlsStore.urlIndex.get(playlistUrl) || hlsStore.identityIndex.get(identityKey) || "";
	let stream = streamId ? hlsStore.streams.get(streamId) : null;
	const now = Date.now();

	if (!stream) {
		streamId = `hls-${now.toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
		stream = {
			id: streamId,
			playlistUrl,
			identityKey,
			tabId: typeof input.tabId === "number" ? input.tabId : -1,
			initiator: input.initiator || "",
			pageUrl: input.pageUrl || "",
			status: "pending",
			parseAttempts: 0,
			lastError: "",
			detectedAt: now,
			lastSeenAt: now,
			isMaster: false,
			encrypted: false,
			playlistType: "unknown",
			qualities: [],
			segmentCount: 0,
			totalDuration: 0,
			nextRetryAt: 0,
			typeHint: input.typeHint || "",
			requestHeaders: input.requestHeaders || {},
		};
		hlsStore.streams.set(streamId, stream);
		hlsStore.urlIndex.set(playlistUrl, streamId);
		hlsStore.identityIndex.set(identityKey, streamId);
		hlsStore.streamOrder.push(streamId);
	} else {
		if (stream.playlistUrl !== playlistUrl) {
			hlsStore.urlIndex.delete(stream.playlistUrl);
			stream.playlistUrl = playlistUrl;
			hlsStore.urlIndex.set(playlistUrl, streamId);
		}

		if (stream.identityKey !== identityKey) {
			hlsStore.identityIndex.delete(stream.identityKey || "");
			stream.identityKey = identityKey;
			hlsStore.identityIndex.set(identityKey, streamId);
		}

		stream.lastSeenAt = now;
		if (typeof input.tabId === "number" && input.tabId >= 0) {
			stream.tabId = input.tabId;
		}
		if (input.initiator) {
			stream.initiator = input.initiator;
		}
		if (input.pageUrl) {
			stream.pageUrl = input.pageUrl;
		}
		if (input.typeHint) {
			stream.typeHint = input.typeHint;
		}
		if (input.requestHeaders && typeof input.requestHeaders === "object") {
			stream.requestHeaders = {
				...(stream.requestHeaders || {}),
				...input.requestHeaders,
			};
		}
	}

	return stream;
}

async function fetchPlaylistText(stream) {
	const playlistUrl = stream?.playlistUrl || "";
	if (!playlistUrl) {
		throw new Error("Missing playlist URL");
	}

	const pageUrl = stream?.pageUrl || stream?.initiator || "";
	const headers = {};
	if (pageUrl) {
		try {
			const pageOrigin = new URL(pageUrl).origin;
			headers["Origin"] = pageOrigin;
		} catch (_) {
			// Ignore origin derivation errors.
		}
	}

	const resp = await fetch(playlistUrl, {
		method: "GET",
		cache: "no-store",
		credentials: "include",
		referrer: pageUrl || undefined,
		referrerPolicy: "strict-origin-when-cross-origin",
		headers,
	});
	if (!resp.ok) {
		throw new Error(`HTTP ${resp.status}`);
	}
	return {
		text: await resp.text(),
		contentType: resp.headers.get("content-type") || "",
	};
}

async function parseDetectedStream(streamId, options = {}) {
	const force = Boolean(options.force);
	const stream = hlsStore.streams.get(streamId);
	if (!stream) {
		return { ok: false, error: "Stream not found" };
	}
	if (hlsStore.parseInFlight.has(streamId)) {
		return { ok: false, error: "Parse in progress" };
	}
	if (!force && stream.status === "parsed" && stream.qualities.length > 0) {
		return { ok: true, stream };
	}

	hlsStore.parseInFlight.add(streamId);
	stream.status = "parsing";
	stream.parseAttempts = Number(stream.parseAttempts || 0) + 1;
	stream.lastSeenAt = Date.now();

	try {
		const fetched = await fetchPlaylistText(stream);
		const parsed = parseM3u8Manifest(fetched.text, stream.playlistUrl);
		stream.status = "parsed";
		stream.lastError = "";
		stream.nextRetryAt = 0;
		stream.isMaster = parsed.isMaster;
		stream.encrypted = parsed.encrypted;
		stream.playlistType = parsed.playlistType;
		stream.segmentCount = parsed.segmentCount;
		stream.totalDuration = parsed.totalDuration;
		stream.qualities = parsed.qualities;
		stream.previewSegments = parsed.segments;
		stream.typeHint = fetched.contentType || stream.typeHint;

		pushDebugEvent({
			traceId: createTraceId(),
			stage: "hls_parsed",
			source: "web_request",
			url: summarizeDebugUrl(stream.playlistUrl),
			qualityCount: stream.qualities.length,
			encrypted: stream.encrypted,
		});

		return { ok: true, stream };
	} catch (err) {
		stream.lastError = err.message || "Failed to parse playlist";
		const attempts = Number(stream.parseAttempts || 1);
		const authBlocked = /^http\s+40[13]$/i.test(String(stream.lastError || "").trim());
		stream.status = authBlocked ? "captured" : "failed";
		if (authBlocked) {
			stream.lastError = `${stream.lastError} (protected stream)`;
		}
		if (!authBlocked && attempts < HLS_PARSE_RETRY_LIMIT) {
			const delayMs = Math.min(30000, Math.pow(2, attempts) * 2000);
			stream.nextRetryAt = Date.now() + delayMs;
			if (chrome.alarms && chrome.alarms.create) {
				chrome.alarms.create(HLS_PARSE_RETRY_ALARM, { when: stream.nextRetryAt });
			}
		} else {
			stream.nextRetryAt = 0;
		}

		pushDebugEvent({
			traceId: createTraceId(),
			stage: "hls_parse_failed",
			source: "web_request",
			url: summarizeDebugUrl(stream.playlistUrl),
			error: stream.lastError,
			attempt: attempts,
			authBlocked,
		});

		return { ok: false, error: stream.lastError };
	} finally {
		hlsStore.parseInFlight.delete(streamId);
	}
}

function scheduleStreamParse(streamId, delayMs = 0) {
	setTimeout(() => {
		parseDetectedStream(streamId).catch(() => {});
	}, Math.max(0, Number(delayMs || 0)));
}

function getContentTypeFromHeaders(responseHeaders = []) {
	for (const header of responseHeaders) {
		const name = String(header?.name || "").toLowerCase();
		if (name !== "content-type") {
			continue;
		}
		return String(header?.value || "").toLowerCase();
	}
	return "";
}

function captureHlsCandidate(details, extra = {}) {
	if (!details?.url) {
		return;
	}

	const typeHint = String(extra.typeHint || "").toLowerCase();
	const urlLooksLikePlaylist = isLikelyM3u8Url(details.url);
	const headerLooksLikePlaylist =
		typeHint.includes("mpegurl") ||
		typeHint.includes("vnd.apple.mpegurl");
	if (!urlLooksLikePlaylist && !headerLooksLikePlaylist) {
		return;
	}

	const stableKey = buildHlsIdentityKey(details.url);
	const dedupeKey = `${details.tabId || -1}|${stableKey || normalizeHlsUrl(details.url)}`;
	const now = Date.now();
	const previous = Number(hlsStore.requestSeen.get(dedupeKey) || 0);
	if (now - previous < HLS_REQUEST_DEDUPE_MS) {
		return;
	}
	hlsStore.requestSeen.set(dedupeKey, now);

	const stream = upsertDetectedStream({
		url: details.url,
		tabId: details.tabId,
		initiator: details.initiator || details.documentUrl || "",
		pageUrl: details.documentUrl || details.initiator || "",
		typeHint: extra.typeHint || "",
		requestHeaders: extra.requestHeaders || {},
	});

	if (!stream) {
		return;
	}

	pushDebugEvent({
		traceId: createTraceId(),
		stage: "hls_detected",
		source: extra.source || "web_request",
		url: summarizeDebugUrl(stream.playlistUrl),
		tabId: stream.tabId,
	});

	const isProtected =
		stream.status === "captured" &&
		/protected stream/i.test(String(stream.lastError || ""));
	if (isProtected) {
		return;
	}

	scheduleStreamParse(stream.id, 50);
}

async function captureHlsCandidateIfEnabled(details, extra = {}) {
	const settings = await getSettings();
	const contextUrl = details?.documentUrl || details?.initiator || details?.url || "";
	if (isSiteDisabledForUrl(contextUrl, settings)) {
		return;
	}

	if (await isSiteDisabledForTab(details?.tabId, settings)) {
		return;
	}

	captureHlsCandidate(details, extra);
}

function registerHlsInterceptors() {
	if (!chrome.webRequest || !chrome.webRequest.onBeforeRequest) {
		return;
	}

	ensureOptionalPermissions(["webRequest"], ["http://*/*", "https://*/*"])
		.then((allowed) => {
			if (!allowed) {
				return;
			}

			chrome.webRequest.onBeforeRequest.addListener(
				(details) => {
					if (!shouldCaptureHlsRequest(details)) {
						return;
					}
					captureHlsCandidateIfEnabled(details, { source: "onBeforeRequest" }).catch(() => {});
				},
				{ urls: ["<all_urls>"], types: ["xmlhttprequest", "media", "other", "main_frame", "sub_frame"] }
			);

			chrome.webRequest.onHeadersReceived.addListener(
				(details) => {
					const contentType = getContentTypeFromHeaders(details.responseHeaders || []);
					if (!contentType) {
						return;
					}
					if (
						contentType.includes("application/vnd.apple.mpegurl") ||
						contentType.includes("application/x-mpegurl") ||
						contentType.includes("audio/mpegurl") ||
						contentType.includes("application/mpegurl")
					) {
						captureHlsCandidateIfEnabled(details, { source: "onHeadersReceived", typeHint: contentType }).catch(() => {});
					}
				},
				{ urls: ["<all_urls>"], types: ["xmlhttprequest", "media", "other", "main_frame", "sub_frame"] },
				["responseHeaders"]
			);

			chrome.webRequest.onBeforeSendHeaders.addListener(
				(details) => {
					if (!shouldCaptureHlsRequest(details)) {
						return;
					}
					const requestHeaders = pickRelevantHlsRequestHeaders(details.requestHeaders || []);
					captureHlsCandidateIfEnabled(details, { source: "onBeforeSendHeaders", requestHeaders }).catch(() => {});
				},
				{ urls: ["<all_urls>"], types: ["xmlhttprequest", "media", "other", "main_frame", "sub_frame"] },
				["requestHeaders", "extraHeaders"]
			);
		})
		.catch(() => {});
}

function getDetectedStreams(tabId = null) {
	cleanupHlsStore();
	const raw = hlsStore.streamOrder
		.map((id) => hlsStore.streams.get(id))
		.filter((s) => Boolean(s))
		.filter((s) => (typeof tabId === "number" && tabId >= 0 ? s.tabId === tabId : true))
		.sort((a, b) => Number(b.lastSeenAt || 0) - Number(a.lastSeenAt || 0));

	// Extra safety: collapse accidental duplicates by stable identity per tab.
	const collapsed = [];
	const seen = new Set();
	for (const s of raw) {
		const key = `${s.tabId}|${s.identityKey || buildHlsIdentityKey(s.playlistUrl) || s.playlistUrl}`;
		if (seen.has(key)) {
			continue;
		}
		seen.add(key);
		collapsed.push(s);
	}

	const streams = collapsed.map((s) => ({
			id: s.id,
			playlistUrl: s.playlistUrl,
			tabId: s.tabId,
			status: s.status,
			detectedAt: s.detectedAt,
			lastSeenAt: s.lastSeenAt,
			isMaster: s.isMaster,
			encrypted: s.encrypted,
			playlistType: s.playlistType,
			segmentCount: s.segmentCount,
			totalDuration: s.totalDuration,
			lastError: s.lastError,
			typeHint: s.typeHint || "",
			qualities: Array.isArray(s.qualities) ? s.qualities : [],
		}));

	return streams;
}

function resolveStreamUrl(stream, qualityId = "") {
	if (!stream) {
		return "";
	}
	const qualities = Array.isArray(stream.qualities) ? stream.qualities : [];
	if (qualityId) {
		const chosen = qualities.find((q) => q.id === qualityId);
		if (chosen?.url) {
			return chosen.url;
		}
	}
	if (qualities.length) {
		return qualities[0].url;
	}
	return stream.playlistUrl || "";
}

async function processHlsRetryQueue() {
	const now = Date.now();
	const due = [];
	for (const streamId of hlsStore.streamOrder) {
		const stream = hlsStore.streams.get(streamId);
		if (!stream) {
			continue;
		}
		if (stream.status === "failed" && Number(stream.nextRetryAt || 0) > 0 && Number(stream.nextRetryAt || 0) <= now) {
			due.push(streamId);
		}
	}

	for (const streamId of due) {
		await parseDetectedStream(streamId, { force: true });
	}
}

function normalizeCaptureMode(mode) {
	if (["strict", "balanced", "aggressive", "all"].includes(mode)) {
		return mode;
	}
	return "all";
}

function isSupportedDownloadScheme(url) {
	if (!url || typeof url !== "string") {
		return false;
	}

	if (url.startsWith("magnet:")) {
		return true;
	}

	try {
		const parsed = new URL(url);
		return ["http:", "https:", "ftp:"].includes(parsed.protocol);
	} catch (_) {
		return false;
	}
}

function hasAnyQueryDownloadHints(parsedUrl) {
	const query = (parsedUrl.search || "").toLowerCase();
	return (
		query.includes("download=") ||
		query.includes("attachment") ||
		query.includes("filename=") ||
		query.includes("file=")
	);
}

function isLikelyDownloadUrl(url, mode = "balanced") {
	if (!url || typeof url !== "string") {
		return false;
	}

	if (!isSupportedDownloadScheme(url)) {
		return false;
	}

	const normalizedMode = normalizeCaptureMode(mode);

	try {
		const parsed = new URL(url);

		if (normalizedMode === "all") {
			return true;
		}

		const path = (parsed.pathname || "").toLowerCase();
		const strictExts = [
			".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".msi", ".iso",
			".apk", ".deb", ".rpm",
		];
		const balancedExtraExts = [
			".mp4", ".mkv", ".mp3", ".flac", ".pdf", ".m4a", ".wav", ".csv",
			".ts", ".m3u8", ".avi", ".webm", ".mov", ".flv", ".wmv", ".rmvb", ".m4v", ".mpg", ".mpeg", ".3gp"
		];
		const aggressiveExtraExts = [
			".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".dmg", ".pkg",
		];

		if (strictExts.some((ext) => path.endsWith(ext))) {
			return true;
		}

		if (
			normalizedMode !== "strict" &&
			balancedExtraExts.some((ext) => path.endsWith(ext))
		) {
			return true;
		}

		if (normalizedMode === "aggressive") {
			if (aggressiveExtraExts.some((ext) => path.endsWith(ext))) {
				return true;
			}
			if (hasAnyQueryDownloadHints(parsed)) {
				return true;
			}
			if ((parsed.pathname || "").toLowerCase().includes("/download")) {
				return true;
			}
		}

		return false;
	} catch (_) {
		return false;
	}
}

function shouldCaptureUrl(url, mode = "all") {
	if (!isSupportedDownloadScheme(url)) {
		return false;
	}

	const normalizedMode = normalizeCaptureMode(mode);
	if (normalizedMode === "all") {
		return true;
	}
	return isLikelyDownloadUrl(url, normalizedMode);
}

function resolveWrappedUrl(candidate) {
	if (!candidate) {
		return "";
	}
	try {
		const parsed = new URL(candidate);
		const wrapped =
			parsed.searchParams.get("imgurl") ||
			parsed.searchParams.get("url") ||
			parsed.searchParams.get("u") ||
			"";
		if (!wrapped) {
			return candidate;
		}
		const decoded = decodeURIComponent(wrapped);
		if (decoded.startsWith("http://") || decoded.startsWith("https://") || decoded.startsWith("ftp://")) {
			return decoded;
		}
		return candidate;
	} catch (_) {
		return candidate;
	}
}

function pickContextMenuUrl(info, tab) {
	const mediaType = (info?.mediaType || "").toLowerCase();
	const srcUrl = resolveWrappedUrl(info?.srcUrl || "");
	const linkUrl = resolveWrappedUrl(info?.linkUrl || "");
	const tabUrl = resolveWrappedUrl(tab?.url || "");

	if (["image", "video", "audio"].includes(mediaType)) {
		return srcUrl || linkUrl || tabUrl || "";
	}

	return linkUrl || srcUrl || tabUrl || "";
}

async function findFallbackContextUrl(tab, mediaType = "") {
	if (!tab?.id) {
		return "";
	}

	const settings = await getSettings();
	if (isSiteDisabledForUrl(tab?.url || "", settings)) {
		return "";
	}

	let scanResponse = null;
	try {
		scanResponse = await chrome.tabs.sendMessage(tab.id, {
			type: "collectSourceCandidates",
			limit: 120,
		});
	} catch (_) {
		return "";
	}

	const candidates = Array.isArray(scanResponse?.candidates)
		? scanResponse.candidates
		: [];
	if (!candidates.length) {
		return "";
	}

	const prefersImage = mediaType === "image";
	if (prefersImage) {
		for (const c of candidates) {
			const u = c?.url || "";
			if (!isSupportedDownloadScheme(u)) {
				continue;
			}
			const lu = u.toLowerCase();
			if (/(\.jpg|\.jpeg|\.png|\.webp|\.gif|\.bmp)(\?|$)/i.test(lu)) {
				return u;
			}
		}
	}

	for (const c of candidates) {
		const u = c?.url || "";
		if (shouldCaptureUrl(u, "all")) {
			return u;
		}
	}

	return "";
}

const autoCaptureInFlight = new Set();

async function maybeAutoCaptureChromeDownload(item) {
	if (!item || typeof item.id !== "number") {
		return;
	}
	if (autoCaptureInFlight.has(item.id)) {
		return;
	}

	const settings = await getSettings();
	if (!settings.autoCaptureDownloads) {
		return;
	}
	const captureMode = normalizeCaptureMode(settings.autoCaptureMode);
	const minBytes = normalizeMinCaptureBytes(settings.autoCaptureMinBytes);
	const disabledRules = normalizeDisabledSites(settings.disabledSites);
	const contextUrl = await resolveDownloadContextUrl(item);

	const url = item.finalUrl || item.url || "";
	if (await isSiteDisabledForTab(Number(item.tabId), settings)) {
		return;
	}
	if (isSiteDisabledForUrl(contextUrl || "", settings) || isSiteDisabledForUrl(url, settings)) {
		return;
	}

	if (disabledRules.length > 0 && !contextUrl) {
		// If we cannot determine origin tab/referrer, avoid accidental captures while site blocks are configured.
		return;
	}
	if (!shouldCaptureUrl(url, captureMode)) {
		return;
	}

	const totalBytes = Number(item.totalBytes || item.fileSize || 0);
	if (totalBytes > 0 && totalBytes < minBytes) {
		return;
	}

	autoCaptureInFlight.add(item.id);
	try {
		const result = await addDownloadWithContext(url, {
			filename: item.filename || "",
			referer: contextUrl || null,
			pageUrl: contextUrl || null,
			source: "downloads_api",
		}, {
			queueOnFailure: true,
		});
		if (result.ok) {
			try {
				await chrome.downloads.cancel(item.id);
			} catch (_) {
				// Some downloads finish too fast to cancel; ignore.
			}
		}
	} finally {
		autoCaptureInFlight.delete(item.id);
	}
}

registerHlsInterceptors();

chrome.runtime.onInstalled.addListener(async () => {
	await ensureSettingsInitialized();
	await processPendingQueue(6);
	await processHlsRetryQueue();

	try {
		await chrome.contextMenus.removeAll();
	} catch (_) {
		// Ignore menu cleanup errors during first install.
	}

	chrome.contextMenus.create({
		id: MENU_ID,
		title: "Download with IDM",
		contexts: ["link", "page", "image", "video", "audio"],
	});

	// Start health check alarm
	if (chrome.alarms && typeof chrome.alarms.create === "function") {
		chrome.alarms.create(HEALTH_CHECK_ALARM, {
			delayInMinutes: 0.1,
			periodInMinutes: HEALTH_CHECK_INTERVAL_MINUTES,
		});
	}

	healthMonitor.check().catch(() => {});
});

chrome.runtime.onStartup.addListener(() => {
	processHlsRetryQueue().catch(() => {});
	healthMonitor.check().catch(() => {});
});

if (chrome.alarms && chrome.alarms.onAlarm) {
	chrome.alarms.onAlarm.addListener((alarm) => {
		if (alarm?.name === RETRY_ALARM_NAME) {
			processPendingQueue().catch(() => {});
			return;
		}

		if (alarm?.name === HEALTH_CHECK_ALARM) {
			healthMonitor.check().catch(() => {});
			return;
		}

		if (alarm?.name === HLS_PARSE_RETRY_ALARM) {
			processHlsRetryQueue().catch(() => {});
			return;
		}
	});
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
	if (info.menuItemId !== MENU_ID) {
		return;
	}

	const settings = await getSettings();
	if (isSiteDisabledForUrl(tab?.url || "", settings)) {
		return;
	}

	const mediaType = (info?.mediaType || "").toLowerCase();
	const remembered = getRememberedContextTarget(tab?.id);
	let url = remembered?.url || pickContextMenuUrl(info, tab);
	pushDebugEvent({
		traceId: createTraceId(),
		stage: "context_target_pick",
		source: remembered ? "remembered_context" : "menu_info",
		url: summarizeDebugUrl(url),
	});

	if (!shouldCaptureUrl(url, "all")) {
		const fallbackUrl = await findFallbackContextUrl(tab, mediaType);
		if (fallbackUrl && shouldCaptureUrl(fallbackUrl, "all")) {
			url = fallbackUrl;
			pushDebugEvent({
				traceId: createTraceId(),
				stage: "context_fallback_used",
				source: "page_scan",
				url: summarizeDebugUrl(url),
			});
		}
	}

	if (!shouldCaptureUrl(url, "all")) {
		pushDebugEvent({
			traceId: createTraceId(),
			stage: "capture_skipped",
			source: "context_menu",
			url: summarizeDebugUrl(url),
			error: "Unsupported or invalid download URL",
		});
		return;
	}

	await addDownloadWithContext(url, {
		referer: remembered?.referer || tab?.url || null,
		pageUrl: tab?.url || remembered?.referer || null,
		filename: remembered?.filename || "",
		source: "context_menu",
	}, {
		queueOnFailure: true,
	});
});

// Browser download interception is disabled by default.
// Manual captures (context menu, popup actions, page detections) still work.

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
	(async () => {
		switch (message?.type) {
			case "rememberContextTarget": {
				const tabId = sender?.tab?.id;
				const settings = await getSettings();
				if (isSiteDisabledForUrl(sender?.tab?.url || message.referer || "", settings)) {
					sendResponse({ ok: true, skipped: true });
					break;
				}
				if (typeof tabId === "number" && message.url) {
					contextTargetByTab.set(tabId, {
						url: message.url,
						referer: message.referer || sender?.tab?.url || "",
						filename: message.filename || "",
						ts: Date.now(),
					});
				}
				sendResponse({ ok: true });
				break;
			}

			case "getSettings": {
				const settings = await getSettings();
				sendResponse({ ok: true, settings });
				break;
			}

			case "saveSettings": {
				const normalized = {
					...(message.settings || {}),
					autoCaptureMinBytes: normalizeMinCaptureBytes(message.settings?.autoCaptureMinBytes),
					disabledSites: normalizeDisabledSites(message.settings?.disabledSites),
				};
				const settings = await saveSettings(normalized);
				await prunePendingQueueForDisabledSites(settings);
				sendResponse({ ok: true, settings });
				break;
			}

			case "ping": {
				const result = await callApi("/api/health", { method: "GET" });
				if (!result.ok) {
					sendResponse({ ok: false, error: result.error });
					break;
				}
				if (!result.data?.success) {
					sendResponse({ ok: false, error: result.data?.error || "Health failed" });
					break;
				}
				sendResponse({ ok: true, data: result.data.data || {} });
				break;
			}

			case "autoDetectServer": {
				const detected = await autoDetectServer();
				if (!detected.ok) {
					sendResponse({ ok: false, error: detected.error });
					break;
				}
				sendResponse({ ok: true, data: detected });
				break;
			}

			case "addDownload": {
				const settings = await getSettings();
				if (isSiteDisabledForUrl(message.pageUrl || message.referer || sender?.tab?.url || message.url || "", settings)) {
					sendResponse({ ok: false, error: "Site disabled in extension settings" });
					break;
				}
				const result = await addDownloadWithContext(message.url || "", {
					referer: message.referer || null,
					pageUrl: message.pageUrl || sender?.tab?.url || message.referer || null,
					filename: message.filename || "",
					source: message.source || "manual_message",
				}, {
					queueOnFailure: true,
				});
				sendResponse(result);
				break;
			}

			case "captureDownloadLink": {
				const userInitiated = Boolean(message.userInitiated);
				const settings = await getSettings();
				if (isSiteDisabledForUrl(message.pageUrl || message.referer || sender?.tab?.url || message.url || "", settings)) {
					sendResponse({ ok: false, captured: false, error: "Site disabled in extension settings" });
					break;
				}
				if (!settings.autoCaptureDownloads && !userInitiated) {
					sendResponse({ ok: false, captured: false, error: "Auto-capture disabled" });
					break;
				}

				const url = message.url || "";
				if (!userInitiated && !shouldCaptureUrl(url, settings.autoCaptureMode)) {
					sendResponse({ ok: false, captured: false, error: "Not a capture candidate" });
					break;
				}

				const result = await addDownloadWithContext(url, {
					filename: message.filename || "",
					referer: message.referer || null,
					pageUrl: message.pageUrl || sender?.tab?.url || message.referer || null,
					source: "content_click_capture",
				}, {
					queueOnFailure: true,
				});

				if (!result.ok) {
					sendResponse({ ok: false, captured: false, error: result.error || "Failed to add" });
					break;
				}

				sendResponse({ ok: true, captured: true, download_id: result.download_id || "" });
				break;
			}

			case "captureStreamCandidates": {
				const candidates = Array.isArray(message.candidates) ? message.candidates : [];
				const settings = await getSettings();
				
				let capturedCount = 0;
				for (const candidate of candidates) {
					const rawUrl = String(candidate.url || "");
					const typeHint = String(candidate.typeHint || "");
					if (!rawUrl) continue;

					const normalizedUrl = normalizeHlsUrl(rawUrl);
					if (!normalizedUrl) continue;

					const typeHintLower = typeHint.toLowerCase();
					const playlistHint =
						isLikelyM3u8Url(normalizedUrl) ||
						typeHintLower.includes("mpegurl") ||
						typeHintLower.includes("vnd.apple.mpegurl");
					if (!playlistHint) continue;

					if (isSiteDisabledForUrl(sender?.tab?.url || candidate.referer || normalizedUrl, settings)) {
						continue;
					}

					captureHlsCandidateIfEnabled({
						url: normalizedUrl,
						tabId: sender?.tab?.id ?? -1,
						initiator: candidate.referer || sender?.tab?.url || "",
						documentUrl: sender?.tab?.url || candidate.referer || "",
						type: "other",
						method: "GET",
					}, {
						source: candidate.source || "content_candidate_batch",
						typeHint,
					}).catch(() => {});
					
					capturedCount++;
				}

				sendResponse({ ok: true, captured: capturedCount > 0, count: capturedCount });
				break;
			}

			case "captureStreamCandidate": {
				const rawUrl = String(message.url || "");
				const typeHint = String(message.typeHint || "");
				if (!rawUrl) {
					sendResponse({ ok: false, error: "Missing URL" });
					break;
				}

				const normalizedUrl = normalizeHlsUrl(rawUrl);
				if (!normalizedUrl) {
					sendResponse({ ok: false, error: "Invalid URL" });
					break;
				}

				const typeHintLower = typeHint.toLowerCase();
				const playlistHint =
					isLikelyM3u8Url(normalizedUrl) ||
					typeHintLower.includes("mpegurl") ||
					typeHintLower.includes("vnd.apple.mpegurl");
				if (!playlistHint) {
					sendResponse({ ok: false, error: "Not a playlist candidate" });
					break;
				}

				const settings = await getSettings();
				if (isSiteDisabledForUrl(sender?.tab?.url || message.referer || normalizedUrl, settings)) {
					sendResponse({ ok: true, skipped: true });
					break;
				}

				captureHlsCandidateIfEnabled({
					url: normalizedUrl,
					tabId: sender?.tab?.id ?? -1,
					initiator: message.referer || sender?.tab?.url || "",
					documentUrl: sender?.tab?.url || message.referer || "",
					type: "other",
					method: "GET",
				}, {
					source: message.source || "content_candidate",
					typeHint,
				}).catch(() => {});

				sendResponse({ ok: true, captured: true });
				break;
			}

			case "scanAndCaptureCurrentTab": {
				const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
				const tab = tabs && tabs.length ? tabs[0] : null;
				if (!tab?.id) {
					sendResponse({ ok: false, error: "No active tab found" });
					break;
				}

				const settings = await getSettings();
				if (isSiteDisabledForUrl(tab.url || "", settings)) {
					sendResponse({ ok: false, error: "Site disabled in extension settings" });
					break;
				}

				let scanResponse = null;
				try {
					scanResponse = await chrome.tabs.sendMessage(tab.id, {
						type: "collectSourceCandidates",
						limit: 80,
					});
				} catch (_) {
					sendResponse({ ok: false, error: "Could not inspect page sources" });
					break;
				}

				const candidates = Array.isArray(scanResponse?.candidates)
					? scanResponse.candidates
					: [];

				if (!candidates.length) {
					sendResponse({ ok: false, error: "No downloadable source found on this page" });
					break;
				}

				let firstSuccess = null;
				for (const candidate of candidates) {
					const candidateUrl = candidate?.url || "";
					if (!candidateUrl || !shouldCaptureUrl(candidateUrl, "all")) {
						continue;
					}
					const result = await addDownloadWithContext(candidateUrl, {
						filename: candidate?.filename || "",
						referer: candidate?.referer || tab.url || null,
						pageUrl: tab.url || candidate?.referer || null,
						source: "popup_scan",
					}, {
						queueOnFailure: true,
					});
					if (result.ok) {
						firstSuccess = {
							download_id: result.download_id || "",
							url: candidateUrl,
						};
						break;
					}
				}

				if (!firstSuccess) {
					sendResponse({ ok: false, error: "Detected sources failed to add in IDM" });
					break;
				}

				sendResponse({ ok: true, data: firstSuccess });
				break;
			}

				case "getDetectedStreams": {
					const tabId = Number(message.tabId);
					const streams = getDetectedStreams(Number.isFinite(tabId) ? tabId : null);
					sendResponse({ ok: true, data: { streams } });
					break;
				}

				case "refreshDetectedStream": {
					const streamId = String(message.streamId || "");
					if (!streamId) {
						sendResponse({ ok: false, error: "Missing stream ID" });
						break;
					}
					const result = await parseDetectedStream(streamId, { force: true });
					sendResponse(result.ok
						? { ok: true }
						: { ok: false, error: result.error || "Failed to parse stream" });
					break;
				}

				case "exportDetectedStreamUrl": {
					const streamId = String(message.streamId || "");
					const qualityId = String(message.qualityId || "");
					const stream = hlsStore.streams.get(streamId);
					if (!stream) {
						sendResponse({ ok: false, error: "Stream not found" });
						break;
					}
					const exportUrl = resolveStreamUrl(stream, qualityId);
					if (!exportUrl) {
						sendResponse({ ok: false, error: "No exportable URL found" });
						break;
					}
					sendResponse({ ok: true, data: { url: exportUrl } });
					break;
				}

				case "downloadDetectedStream": {
					const streamId = String(message.streamId || "");
					const qualityId = String(message.qualityId || "");
					const stream = hlsStore.streams.get(streamId);
					if (!stream) {
						sendResponse({ ok: false, error: "Stream not found" });
						break;
					}
					const selectedUrl = resolveStreamUrl(stream, qualityId);
					if (!selectedUrl) {
						sendResponse({ ok: false, error: "No stream URL available" });
						break;
					}
					const result = await addDownloadWithContext(selectedUrl, {
						referer: stream.pageUrl || stream.initiator || null,
						pageUrl: stream.pageUrl || stream.initiator || null,
						headers: {
							...(stream.requestHeaders || {}),
							"User-Agent": navigator.userAgent || "",
							"Origin": (() => {
								try {
									const base = stream.pageUrl || stream.initiator || "";
									return base ? new URL(base).origin : "";
								} catch (_) {
									return "";
								}
							})(),
						},
						source: "hls_stream_capture",
					}, {
						queueOnFailure: true,
					});
					sendResponse(result.ok
						? { ok: true, data: { download_id: result.download_id || "", url: selectedUrl } }
						: { ok: false, error: result.error || "Failed to add stream" });
					break;
				}

				case "clearDetectedStreams": {
					const tabId = Number(message.tabId);
					if (Number.isFinite(tabId) && tabId >= 0) {
						for (const streamId of [...hlsStore.streamOrder]) {
							const stream = hlsStore.streams.get(streamId);
							if (!stream || stream.tabId !== tabId) {
								continue;
							}
							hlsStore.urlIndex.delete(stream.playlistUrl);
							hlsStore.identityIndex.delete(stream.identityKey || "");
							hlsStore.streams.delete(streamId);
							hlsStore.parseInFlight.delete(streamId);
						}
						hlsStore.streamOrder = hlsStore.streamOrder.filter((id) => {
							const item = hlsStore.streams.get(id);
							return Boolean(item);
						});
					} else {
						hlsStore.streams.clear();
						hlsStore.urlIndex.clear();
						hlsStore.identityIndex.clear();
						hlsStore.streamOrder = [];
						hlsStore.parseInFlight.clear();
					}
					sendResponse({ ok: true });
					break;
				}

			case "getPendingQueueStats": {
				const stats = await getPendingQueueStats();
				sendResponse({ ok: true, data: stats });
				break;
			}

			case "processPendingQueueNow": {
				const outcome = await processPendingQueue();
				sendResponse({ ok: true, data: outcome });
				break;
			}

			case "getActiveDownloads": {
				const active = await getActiveDownloadsSnapshot(message.limit || 8);
				if (!active.ok) {
					sendResponse({ ok: false, error: active.error || "Failed to fetch active downloads" });
					break;
				}

				const queueStats = await getPendingQueueStats();
				sendResponse({
					ok: true,
					data: {
						downloads: active.downloads,
						total: active.total,
						pendingQueue: queueStats,
					},
				});
				break;
			}

			case "getDebugEvents": {
				sendResponse({ ok: true, events: [...debugEvents] });
				break;
			}

			case "clearDebugEvents": {
				debugEvents.length = 0;
				sendResponse({ ok: true });
				break;
			}

			case "batchDownload": {
				const batchResult = await addBatchDownloads(
					message.urls || [],
					{ referer: message.referer || null }
				);
				sendResponse(batchResult);
				break;
			}

			case "controlDownload": {
				const ctrlResult = await controlDownload(
					message.download_id || "",
					message.action || ""
				);
				sendResponse(ctrlResult);
				break;
			}

			case "getDownloadHistory": {
				const histResult = await getDownloadHistory({
					limit: message.limit,
					offset: message.offset,
					status: message.status,
				});
				sendResponse(histResult);
				break;
			}

			case "getServerStats": {
				const srvStats = await getServerStats();
				sendResponse(srvStats);
				break;
			}

			case "getStats": {
				const localStats = await statsTracker.getStats();
				sendResponse({ ok: true, data: localStats });
				break;
			}

			case "resetStats": {
				await statsTracker.resetStats();
				sendResponse({ ok: true });
				break;
			}

			case "getHealthStatus": {
				const health = healthMonitor.getStatus();
				sendResponse({ ok: true, data: health });
				break;
			}

			case "connectWebSocket": {
				await wsManager.connect();
				sendResponse({ ok: true, connected: wsManager.connected });
				break;
			}

			case "disconnectWebSocket": {
				wsManager.disconnect();
				sendResponse({ ok: true });
				break;
			}

			case "pairWithCode": {
				const pairResult = await pairWithCode(
					message.pairing_code || "",
					{ host: message.host, port: message.port },
				);
				sendResponse(pairResult);
				break;
			}

			default:
				sendResponse({ ok: false, error: "Unknown message type" });
		}
	})();

	return true;
});
