function closestAnchor(el) {
  let node = el;
  while (node && node !== document.documentElement) {
    if (node.tagName && node.tagName.toLowerCase() === "a") {
      return node;
    }
    node = node.parentElement;
  }
  return null;
}

const IDM_ICON_CLASS = "idm-bridge-inline-btn";
const IDM_ANCHOR_MARK = "data-idm-bridge-marked";
const IDM_MEDIA_FLOAT_CLASS = "idm-bridge-media-float-btn";

let mediaFloatButton = null;
let mediaFloatTarget = null;
const STREAM_CANDIDATE_DEDUPE_MS = 10000;
const streamCandidateSeenAt = new Map();

function hasDownloadHints(anchor, target) {
  const href = (anchor?.href || "").toLowerCase();
  const text = ((target?.textContent || anchor?.textContent || "") + "").toLowerCase();
  const rel = (anchor?.getAttribute("rel") || "").toLowerCase();
  const cls = (anchor?.className || "").toString().toLowerCase();

  if (anchor?.hasAttribute("download")) {
    return true;
  }

  if (rel.includes("download")) {
    return true;
  }

  if (cls.includes("download")) {
    return true;
  }

  return (
    text.includes("download") ||
    href.includes("download") ||
    href.includes("/dl/") ||
    href.includes("file=") ||
    href.includes("attachment")
  );
}

function hasExplicitDownloadUrlSignal(anchor) {
  const href = (anchor?.href || anchor?.getAttribute("href") || "").toLowerCase();
  const rel = (anchor?.getAttribute("rel") || "").toLowerCase();
  const cls = (anchor?.className || "").toString().toLowerCase();

  if (anchor?.hasAttribute("download")) {
    return true;
  }

  if (rel.includes("download") || cls.includes("download")) {
    return true;
  }

  if (href.includes("/dl/") || href.includes("file=") || href.includes("attachment") || href.includes("download")) {
    return true;
  }

  if (/\.(zip|rar|7z|exe|msi|pdf|docx?|xlsx?|pptx?|mp4|mkv|webm|mp3|m4a|flac|ts|m3u8|avi|mov|flv|wmv|jpg|jpeg|png|webp|gif|bmp)(\?|$)/i.test(href)) {
    return true;
  }

  return false;
}

function isLikelyActionOrGeneratorLink(anchor, eventTarget) {
  const rawHref = anchor?.getAttribute("href") || anchor?.href || "";
  const text = ((eventTarget?.textContent || anchor?.textContent || "") + "").toLowerCase();
  const onclick = (anchor?.getAttribute("onclick") || "").toLowerCase();

  if (!rawHref || rawHref === "#" || /^javascript:/i.test(rawHref.trim())) {
    return true;
  }

  if (text.includes("generate") && text.includes("download")) {
    return true;
  }

  if (onclick.includes("generate") || onclick.includes("downloadlink") || onclick.includes("direct")) {
    return true;
  }

  try {
    const target = new URL(rawHref, window.location.href);
    const current = new URL(window.location.href);
    if (
      target.origin === current.origin &&
      target.pathname === current.pathname &&
      (target.search || "") === (current.search || "")
    ) {
      return true;
    }
  } catch (_) {
    return true;
  }

  return false;
}

function shouldInterceptAnchorClick(anchor, eventTarget) {
  const rawHref = anchor?.href || anchor?.getAttribute("href") || "";
  if (!isSupportedUrl(rawHref)) {
    return false;
  }

  if (!hasDownloadHints(anchor, eventTarget)) {
    return false;
  }

  if (!hasExplicitDownloadUrlSignal(anchor)) {
    return false;
  }

  if (isLikelyActionOrGeneratorLink(anchor, eventTarget)) {
    return false;
  }

  return true;
}

function isSupportedUrl(href) {
  if (!href || typeof href !== "string") {
    return false;
  }

  if (href.startsWith("magnet:")) {
    return true;
  }

  try {
    const parsed = new URL(href, window.location.href);
    return ["http:", "https:", "ftp:"].includes(parsed.protocol);
  } catch (_) {
    return false;
  }
}

