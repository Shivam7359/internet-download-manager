// IDM v2.0 — content.js — audited 2026-03-28

const STREAM_URL_RE = /\.(m3u8|mpd|mp4|webm)(\?|$)/i;
const IDM_MEDIA_MARK = "data-idm-media-injected";

let featureFlags = {
  showInlineIdmButtons: true,
  videoStreamDetection: true,
  fileTypes: [],
  minFileSizeKb: 0,
  downloadScoreThreshold: 60,
  showUncertainLinks: false,
  scorerDebugMode: false,
};

const DEFAULT_SETTINGS = {
  capture: {
    auto: false,
    click_intercept: false,
    inline_buttons: true,
    right_click: true,
    streams: true,
    batch: true,
  },
  threshold: 60,
  maxBadgesPerPage: 50,
};

let scorer = null;
const linkState = new WeakMap();
const resolvedLinks = new WeakSet();  // Guard: track links already processed to prevent re-evaluation
const secondPassQueue = new Set();
let secondPassScheduled = false;
const idmClickBypassUrls = new Map();
const headCache = new Map();
const HEAD_CACHE_TTL_MS = 120000;

// Page context detection for file hosting sites
let PAGE_CONTEXT = {
  isFileHostingPage: false,
  hasMultipleServers: false,
  pageFilename: null,
};
let BASE_SCORE_BONUS = 0;
let MAX_BADGES_PER_PAGE = DEFAULT_SETTINGS.maxBadgesPerPage;

function safeSendMessage(payload, callback = null) {
  try {
    chrome.runtime.sendMessage(payload, (response) => {
      void chrome.runtime.lastError;
      if (typeof callback === "function") {
        callback(response || null);
      }
    });
  } catch (_) {
    if (typeof callback === "function") {
      callback(null);
    }
  }
}

function normalizeAbsolute(url) {
  const raw = String(url || "").trim();
  if (!raw || raw.startsWith("#") || /^javascript:/i.test(raw)) {
    return "";
  }
  try {
    const parsed = new URL(raw, location.href);
    if (/^javascript:|^data:/i.test(parsed.protocol)) {
      return "";
    }
    parsed.hash = "";
    return parsed.toString();
  } catch (_) {
    return "";
  }
}

function detectType(url) {
  const lower = String(url || "").toLowerCase();
  if (lower.includes(".m3u8")) return "hls";
  if (lower.includes(".mpd")) return "dash";
  if (lower.startsWith("blob:")) return "blob";
  return "direct";
}

function setupScorer() {
  if (typeof DownloadScorer !== "function") {
    scorer = null;
    return;
  }
  if (!scorer) {
    scorer = new DownloadScorer({
      defaultThreshold: Number(featureFlags.downloadScoreThreshold || 60),
      debug: Boolean(featureFlags.scorerDebugMode),
      maxConcurrentHead: 3,
      headTimeoutMs: 2000,
      cacheTtlMs: 60000,
    });
  }
  scorer.defaultThreshold = Number(featureFlags.downloadScoreThreshold || 60);
  scorer.setDebug(Boolean(featureFlags.scorerDebugMode));
}

// ─────────────────────────────────────────────────────────────────
// Page Context Detection — identify file hosting pages
// ─────────────────────────────────────────────────────────────────

function extractPageFilename() {
  // Look for filename in page title, headings, or dedicated filename element
  const selectors = ['h1', 'h2', '.filename', '.title', '[class*="file-name"]', 'title'];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el && /\.(mkv|mp4|avi|zip|rar|exe|pdf|mp3|mov|flv|wmv|webm|m4a)/i.test(el.textContent)) {
      const match = el.textContent.match(/[\w\.\-\[\]()]+\.(mkv|mp4|avi|zip|rar|exe|pdf|mp3|mov|flv|wmv|webm|m4a)/i);
      if (match) return match[0];
    }
  }
  return null;
}