function normalizeUrl(raw) {
  if (!raw || typeof raw !== "string") {
    return "";
  }

  const unwrap = (u) => {
    try {
      const parsed = new URL(u, window.location.href);
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
  };

  try {
    return unwrap(new URL(raw, window.location.href).toString());
  } catch (_) {
    return "";
  }
}

function extractWrappedPageUrl(raw) {
  if (!raw || typeof raw !== "string") {
    return "";
  }

  try {
    const parsed = new URL(raw, window.location.href);
    const wrapped =
      parsed.searchParams.get("imgrefurl") ||
      parsed.searchParams.get("refurl") ||
      parsed.searchParams.get("pageurl") ||
      "";
    if (wrapped && /^(https?|ftp):\/\//i.test(wrapped)) {
      return wrapped;
    }
  } catch (_) {
    return "";
  }

  return "";
}

function findSourcePageUrl(anchor, eventTarget) {
  const direct = extractWrappedPageUrl(anchor?.href || anchor?.getAttribute("href") || "");
  if (direct) {
    return direct;
  }

  let node = eventTarget;
  for (let i = 0; node && node !== document.documentElement && i < 6; i += 1) {
    if (node.querySelectorAll) {
      const anchors = node.querySelectorAll("a[href]");
      for (const candidate of anchors) {
        const wrapped = extractWrappedPageUrl(candidate.getAttribute("href") || candidate.href || "");
        if (wrapped) {
          return wrapped;
        }
      }
    }
    node = node.parentElement;
  }

  return window.location.href;
}

function isLikelyStreamUrl(rawUrl) {
  const lower = (rawUrl || "").toLowerCase().trim();
  return (
    lower.includes(".m3u8") ||
    /[?&](format|type|ext|output|container)=m3u8(&|$)/i.test(lower)
  );
}

function extractCandidateUrlsFromString(raw) {
  if (!raw || typeof raw !== "string") {
    return [];
  }

  const found = new Set();
  const push = (candidate) => {
    const url = normalizeUrl(candidate || "");
    if (url) {
      found.add(url);
    }
  };

  push(raw);

  try {
    const decoded = decodeURIComponent(raw);
    if (decoded !== raw) {
      push(decoded);
    }
  } catch (_) {
    // Ignore decode errors.
  }

  try {
    const parsed = new URL(raw, window.location.href);
    const keys = ["url", "src", "file", "playlist", "manifest", "m3u8", "u"];
    for (const key of keys) {
      const value = parsed.searchParams.get(key) || "";
      if (!value) {
        continue;
      }
      push(value);
      try {
        const decodedValue = decodeURIComponent(value);
        if (decodedValue !== value) {
          push(decodedValue);
        }
      } catch (_) {}
    }
  } catch (_) {
    // Raw may not be URL-like; regex fallback below handles that.
  }

  const matches = raw.match(/https?:\/\/[^"'\s<>]+/gi) || [];
  for (const match of matches) {
    push(match);
  }

  return Array.from(found);
}

function shouldSendStreamCandidate(url) {
  const now = Date.now();
  const previous = Number(streamCandidateSeenAt.get(url) || 0);
  if (now - previous < STREAM_CANDIDATE_DEDUPE_MS) {
    return false;
  }
  streamCandidateSeenAt.set(url, now);

  if (streamCandidateSeenAt.size > 300) {
    for (const [key, ts] of streamCandidateSeenAt.entries()) {
      if (now - Number(ts || 0) > STREAM_CANDIDATE_DEDUPE_MS) {
        streamCandidateSeenAt.delete(key);
      }
    }
  }

  return true;
}

let candidateBatch = [];
let candidateBatchTimer = null;

function sendStreamCandidate(url, source = "content") {
  if (!url || !isLikelyStreamUrl(url)) {
    return;
  }
  if (!shouldSendStreamCandidate(url)) {
    return;
  }

  candidateBatch.push({
    url,
    referer: window.location.href,
    source,
    typeHint: "",
  });

  if (!candidateBatchTimer) {
    candidateBatchTimer = setTimeout(() => {
      const candidates = candidateBatch;
      candidateBatch = [];
      candidateBatchTimer = null;
      
      safeRuntimeSendMessage({
        type: "captureStreamCandidates",
        candidates,
      });
    }, 250);
  }
}

function inspectIframeNode(node, source = "iframe") {
  if (!(node instanceof Element)) {
    return;
  }
  if (!node.matches("iframe")) {
    return;
  }

  const rawValues = [
    node.getAttribute("src") || "",
    node.getAttribute("data-src") || "",
    node.getAttribute("data-url") || "",
  ];

  for (const raw of rawValues) {
    const candidates = extractCandidateUrlsFromString(raw);
    for (const candidate of candidates) {
      sendStreamCandidate(candidate, source);
    }
  }
}

function scoreCandidate(url, context = "") {
  const lowerUrl = (url || "").toLowerCase();
  const lowerCtx = (context || "").toLowerCase();
  let score = 0;

  if (lowerUrl.includes("download") || lowerCtx.includes("download")) {
    score += 40;
  }
  if (lowerUrl.includes("attachment") || lowerUrl.includes("file=")) {
    score += 25;
  }
  if (lowerUrl.startsWith("https://")) {
    score += 10;
  }

  if (
    lowerUrl.includes("google.com/search") ||
    lowerUrl.includes("/imgres") ||
    lowerUrl.includes("bing.com/images/search")
  ) {
    score -= 80;
  }

  if (lowerUrl.includes("?")) {
    score -= 4;
  }

  const extBoost = [
    ".mp4", ".mkv", ".webm", ".avi", ".ts", ".m3u8", ".mov", ".flv", ".wmv", ".rmvb", ".m4v", ".mpg", ".mpeg", ".3gp",
    ".mp3", ".m4a", ".flac",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".exe", ".msi"
  ];
  if (extBoost.some((ext) => lowerUrl.includes(ext))) {
    score += 45;
  }

  if (/(\.jpg|\.jpeg|\.png|\.webp|\.gif|\.bmp)(\?|$)/i.test(lowerUrl)) {
    score += 30;
  }

  if (/(\.mp4|\.mkv|\.webm|\.mp3|\.m4a|\.flac|\.ts|\.m3u8|\.avi|\.mov|\.flv|\.wmv|\.rmvb|\.m4v|\.mpg|\.mpeg|\.3gp)(\?|$)/i.test(lowerUrl)) {
    score += 30;
  }

  return score;
}

function collectSourceCandidates(limit = 80) {
  const rawCandidates = [];

  const pushCandidate = (rawUrl, filename = "", context = "") => {
    const url = normalizeUrl(rawUrl);
    if (!url || !isSupportedUrl(url)) {
      return;
    }
    rawCandidates.push({
      url,
      filename,
      referer: window.location.href,
      score: scoreCandidate(url, context),
    });
  };

  const anchors = document.querySelectorAll("a[href]");
  for (const a of anchors) {
    const text = (a.textContent || "").trim();
    const href = a.getAttribute("href") || "";
    const suggestedName = a.getAttribute("download") || "";
    if (text.toLowerCase().includes("download") || href.toLowerCase().includes("download")) {
      pushCandidate(href, suggestedName, text || href);
    }
  }

  const mediaSelectors = [
    "img[src]", "video[src]", "audio[src]", "source[src]",
    "iframe[src]", "iframe[data-src]", "iframe[data-url]",
    "[data-src]", "[data-url]", "[data-href]",
  ];
  for (const selector of mediaSelectors) {
    const nodes = document.querySelectorAll(selector);
    for (const node of nodes) {
      const src =
        node.getAttribute("src") ||
        node.getAttribute("data-src") ||
        node.getAttribute("data-url") ||
        node.getAttribute("data-href") ||
        "";
      pushCandidate(src, "", selector);
    }
  }

  const metas = document.querySelectorAll("meta[property], meta[name]");
  for (const meta of metas) {
    const prop = (meta.getAttribute("property") || meta.getAttribute("name") || "").toLowerCase();
    if (!prop.includes("image") && !prop.includes("video")) {
      continue;
    }
    pushCandidate(meta.getAttribute("content") || "", "", prop);
  }

  const byUrl = new Map();
  for (const c of rawCandidates) {
    const existing = byUrl.get(c.url);
    if (!existing || c.score > existing.score) {
      byUrl.set(c.url, c);
    }
  }

  return Array.from(byUrl.values())
    .sort((a, b) => b.score - a.score)
    .slice(0, Math.max(1, limit));
}

async function tryCapture(anchor, eventTarget) {
  const rawHref = anchor?.href || anchor?.getAttribute("href") || "";
  const mediaUrl = nearestMediaUrl(eventTarget);
  const chosenUrl = normalizeUrl(mediaUrl || rawHref || "");
  if (!chosenUrl || !isSupportedUrl(chosenUrl)) {
    return { captured: false };
  }

  if (!hasDownloadHints(anchor, eventTarget)) {
    return { captured: false };
  }

  const sourcePageUrl = findSourcePageUrl(anchor, eventTarget);

  try {
    const res = await chrome.runtime.sendMessage({
      type: "captureDownloadLink",
      url: chosenUrl,
      referer: sourcePageUrl,
      pageUrl: sourcePageUrl,
      filename: anchor.getAttribute("download") || "",
      userInitiated: true,
    });

    if (res?.ok && res?.captured) {
      return { captured: true };
    }

    return { captured: false };
  } catch (_) {
    return { captured: false };
  }
}

function injectStyles() {
  if (document.getElementById("idm-bridge-inline-style")) {
    return;
  }

  const style = document.createElement("style");
  style.id = "idm-bridge-inline-style";
  style.textContent = `
    .${IDM_ICON_CLASS} {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      margin-left: 6px;
      padding: 2px 6px;
      border: 1px solid #0f766e;
      border-radius: 999px;
      background: #ecfeff;
      color: #0f766e;
      font-size: 11px;
      font-weight: 600;
      line-height: 1.4;
      cursor: pointer;
      user-select: none;
      vertical-align: middle;
    }

    .${IDM_ICON_CLASS}:hover {
      background: #0f766e;
      color: #ffffff;
    }

    .${IDM_ICON_CLASS}[data-state="sending"] {
      opacity: 0.8;
      pointer-events: none;
    }

    .${IDM_MEDIA_FLOAT_CLASS} {
      position: absolute;
      z-index: 2147483646;
      border: 1px solid #0f766e;
      border-radius: 999px;
      background: rgba(236, 254, 255, 0.95);
      color: #0f766e;
      padding: 4px 9px;
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 6px 20px rgba(15, 118, 110, 0.22);
      display: none;
    }

    .${IDM_MEDIA_FLOAT_CLASS}:hover {
      background: #0f766e;
      color: #ffffff;
    }
  `;

  (document.head || document.documentElement).appendChild(style);
}

function makeIdmButton(anchor) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = IDM_ICON_CLASS;
  button.textContent = "IDM";
  button.title = "Download with IDM";

  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();

    button.dataset.state = "sending";
    const originalText = button.textContent;
    button.textContent = "...";

    const result = await tryCapture(anchor, anchor);
    if (result.captured) {
      button.textContent = "OK";
      setTimeout(() => {
        button.textContent = "IDM";
        delete button.dataset.state;
      }, 1200);
      return;
    }

    button.textContent = originalText;
    delete button.dataset.state;
  });

  return button;
}

function shouldDecorateAnchor(anchor) {
  const rawHref = anchor?.href || anchor?.getAttribute("href") || "";
  if (!isSupportedUrl(rawHref)) {
    return false;
  }
  return hasDownloadHints(anchor, anchor) && hasExplicitDownloadUrlSignal(anchor) && !isLikelyActionOrGeneratorLink(anchor, anchor);
}

function decorateDownloadAnchors(root = document) {
  const anchors = root.querySelectorAll ? root.querySelectorAll("a[href]") : [];
  for (const anchor of anchors) {
    if (!shouldDecorateAnchor(anchor)) {
      continue;
    }
    if (anchor.getAttribute(IDM_ANCHOR_MARK) === "1") {
      continue;
    }

    const btn = makeIdmButton(anchor);
    anchor.insertAdjacentElement("afterend", btn);
    anchor.setAttribute(IDM_ANCHOR_MARK, "1");
  }
}

function nearestMediaUrl(el) {
  let node = el;
  while (node && node !== document.documentElement) {
    if (node.tagName) {
      const tag = node.tagName.toLowerCase();
      if (tag === "img" || tag === "video" || tag === "audio" || tag === "source") {
        return (
          node.getAttribute("src") ||
          node.getAttribute("data-src") ||
          node.currentSrc ||
          ""
        );
      }
    }
    node = node.parentElement;
  }
  return "";
}