function detectPageContext() {
  const bodyText = document.body?.innerText || "";

  const hasGB = /\d+(\.\d+)?\s*GB/i.test(bodyText);
  const hasMB = /\d+(\.\d+)?\s*MB/i.test(bodyText);
  const hasVideoType = /video\/|audio\//i.test(bodyText);
  const hasMediaExt = /\.(mkv|mp4|avi|mov|flac|mp3|zip|rar|exe)/i.test(bodyText);
  const hasDownloadText = /download link|download now|download \[/i.test(bodyText);

  const isFileHostingPage = (hasGB || hasMB) && (hasVideoType || hasMediaExt || hasDownloadText);

  let pageFilename = null;
  const headings = document.querySelectorAll("h1, h2, h3, .title, [class*='filename']");
  for (const el of headings) {
    const text = String(el.textContent || "");
    const match = text.match(/[\w\.\-\[\]()]+\.(mkv|mp4|avi|zip|rar|exe|pdf|mp3|flac)/i);
    if (match) {
      pageFilename = match[0];
      break;
    }
  }

  if (!pageFilename) {
    const allText = document.querySelectorAll("*");
    for (const el of allText) {
      if (el.children.length !== 0) {
        continue;
      }
      const text = String(el.textContent || "");
      const match = text.match(/[\w\.\-]+\.(mkv|mp4|avi|zip|rar|exe|pdf|mp3)/i);
      if (match) {
        pageFilename = match[0];
        break;
      }
    }
  }

  const hasMultipleDownloadButtons =
    document.querySelectorAll("a").length > 0 &&
    [...document.querySelectorAll("a")].filter((a) => /download|dl\.|mirror|server/i.test(a.textContent || "")).length >= 2;

  return {
    isFileHostingPage,
    hasMultipleServers: hasMultipleDownloadButtons,
    pageFilename: pageFilename || extractPageFilename(),
  };
}

function scoreLinkFromDOM(linkElement) {
  if (!(linkElement instanceof HTMLAnchorElement)) {
    return 0;
  }

  let score = 0;
  const href = String(linkElement.href || "");
  const text = String(linkElement.textContent || "").trim();
  const title = String(linkElement.getAttribute("title") || "").trim();

  let url;
  try {
    url = new URL(href, location.href);
  } catch (_) {
    return 0;
  }

  if (/^download/i.test(text)) score += 50;
  if (/download\s*[\[(].+[\])]/i.test(text)) score += 35;

  const fileExtensions = /\.(zip|rar|7z|tar|gz|exe|msi|dmg|apk|iso|pdf|mp4|mp3|mkv|avi|mov|flac|wav|epub|deb|rpm|pkg|bin|img|torrent)(\?|$)/i;
  if (fileExtensions.test(url.pathname)) score += 40;

  if (/\/(dl|download|get|file|files|serve|release|dist|cdn|media|drive)\b/i.test(url.pathname)) score += 20;
  if (linkElement.hasAttribute("download")) score += 30;

  const rect = linkElement.getBoundingClientRect();
  if (rect.width > 150 && rect.height > 35) score += 15;

  if (linkElement.querySelector("img, svg, i, span[class*='icon'], span[class*='download']")) score += 10;

  if (PAGE_CONTEXT.isFileHostingPage) score += 40;

  const fileHostPattern = /^(github\.com|sourceforge\.net|mediafire\.com|mega\.nz|drive\.google\.com|dropbox\.com|hubcloud\.|fsl\.|pixeldrain\.)/i;
  if (fileHostPattern.test(url.hostname)) score += 20;

  if (title && /download|server|mirror|get/i.test(title)) score += 10;

  if (linkElement.closest("nav, header, footer, .navbar, .menu, .sidebar, .breadcrumb, .pagination")) score -= 50;

  if (/^(home|about|contact|login|signup|register|blog|news|help|faq|terms|privacy|back|next|previous|more|read more|see all|view all)$/i.test(text)) {
    score -= 40;
  }

  if (href.startsWith("#") || (url.pathname === window.location.pathname && !url.search)) score -= 50;
  if (/\/(login|logout|signin|oauth|auth|share|intent)\b/i.test(url.pathname)) score -= 40;

  return Math.max(0, score);
}

// Detect redirect handler URLs that legitimately serve files
function isRedirectHandlerURL(url) {
  try {
    const parsed = new URL(url);
    const scriptPattern = /\.(php|asp|aspx|jsp|cgi)$/i;
    const downloadPathPattern = /\/(dl|download|get|file|serve|stream|redirect)\b/i;
    return scriptPattern.test(parsed.pathname) ||
           downloadPathPattern.test(parsed.pathname);
  } catch (_) {
    return false;
  }
}

function scheduleIdle(task, timeout = 1200) {
  if (typeof requestIdleCallback === "function") {
    requestIdleCallback(() => task(), { timeout });
    return;
  }
  setTimeout(task, 0);
}

function shouldShowForScore(result, url = "") {
  const threshold = Number(featureFlags.downloadScoreThreshold || 60);
  const score = Number(result?.score || 0);
  if (!Number.isFinite(score) || score < 0) {
    return false;
  }
  if (String(url).startsWith("blob:")) {
    return Boolean(featureFlags.videoStreamDetection) && Boolean(featureFlags.showUncertainLinks) && score >= 40;
  }
  if (score >= threshold) {
    return true;
  }
  return Boolean(featureFlags.showUncertainLinks) && score >= 40 && score <= 59;
}

function maybeScoreTooltip(result) {
  if (!featureFlags.scorerDebugMode || !result) {
    return "";
  }
  return `Score: ${Number(result.score || 0)}/100`;
}

function scoreLinkSync(link) {
  if (!scorer) {
    return { score: 0, verdict: "unknown", signals: [] };
  }
  return scorer.scoreSync(link, { pageUrl: location.href });
}

async function scoreLinkAsync(link) {
  if (!scorer) {
    return { score: 0, verdict: "unknown", signals: [] };
  }
  return scorer.score(link, { pageUrl: location.href });
}

function reportStream(url, details = {}) {
  if (!featureFlags.videoStreamDetection) return;
  const normalized = normalizeAbsolute(url);
  if (!normalized) return;

  safeSendMessage({
    type: "streamDetected",
    stream: {
      url: normalized,
      type: details.type || detectType(normalized),
      referer: location.href,
      title: details.title || document.title || "",
      cookies: "",
    },
  });
}

function markBrowserPreferredDownloadUrl(url, ttlMs = 30000) {
  const normalized = normalizeAbsolute(url);
  if (!normalized) {
    return;
  }
  idmClickBypassUrls.set(normalized, Date.now() + Math.max(1000, Number(ttlMs) || 30000));
  safeSendMessage({
    type: "markBrowserPreferredUrl",
    url: normalized,
    ttlMs,
  });
}

function isTemporarilyBrowserPreferred(url) {
  const normalized = normalizeAbsolute(url);
  if (!normalized) {
    return false;
  }
  const until = Number(idmClickBypassUrls.get(normalized) || 0);
  if (!until) {
    return false;
  }
  if (until < Date.now()) {
    idmClickBypassUrls.delete(normalized);
    return false;
  }
  return true;
}

function injectNetworkHooks() {
  if (document.getElementById("idm-network-hooks-script")) {
    return;
  }

  const script = document.createElement("script");
  script.id = "idm-network-hooks-script";
  script.src = chrome.runtime.getURL("content/injected_network_hooks.js");
  script.async = false;
  script.onload = () => {
    script.remove();
  };
  script.onerror = () => {
    // CSP may still block external injection on some origins; fail silently.
    script.remove();
  };

  (document.documentElement || document.body).appendChild(script);
}

function bootstrapStreamListeners() {
  window.addEventListener("message", (event) => {
    if (event.source !== window || !event.data || !event.data.__idmBridge) return;
    const payload = event.data.payload || {};
    if (payload.type === "stream" && payload.url) {
      reportStream(payload.url, {
        type: payload.streamType || detectType(payload.url),
        title: payload.title || document.title,
      });
      if (String(payload.url).startsWith("blob:")) {
        reportStream(payload.url, {
          type: "blob",
          title: `${document.title || "Video"} (stream detected, quality may vary)`,
        });
      }
    }
  });
}

function showAddedTooltip(anchorElement, message = "Added to IDM ✓") {
  const tip = document.createElement("span");
  tip.textContent = message;
  tip.style.position = "absolute";
  tip.style.left = "0";
  tip.style.top = "-22px";
  tip.style.fontSize = "11px";
  tip.style.padding = "2px 6px";
  tip.style.borderRadius = "6px";
  tip.style.background = "#1e293b";
  tip.style.color = "#fff";
  tip.style.whiteSpace = "nowrap";
  tip.style.zIndex = "999999";

  anchorElement.style.position = anchorElement.style.position || "relative";
  anchorElement.appendChild(tip);
  setTimeout(() => tip.remove(), 1500);
}

function defaultBadgeLabelForState(state) {
  return state?.resolveStatus === "unverified" ? "+ IDM ?" : "+ IDM";
}

function setButtonBadgeText(state, label) {
  const badge = state?.button?.querySelector?.("[data-idm-badge]");
  if (badge) {
    badge.textContent = label;
  }
}

function setButtonBadgeStyle(state, mode) {
  if (!state?.button) {
    return;
  }
  if (mode === "pending") {
    state.button.style.background = "#64748b";
    state.button.style.cursor = "progress";
    return;
  }
  if (mode === "unverified") {
    state.button.style.background = "#f59e0b";
    state.button.style.cursor = "pointer";
    return;
  }
  state.button.style.background = "#2ecc71";
  state.button.style.cursor = "pointer";
}

function getResolvedTargetForLink(link, fallbackUrl = "") {
  const state = (link && typeof link === "object") ? linkState.get(link) : null;
  const resolved = state?.resolved;
  if (!resolved) {
    return { url: normalizeAbsolute(fallbackUrl || link?.href || ""), filename: "" };
  }
  const targetUrl = normalizeAbsolute(resolved.finalUrl || fallbackUrl || link?.href || "");
  return {
    url: targetUrl,
    filename: String(resolved.filename || ""),
  };
}

function applyResolveDecision(link, resolveData) {
  if (!(link instanceof HTMLAnchorElement)) {
    return;
  }
  const state = getOrCreateLinkState(link);
  state.resolving = false;
  state.resolved = resolveData || null;

  // Get the original DOM score before any resolution
  const domScore = scorer ? scorer.scoreSync(link, { pageUrl: location.href }) : { score: 0 };
  const finalDomScore = Number(domScore?.score || 0) + BASE_SCORE_BONUS;

  // If HEAD request failed or returned no data, trust DOM score
  if (!resolveData) {
    if (finalDomScore >= 60) {
      // Keep badge — DOM scored it high
      state.resolveStatus = "unverified";
      setButtonBadgeStyle(state, "unverified");
      setButtonBadgeText(state, "+ IDM ?");
      return;
    }
    state.resolveStatus = "rejected";
    removeInlineButton(link);
    return;
  }

  const isHtml = Boolean(resolveData.isHtmlPage);
  const isBinary = Boolean(resolveData.isBinary);
  const verified = Boolean(resolveData.verified);
  const hasHardError = Boolean(resolveData.error);
  const corsBlocked = Boolean(resolveData.corsBlocked);

  // If HEAD returned HTML but it's a known redirect handler URL with high DOM score — keep badge
  if (isHtml && !verified && isRedirectHandlerURL(state.url)) {
    if (finalDomScore >= 60 || PAGE_CONTEXT.isFileHostingPage) {
      state.resolveStatus = "unverified";
      setButtonBadgeStyle(state, "unverified");
      setButtonBadgeText(state, "+ IDM ?");
      return;
    }
  }

  // On file hosting pages, always trust DOM score
  if (PAGE_CONTEXT.isFileHostingPage && finalDomScore >= 60) {
    if (isHtml) {
      // HTML response on file hosting page with high DOM score — treat as unverified
      state.resolveStatus = "unverified";
      setButtonBadgeStyle(state, "unverified");
      setButtonBadgeText(state, "+ IDM ?");
      return;
    }
  }

  // CORS blocked or hard error — check DOM score
  if (corsBlocked || hasHardError) {
    if (finalDomScore >= 60) {
      state.resolveStatus = "unverified";
      setButtonBadgeStyle(state, "unverified");
      setButtonBadgeText(state, "+ IDM ?");
      return;
    }
    state.resolveStatus = "rejected";
    removeInlineButton(link);
    return;
  }

  // Regular verification logic
  if (isHtml || (verified && !isBinary)) {
    state.resolveStatus = "rejected";
    removeInlineButton(link);
    return;
  }

  if (verified && isBinary) {
    state.resolveStatus = "verified";
    setButtonBadgeStyle(state, "verified");
    setButtonBadgeText(state, "+ IDM");
    return;
  }

  state.resolveStatus = "unverified";
  setButtonBadgeStyle(state, "unverified");
  setButtonBadgeText(state, "+ IDM ?");
}

function requestResolve(link, href) {
  if (!(link instanceof HTMLElement)) {
    return;
  }
  const state = getOrCreateLinkState(link);
  if (!state.button) {
    return;
  }

  const cached = getHeadCacheResult(href);
  if (cached) {
    applyResolveDecision(link, cached);
    return;
  }

  if (state.resolving) {
    return;
  }

  state.resolving = true;
  state.resolveStatus = "pending";
  setButtonBadgeStyle(state, "pending");
  setButtonBadgeText(state, "...");

  safeSendMessage({
    type: "RESOLVE_URL",
    url: href,
    referer: location.href,
    cookies: document.cookie || "",
  }, (response) => {
    const payload = response?.ok ? (response.data || null) : null;
    setHeadCacheResult(href, payload || { error: true, corsBlocked: true });
    applyResolveDecision(link, payload || { error: true, corsBlocked: true });
  });
}

function createInlineButton(targetUrl, scoreResult = null, mode = "inline") {
  const button = document.createElement("button");
  button.type = "button";
  button.setAttribute("data-idm-inline-button", "1");
  button.setAttribute("aria-label", "Download with IDM");
  button.style.display = "inline-flex";
  button.style.alignItems = "center";
  button.style.gap = "4px";
  button.style.marginLeft = mode === "overlay" ? "0" : "6px";
  button.style.padding = mode === "overlay" ? "2px 8px" : "2px 6px";
  button.style.fontSize = "11px";
  button.style.fontWeight = "600";
  button.style.lineHeight = "1";
  button.style.border = "0";
  button.style.borderRadius = mode === "overlay" ? "10px" : "999px";
  button.style.background = "#2ecc71";
  button.style.color = "#fff";
  button.style.cursor = "pointer";
  button.style.verticalAlign = "middle";
  button.style.whiteSpace = "nowrap";
  button.style.transformOrigin = "center";
  button.style.transition = "transform 120ms ease, background-color 120ms ease";

  button.innerHTML = `
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M12 4V15" stroke="white" stroke-width="2" stroke-linecap="round"/>
      <path d="M7.5 10.5L12 15L16.5 10.5" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M5 19H19" stroke="white" stroke-width="2" stroke-linecap="round"/>
    </svg>
    <span data-idm-badge>+ IDM</span>
  `;
  button.title = maybeScoreTooltip(scoreResult);

  if (mode === "overlay") {
    button.style.position = "absolute";
    button.style.top = "6px";
    button.style.right = "6px";
    button.style.zIndex = "9999";
  }

  button.addEventListener("mouseenter", () => {
    button.style.background = "#27ae60";
    button.style.transform = "scale(1.05)";
  });

  button.addEventListener("mouseleave", () => {
    button.style.background = "#2ecc71";
    button.style.transform = "scale(1)";
  });

  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();

    const linkedElement = button.__idmTargetElement instanceof HTMLElement
      ? button.__idmTargetElement
      : (button.previousElementSibling instanceof HTMLElement ? button.previousElementSibling : null);
    const href = linkedElement ? extractCandidateUrl(linkedElement) : normalizeAbsolute(targetUrl);
    const cached = getHeadCacheResult(href);

    if (!cached || cached.error || cached.corsBlocked || cached.isHtmlPage || !cached.isBinaryFile) {
      if (href) {
        window.location.href = href;
      }
      return;
    }

    const target = getResolvedTargetForLink(linkedElement, cached.finalUrl || href || targetUrl);
    sendToIDM(target.url, target.filename).then((accepted) => {
      if (accepted) {
        const badge = button.querySelector("[data-idm-badge]");
        if (badge) {
          badge.textContent = "✓ IDM";
          setTimeout(() => {
            badge.textContent = "⬇ IDM";
          }, 2000);
        } else {
          showAddedTooltip(button, "Added to IDM ✓");
        }
      } else if (href) {
        window.location.href = href;
      }
    });
  });

  return button;
}