function mediaSourceFromElement(node) {
  if (!node || !node.tagName) {
    return "";
  }

  const tag = node.tagName.toLowerCase();
  if (tag !== "video" && tag !== "audio") {
    return "";
  }

  const direct =
    node.currentSrc ||
    node.getAttribute("src") ||
    "";
  if (direct) {
    return normalizeUrl(direct);
  }

  const source = node.querySelector("source[src]");
  if (source) {
    return normalizeUrl(source.getAttribute("src") || "");
  }

  return "";
}

function ensureMediaFloatButton() {
  if (mediaFloatButton && document.body?.contains(mediaFloatButton)) {
    return mediaFloatButton;
  }

  mediaFloatButton = document.createElement("button");
  mediaFloatButton.type = "button";
  mediaFloatButton.className = IDM_MEDIA_FLOAT_CLASS;
  mediaFloatButton.textContent = "Download with IDM";

  mediaFloatButton.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();

    const media = mediaFloatTarget;
    const mediaUrl = mediaSourceFromElement(media);
    if (!mediaUrl || !isSupportedUrl(mediaUrl)) {
      hideMediaFloatButton();
      return;
    }

    const previous = mediaFloatButton.textContent;
    mediaFloatButton.textContent = "...";
    try {
      const res = await chrome.runtime.sendMessage({
        type: "addDownload",
        url: mediaUrl,
        referer: window.location.href,
        pageUrl: window.location.href,
        source: "media_float_button",
      });
      mediaFloatButton.textContent = res?.ok || res?.queued ? "OK" : "Fail";
    } catch (_) {
      mediaFloatButton.textContent = "Fail";
    }

    setTimeout(() => {
      if (mediaFloatButton) {
        mediaFloatButton.textContent = previous;
      }
    }, 900);
  });

  (document.body || document.documentElement).appendChild(mediaFloatButton);
  return mediaFloatButton;
}

function hideMediaFloatButton() {
  if (mediaFloatButton) {
    mediaFloatButton.style.display = "none";
  }
  mediaFloatTarget = null;
}

function positionMediaFloatButton(target) {
  const button = ensureMediaFloatButton();
  if (!button || !target || !target.getBoundingClientRect) {
    hideMediaFloatButton();
    return;
  }

  const rect = target.getBoundingClientRect();
  if (!rect || rect.width < 40 || rect.height < 24) {
    hideMediaFloatButton();
    return;
  }

  const top = window.scrollY + rect.top + 8;
  const left = window.scrollX + rect.right - 152;

  button.style.top = `${Math.max(window.scrollY + 6, top)}px`;
  button.style.left = `${Math.max(window.scrollX + 6, left)}px`;
  button.style.display = "inline-flex";
  mediaFloatTarget = target;
}

function maybeShowMediaFloatFromEvent(event) {
  const rawTarget = event?.target;
  if (!(rawTarget instanceof Element)) {
    hideMediaFloatButton();
    return;
  }

  if (
    mediaFloatButton &&
    (rawTarget === mediaFloatButton || rawTarget.closest(`.${IDM_MEDIA_FLOAT_CLASS}`))
  ) {
    return;
  }

  const mediaTarget = rawTarget.closest("video, audio");
  if (!mediaTarget) {
    hideMediaFloatButton();
    return;
  }

  const src = mediaSourceFromElement(mediaTarget);
  if (!src || !isSupportedUrl(src)) {
    hideMediaFloatButton();
    return;
  }

  positionMediaFloatButton(mediaTarget);
}

function safeRuntimeSendMessage(payload) {
  try {
    const maybePromise = chrome.runtime.sendMessage(payload, () => {
      // Swallow transient runtime errors during extension reload/update.
      void chrome.runtime.lastError;
    });

    if (maybePromise && typeof maybePromise.catch === "function") {
      maybePromise.catch(() => {});
    }
  } catch (_) {
    // Extension context can be invalidated during hot reload; ignore.
  }
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
    if (!normalized || !/^[a-z0-9.-]+$/.test(normalized) || seen.has(normalized)) {
      continue;
    }
    seen.add(normalized);
    out.push(normalized);
  }

  return out;
}