function extractUrlFromOnclick(onclickValue) {
  const raw = String(onclickValue || "");
  if (!raw) {
    return "";
  }

  const absoluteUrlMatch = raw.match(/https?:\/\/[^'"\s)]+/i);
  if (absoluteUrlMatch?.[0]) {
    return normalizeAbsolute(absoluteUrlMatch[0]);
  }

  const quotedPathMatch = raw.match(/["'](\/(?:[^"']+))["']/);
  if (quotedPathMatch?.[1]) {
    return normalizeAbsolute(quotedPathMatch[1]);
  }

  return "";
}

function extractCandidateUrl(linkElement) {
  if (!(linkElement instanceof Element)) {
    return "";
  }

  const directHref = normalizeAbsolute(linkElement.getAttribute("href") || linkElement.href || "");
  if (directHref) {
    return directHref;
  }

  const dataHref = normalizeAbsolute(
    linkElement.getAttribute("data-href") ||
    linkElement.getAttribute("data-url") ||
    linkElement.getAttribute("data-link") ||
    ""
  );
  if (dataHref) {
    return dataHref;
  }

  const fromOnclick = extractUrlFromOnclick(linkElement.getAttribute("onclick") || "");
  if (fromOnclick) {
    return fromOnclick;
  }

  return "";
}

function getOrCreateLinkState(link) {
  const current = linkState.get(link);
  if (current) {
    return current;
  }
  const init = {
    url: "",
    firstPass: null,
    finalResult: null,
    button: null,
    buttonMode: "inline",
    patchedPosition: false,
    originalPosition: "",
    inSecondPass: false,
    pendingPromise: null,
    resolving: false,
    resolveStatus: "idle",
    resolved: null,
  };
  linkState.set(link, init);
  return init;
}

function getButtonStyle(linkElement) {
  // Always use sibling inline badges for link targets.
  // Appending interactive controls inside <a> can be ignored or suppressed by pages/browsers.
  return "inline";
}

function getBadgeHostElement(linkElement) {
  if (!(linkElement instanceof HTMLElement)) {
    return linkElement;
  }
  if (linkElement instanceof HTMLAnchorElement) {
    const innerButton = linkElement.querySelector("button");
    if (innerButton instanceof HTMLElement) {
      return innerButton;
    }
  }
  return linkElement;
}

function ensureOverlayAnchorPosition(link, state) {
  if (!(link instanceof HTMLElement) || state.patchedPosition) {
    return;
  }
  const current = getComputedStyle(link).position;
  if (current === "static" || !current) {
    state.originalPosition = link.style.position || "";
    link.style.position = "relative";
    state.patchedPosition = true;
  }
}

function removeInlineButton(link) {
  const state = getOrCreateLinkState(link);
  if (!state?.button) {
    return;
  }
  state.button.remove();
  state.button = null;
  state.buttonMode = "inline";
  if (state.patchedPosition && link instanceof HTMLElement) {
    link.style.position = state.originalPosition;
    state.patchedPosition = false;
    state.originalPosition = "";
  }
}

function countInjectedBadges() {
  return document.querySelectorAll("[data-idm-inline-button='1']").length;
}

function injectIDMBadge(linkElement, domScore = 0) {
  if (!(linkElement instanceof HTMLElement)) {
    return false;
  }
  if (!featureFlags.showInlineIdmButtons) {
    return false;
  }
  if (countInjectedBadges() >= MAX_BADGES_PER_PAGE) {
    return false;
  }

  const href = extractCandidateUrl(linkElement);
  if (!href) {
    return false;
  }

  const scoreResult = {
    score: Number(domScore || 0),
    verdict: Number(domScore || 0) >= Number(featureFlags.downloadScoreThreshold || 60) ? "download" : "unknown",
    signals: ["dom-only"],
  };

  upsertInlineButton(linkElement, href, scoreResult);
  return true;
}

function removeBadge(linkElement) {
  removeInlineButton(linkElement);
}

function setHeadCacheResult(href, data) {
  if (!href) {
    return;
  }
  headCache.set(href, {
    data: data || null,
    expiresAt: Date.now() + HEAD_CACHE_TTL_MS,
  });
}

function getHeadCacheResult(href) {
  if (!href) {
    return null;
  }
  const cached = headCache.get(href);
  if (!cached) {
    return null;
  }
  if (cached.expiresAt <= Date.now()) {
    headCache.delete(href);
    return null;
  }
  return cached.data || null;
}

function updateBadgeAppearance(linkElement, result) {
  if (!(linkElement instanceof HTMLElement)) {
    return;
  }
  const state = linkState.get(linkElement);
  const button = state?.button || null;
  const badge = button?.querySelector?.("[data-idm-badge]") || null;
  if (!button || !badge) {
    return;
  }

  if (!result || result.error || result.corsBlocked) {
    button.style.background = "#7f8c8d";
    badge.textContent = "? IDM";
    button.title = "Unknown - will navigate to page";
    return;
  }

  if (result.isHtmlPage) {
    button.style.background = "#f39c12";
    badge.textContent = "↗ IDM";
    button.title = "Goes to download page - IDM captures there";
    return;
  }

  if (result.isBinaryFile) {
    button.style.background = "#2ecc71";
    badge.textContent = "⬇ IDM";
    button.title = "Direct download - click to send to IDM";
    return;
  }

  button.style.background = "#7f8c8d";
  badge.textContent = "? IDM";
  button.title = "Unknown - will navigate to page";
}

function resolveUrlWithCache(href) {
  return new Promise((resolve) => {
    const cached = getHeadCacheResult(href);
    if (cached) {
      resolve(cached);
      return;
    }

    safeSendMessage({
      type: "RESOLVE_URL",
      url: href,
      referer: location.href,
      cookies: document.cookie || "",
    }, (response) => {
      const payload = response?.ok ? (response.data || null) : null;
      setHeadCacheResult(href, payload || { error: true, corsBlocked: true });
      resolve(payload);
    });
  });
}

async function verifyWithHEAD(linkElement, domScore) {
  if (!(linkElement instanceof HTMLElement)) {
    return;
  }

  try {
    const href = extractCandidateUrl(linkElement);
    if (!href) {
      return;
    }

    const result = await resolveUrlWithCache(href);
    updateBadgeAppearance(linkElement, result);
  } catch (_) {
    updateBadgeAppearance(linkElement, { error: true, corsBlocked: true });
  }
}

async function preCacheAndUpdateBadge(linkElement) {
  if (!(linkElement instanceof HTMLElement)) {
    return;
  }
  const href = extractCandidateUrl(linkElement);
  if (!href) {
    return;
  }
  const cached = getHeadCacheResult(href);
  if (cached) {
    updateBadgeAppearance(linkElement, cached);
    return;
  }
  const result = await resolveUrlWithCache(href);
  updateBadgeAppearance(linkElement, result);
}

function processLink(linkElement) {
  if (!(linkElement instanceof HTMLElement)) {
    return;
  }

  if (resolvedLinks.has(linkElement)) return;

  const href = extractCandidateUrl(linkElement);
  const tag = String(linkElement.tagName || "").toLowerCase();
  const text = String(linkElement.textContent || "").trim();
  if (!href && !/^download/i.test(text)) {
    return;
  }

  if (href.startsWith("#")) return;
  if (href.startsWith("javascript:")) return;
  if (href.startsWith("mailto:")) return;
  if (tag === "input") return;

  const domScore = scoreLinkFromDOM(linkElement);
  const threshold = Number(featureFlags.downloadScoreThreshold || 60);

  const state = getOrCreateLinkState(linkElement);
  state.url = normalizeAbsolute(href);
  state.firstPass = { score: domScore, verdict: domScore >= threshold ? "download" : "unknown", signals: ["dom-only"] };
  state.finalResult = state.firstPass;

  if (domScore >= threshold) {
    const injected = injectIDMBadge(linkElement, domScore);
    if (injected) {
      resolvedLinks.add(linkElement);
      void preCacheAndUpdateBadge(linkElement);
    }
  } else {
    removeBadge(linkElement);
  }
}

function upsertInlineButton(link, href, scoreResult) {
  const state = getOrCreateLinkState(link);
  const mode = getButtonStyle(link);
  const badgeHost = getBadgeHostElement(link);

  if (state.button && state.button.isConnected && state.buttonMode !== mode) {
    state.button.remove();
    state.button = null;
  }

  if (!state.button || !state.button.isConnected) {
    state.button = createInlineButton(href, scoreResult, mode);
    state.button.__idmTargetElement = link;
    state.buttonMode = mode;
    if (mode === "overlay") {
      ensureOverlayAnchorPosition(link, state);
      link.appendChild(state.button);
    } else {
      badgeHost.insertAdjacentElement("afterend", state.button);
    }
  }
  state.button.title = maybeScoreTooltip(scoreResult);
}

function evaluateLinkFirstPass(link) {
  if (!(link instanceof HTMLAnchorElement)) return null;
  
  // Guard: skip if this link has already been resolved and had a badge injected
  if (resolvedLinks.has(link)) {
    return null;
  }
  
  const href = normalizeAbsolute(link.getAttribute("href") || link.href || "");
  const state = getOrCreateLinkState(link);
  state.url = href;
  if (!href) {
    removeInlineButton(link);
    return null;
  }

  const result = scoreLinkSync(link);
  
  // Apply bonus score on file hosting pages
  const boostedScore = result.score + BASE_SCORE_BONUS;
  const boostedResult = { ...result, score: boostedScore };
  
  state.firstPass = result;
  state.finalResult = boostedResult;

  if (!featureFlags.showInlineIdmButtons) {
    removeInlineButton(link);
    return result;
  }

  if (boostedResult.score > 75 && shouldShowForScore(boostedResult, href)) {
    resolvedLinks.add(link);  // Mark as processed
    upsertInlineButton(link, href, boostedResult);
  } else {
    removeInlineButton(link);
  }

  if (result.score >= 30 && result.score <= 75) {
    secondPassQueue.add(link);
    scheduleSecondPass();
  }
  return result;
}

function scheduleSecondPass() {
  if (secondPassScheduled) {
    return;
  }
  secondPassScheduled = true;
  scheduleIdle(async () => {
    secondPassScheduled = false;
    const batch = Array.from(secondPassQueue).slice(0, 40);
    batch.forEach((link) => secondPassQueue.delete(link));
    await Promise.all(batch.map(async (link) => {
      if (!(link instanceof HTMLAnchorElement) || !link.isConnected) return;
      
      // Skip if already resolved
      if (resolvedLinks.has(link)) return;
      
      const state = getOrCreateLinkState(link);
      if (state.inSecondPass) return;
      const firstPassScore = Number(state.firstPass?.score || 0);
      if (firstPassScore < 30 || firstPassScore > 75) return;
      state.inSecondPass = true;
      try {
        const finalResult = await scoreLinkAsync(link);
        const boostedScore = finalResult.score + BASE_SCORE_BONUS;
        const boostedResult = { ...finalResult, score: boostedScore };
        state.finalResult = boostedResult;
        const href = state.url || normalizeAbsolute(link.getAttribute("href") || link.href || "");
        if (!href) {
          removeInlineButton(link);
          return;
        }
        if (featureFlags.showInlineIdmButtons && shouldShowForScore(boostedResult, href)) {
          resolvedLinks.add(link);  // Mark as processed
          upsertInlineButton(link, href, boostedResult);
        } else {
          removeInlineButton(link);
        }
      } catch (_) {
        // Ignore second-pass scoring errors and keep first-pass result.
      } finally {
        state.inSecondPass = false;
      }
    }));
    if (secondPassQueue.size > 0) {
      scheduleSecondPass();
    }
  }, 1200);
}

function runFirstPassForRoot(rootNode = document) {
  if (!rootNode?.querySelectorAll) {
    return;
  }
  const links = rootNode.querySelectorAll("a, button, [role='button']");
  for (const link of links) {
    processLink(link);
  }
}

function scanAllLinks() {
  runFirstPassForRoot(document);
}

function injectMediaOverlayButtons(rootNode = document) {
  if (!featureFlags.showInlineIdmButtons) {
    return;
  }

  const mediaElements = rootNode.querySelectorAll ? rootNode.querySelectorAll("video[src], audio[src]") : [];
  for (const media of mediaElements) {
    if (!(media instanceof HTMLElement)) continue;
    if (media.hasAttribute(IDM_MEDIA_MARK)) continue;

    const src = normalizeAbsolute(media.getAttribute("src") || media.src || "");
    if (!src) continue;

    const result = scorer
      ? scorer.scoreSync({
        url: src,
        text: media.getAttribute("title") || document.title,
        title: media.getAttribute("title") || "",
      }, { pageUrl: location.href })
      : { score: 0 };

    const shouldShow = STREAM_URL_RE.test(src) || shouldShowForScore(result, src);
    if (!shouldShow) continue;

    const wrapper = document.createElement("span");
    wrapper.style.position = "relative";
    wrapper.style.display = "inline-block";

    media.parentNode?.insertBefore(wrapper, media);
    wrapper.appendChild(media);

    const btn = createInlineButton(src, result);
    btn.style.position = "absolute";
    btn.style.right = "8px";
    btn.style.bottom = "8px";
    btn.style.marginLeft = "0";
    btn.style.zIndex = "999999";

    wrapper.appendChild(btn);
    media.setAttribute(IDM_MEDIA_MARK, "1");
  }
}

function observeDynamicContent() {
  let debounceTimer = null;
  let pendingNodes = [];

  const flush = () => {
    const nodes = pendingNodes;
    pendingNodes = [];
    for (const node of nodes) {
      if (!(node instanceof Element)) continue;
      runFirstPassForRoot(node);
      injectMediaOverlayButtons(node);
      if (node.matches("video, audio, source")) {
        const src = normalizeAbsolute(node.getAttribute("src") || node.src || "");
        if (src && (STREAM_URL_RE.test(src) || src.startsWith("blob:"))) {
          reportStream(src, { type: detectType(src), title: document.title });
        }
      }
      node.querySelectorAll?.("video, audio, source").forEach((el) => {
        const src = normalizeAbsolute(el.getAttribute("src") || el.src || "");
        if (src && (STREAM_URL_RE.test(src) || src.startsWith("blob:"))) {
          reportStream(src, { type: detectType(src), title: document.title });
        }
      });
    }
  };

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      pendingNodes.push(...Array.from(mutation.addedNodes));
    }
    if (debounceTimer) {
      clearTimeout(debounceTimer);
    }
    debounceTimer = setTimeout(flush, 120);
  });

  observer.observe(document.documentElement || document.body, {
    childList: true,
    subtree: true,
  });
}

function bytesFromResourceTiming(src) {
  const abs = normalizeAbsolute(src);
  if (!abs || !performance?.getEntriesByName) return 0;
  const entries = performance.getEntriesByName(abs);
  if (!entries?.length) return 0;
  const last = entries[entries.length - 1];
  return Number(last.transferSize || last.encodedBodySize || 0);
}

function collectBatchCandidates() {
  const map = new Map();
  const push = (item) => {
    const url = normalizeAbsolute(item.url || "");
    if (!url || map.has(url)) return;
    map.set(url, {
      url,
      type: item.type || "link",
      title: item.title || "",
      size: Number(item.size || 0),
    });
  };

  document.querySelectorAll("a[href]").forEach((a) => {
    const hrefRaw = String(a.getAttribute("href") || a.href || "").trim();
    if (!hrefRaw || hrefRaw.startsWith("#") || /^javascript:/i.test(hrefRaw)) return;
    const url = normalizeAbsolute(hrefRaw);
    if (!url) return;
    const result = scorer
      ? scorer.scoreSync(a, { pageUrl: location.href })
      : { score: 0 };
    if (!shouldShowForScore(result, url)) return;
    push({ url, type: "link", title: (a.textContent || "").trim() || a.getAttribute("title") || "" });
  });

  document.querySelectorAll("video[src], audio[src], source[src]").forEach((media) => {
    const src = normalizeAbsolute(media.getAttribute("src") || media.src || "");
    if (!src) return;
    push({ url: src, type: "media", title: media.getAttribute("title") || document.title });
  });

  const minBytes = Math.max(0, Number(featureFlags.minFileSizeKb || 0)) * 1024;
  document.querySelectorAll("img[src]").forEach((img) => {
    const src = normalizeAbsolute(img.getAttribute("src") || img.src || "");
    if (!src) return;
    const bytes = bytesFromResourceTiming(src);
    if (minBytes > 0 && bytes > 0 && bytes < minBytes) return;
    push({ url: src, type: "image", title: img.getAttribute("alt") || "", size: bytes });
  });

  return Array.from(map.values());
}