function isCurrentSiteDisabled(settings) {
  let host = "";
  try {
    host = new URL(window.location.href).hostname.toLowerCase();
  } catch (_) {
    host = "";
  }
  if (!host) {
    return false;
  }

  const rules = normalizeDisabledSites(settings?.disabledSites || []);
  return rules.some((rule) => host === rule || host.endsWith(`.${rule}`));
}

async function shouldEnableCaptureOnThisPage() {
  try {
    const data = await chrome.storage.local.get("settings");
    const settings = data?.settings || {};
    return !isCurrentSiteDisabled(settings);
  } catch (_) {
    return true;
  }
}

function rememberContextTarget(event) {
  const anchor = closestAnchor(event.target);
  const mediaUrl = nearestMediaUrl(event.target);
  const href = anchor?.getAttribute("href") || anchor?.href || "";
  const chosen = normalizeUrl(mediaUrl || href || "");
  if (!chosen || !isSupportedUrl(chosen)) {
    return;
  }

  const sourcePageUrl = findSourcePageUrl(anchor, event.target);

  safeRuntimeSendMessage({
    type: "rememberContextTarget",
    url: chosen,
    referer: sourcePageUrl,
    pageUrl: sourcePageUrl,
    filename: anchor?.getAttribute("download") || "",
  });
}

async function initializeIdmContentCapture() {
  const enabled = await shouldEnableCaptureOnThisPage();
  if (!enabled) {
    return;
  }

  document.addEventListener(
    "click",
    (event) => {
      const anchor = closestAnchor(event.target);
      if (!anchor) {
        return;
      }

      if (event.defaultPrevented) {
        return;
      }

      // Only intercept links that already look like concrete downloadable URLs.
      if (!shouldInterceptAnchorClick(anchor, event.target)) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();

      tryCapture(anchor, event.target).then((result) => {
        if (result.captured) {
          return;
        }

        // Fallback to normal browser behavior when IDM capture fails.
        const fallbackHref = anchor?.href || anchor?.getAttribute("href") || "";
        if (/^javascript:/i.test((fallbackHref || "").trim())) {
          return;
        }
        window.location.href = fallbackHref;
      });
    },
    true
  );

  document.addEventListener("contextmenu", rememberContextTarget, true);

  injectStyles();
  decorateDownloadAnchors(document);
  document.querySelectorAll("iframe").forEach((node) => inspectIframeNode(node, "iframe_initial_scan"));

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "attributes") {
        const target = mutation.target;
        if (target instanceof Element && target.matches("iframe")) {
          inspectIframeNode(target, `iframe_attr_${mutation.attributeName || "unknown"}`);
        }
        continue;
      }

      for (const node of mutation.addedNodes) {
        if (!(node instanceof Element)) {
          continue;
        }

        if (node.matches && node.matches("iframe")) {
          inspectIframeNode(node, "iframe_added");
        }

        if (node.querySelectorAll) {
          node.querySelectorAll("iframe").forEach((iframe) => inspectIframeNode(iframe, "iframe_added_subtree"));
        }

        if (node.matches && node.matches("a[href]")) {
          decorateDownloadAnchors(node.parentElement || document);
        } else {
          decorateDownloadAnchors(node);
        }
      }
    }
  });

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["src", "data-src", "data-url"],
  });

  document.addEventListener("mousemove", maybeShowMediaFloatFromEvent, true);
  document.addEventListener("scroll", () => {
    if (mediaFloatTarget) {
      positionMediaFloatButton(mediaFloatTarget);
    }
  }, true);
  window.addEventListener("resize", () => {
    if (mediaFloatTarget) {
      positionMediaFloatButton(mediaFloatTarget);
    }
  });

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "collectSourceCandidates") {
      return false;
    }

    const limit = Number(message.limit) || 80;
    const candidates = collectSourceCandidates(limit);
    sendResponse({ ok: true, candidates });
    return false;
  });
}

initializeIdmContentCapture().catch(() => {});