function getCachedScoreForLink(linkElement) {
  if (!(linkElement instanceof HTMLAnchorElement)) {
    return null;
  }
  const state = linkState.get(linkElement);
  if (state?.finalResult && Number.isFinite(Number(state.finalResult.score))) {
    return state.finalResult;
  }
  if (state?.firstPass && Number.isFinite(Number(state.firstPass.score))) {
    return state.firstPass;
  }
  return null;
}

function showButtonFeedback(link, message) {
  if (!(link instanceof HTMLAnchorElement)) {
    return;
  }

  let badge = link.querySelector("[data-idm-badge]");
  if (!badge) {
    const state = linkState.get(link);
    badge = state?.button?.querySelector?.("[data-idm-badge]") || null;
  }
  if (!badge) {
    return;
  }

  badge.textContent = message;
  setTimeout(() => {
    const state = getOrCreateLinkState(link);
    badge.textContent = defaultBadgeLabelForState(state);
  }, 2000);
}

async function sendToIDM(url, filename = "") {
  const normalized = normalizeAbsolute(url);
  if (!normalized) {
    return false;
  }

  safeSendMessage({ type: "IDM_CLICK_CAPTURED", url: normalized });

  // Use filename_hint from page context if available and filename is missing
  const filenameToSend = filename || PAGE_CONTEXT.pageFilename || "";

  const request = new Promise((resolve) => {
    safeSendMessage({
      type: "addDownload",
      url: normalized,
      filename: filenameToSend,
      filename_hint: PAGE_CONTEXT.pageFilename || undefined,
      referer: location.href,
      cookies: document.cookie || "",
      category: "Auto",
    }, (response) => {
      resolve(Boolean(response?.ok && (response?.data?.download_id || response?.data?.data?.download_id || response?.data?.downloadId)));
    });
  });

  const timeout = new Promise((resolve) => {
    setTimeout(() => resolve(false), 3000);
  });

  return Promise.race([request, timeout]);
}

async function loadFeatureFlags() {
  return new Promise((resolve) => {
    safeSendMessage({ type: "getFeatureFlags" }, (resp) => {
      featureFlags = {
        ...featureFlags,
        ...(resp?.flags || {}),
      };
      setupScorer();
      resolve(featureFlags);
    });
  });
}

async function loadSettings() {
  return new Promise((resolve) => {
    try {
      chrome.storage.sync.get("settings", (stored) => {
        const capture = {
          ...DEFAULT_SETTINGS.capture,
          ...(stored?.settings?.capture || {}),
        };

        const settings = {
          ...DEFAULT_SETTINGS,
          ...(stored?.settings || {}),
          capture,
        };

        if (settings.capture.inline_buttons === undefined || settings.capture.inline_buttons === null) {
          settings.capture.inline_buttons = true;
        }
        resolve(settings);
      });
    } catch (_) {
      resolve({ ...DEFAULT_SETTINGS, capture: { ...DEFAULT_SETTINGS.capture } });
    }
  });
}

async function applyRuntimeSettings() {
  const settings = await loadSettings();
  featureFlags.showInlineIdmButtons = settings.capture.inline_buttons !== false;
  featureFlags.downloadScoreThreshold = Number(settings.threshold || 60);
  MAX_BADGES_PER_PAGE = Math.max(1, Number(settings.maxBadgesPerPage || DEFAULT_SETTINGS.maxBadgesPerPage));
}

document.addEventListener("keydown", (event) => {
  if (event.altKey && event.shiftKey && (event.key === "D" || event.key === "d")) {
    const items = collectBatchCandidates();
    safeSendMessage({ type: "batchScanResult", items });
  }
}, true);

document.addEventListener("click", async (event) => {
  const rawTarget = event.target;
  if (rawTarget instanceof Element && rawTarget.closest("[data-idm-inline-button]")) {
    return;
  }

  if (event.button !== 0 || event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) {
    return;
  }

  const link = rawTarget instanceof Element ? rawTarget.closest("a[href]") : null;
  if (!(link instanceof HTMLAnchorElement) || !link.href) {
    return;
  }

  if (event.shiftKey || event.ctrlKey || event.metaKey) {
    return;
  }

  if (!link.href.startsWith("http")) {
    return;
  }

  const clickedButton = rawTarget instanceof Element ? rawTarget.closest("button") : null;
  const state = linkState.get(link) || (clickedButton instanceof HTMLElement ? linkState.get(clickedButton) : null);
  const hasBadge = Boolean(state?.button?.querySelector?.("[data-idm-badge]"));
  if (!hasBadge) {
    return;
  }

  const href = normalizeAbsolute(link.href || "");
  if (!href || isTemporarilyBrowserPreferred(href)) {
    return;
  }

  let cached = getHeadCacheResult(href);
  if (!cached) {
    try {
      const result = await Promise.race([
        resolveUrlWithCache(href),
        new Promise((resolve) => setTimeout(() => resolve({ timeout: true }), 2000)),
      ]);
      cached = result || null;
      setHeadCacheResult(href, cached || { timeout: true, error: true, corsBlocked: true });
      updateBadgeAppearance(link, cached || { timeout: true, error: true, corsBlocked: true });
    } catch (_) {
      return;
    }
  }

  if (!cached || cached.timeout || cached.error || cached.corsBlocked || cached.isHtmlPage || !cached.isBinaryFile) {
    return;
  }

  event.preventDefault();
  event.stopPropagation();

  showButtonFeedback(link, "sending...");

  const target = getResolvedTargetForLink(link, cached.finalUrl || href);
  const accepted = await sendToIDM(target.url, target.filename);
  if (accepted) {
    showButtonFeedback(link, "✓ IDM");
    return;
  }

  showButtonFeedback(link, "opening in browser...");
  markBrowserPreferredDownloadUrl(href, 10000);
  window.location.href = href;
}, true);

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const type = String(message?.type || "");
  if (type === "scanPageForBatch") {
    sendResponse({ ok: true, items: collectBatchCandidates() });
    return true;
  }
  if (type === "refreshFeatureFlags") {
    Promise.all([loadFeatureFlags(), applyRuntimeSettings()]).then(() => {
      runFirstPassForRoot(document);
      injectMediaOverlayButtons(document);
      sendResponse({ ok: true });
    }).catch(() => sendResponse({ ok: false }));
    return true;
  }
  return undefined;
});

(async function init() {
  injectNetworkHooks();
  bootstrapStreamListeners();
  await Promise.all([loadFeatureFlags(), applyRuntimeSettings()]);

  function initIDMScanner() {
    PAGE_CONTEXT = detectPageContext();
    BASE_SCORE_BONUS = PAGE_CONTEXT.isFileHostingPage ? 40 : 0;
    scanAllLinks();
    injectMediaOverlayButtons(document);
    observeDynamicContent();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initIDMScanner, { once: true });
  } else {
    initIDMScanner();
  }

  window.addEventListener("load", () => {
    PAGE_CONTEXT = detectPageContext();
    BASE_SCORE_BONUS = PAGE_CONTEXT.isFileHostingPage ? 40 : 0;
    scanAllLinks();
  });
})();
