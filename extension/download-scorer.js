// IDM v2.0 — download-scorer.js — audited 2026-03-28

// Debug mode: set to true to enable detailed console logging
const DEBUG_MODE = false;

(function initDownloadScorer(globalScope) {
  const KNOWN_EXTENSIONS = new Set([
    "zip", "rar", "7z", "tar", "gz", "exe", "msi", "dmg", "apk", "iso", "pdf", "mp4", "mp3", "mkv", "flac", "wav", "epub", "deb", "rpm", "pkg",
  ]);

  const POSITIVE_URL_SEGMENTS = ["/download/", "/dl/", "/get/", "/file/", "/files/", "/attachment/", "/release/", "/dist/", "/assets/", "/media/", "/cdn/"];
  const POSITIVE_TEXT_KEYWORDS = ["download", "get", "save", "export", "install", "setup", "update", "release", "version", "v1", "v2", "x64", "x86", "win", "mac", "linux", "apk"];
  const NAV_TEXT_KEYWORDS = ["home", "about", "contact", "login", "signup", "register", "blog", "news", "help", "faq", "terms", "privacy", "back", "next", "previous", "more", "read more", "see all", "view all"];
  const NAV_SELECTORS = "nav, header, footer, .navbar, .menu, .sidebar, .breadcrumb, .pagination";
  const DOWNLOAD_SECTION_HINTS = ["download", "release", "asset", "attachment", "file-list", "dl-section"];
  const SCRIPT_HANDLER_EXTENSIONS = [".php", ".asp", ".aspx", ".do", ".action", ".cgi", ".pl", ".py"];

  const BINARY_CONTENT_TYPES = [
    "application/octet-stream",
    "application/zip",
    "application/x-msdownload",
    "application/x-rar-compressed",
    "application/pdf",
    "application/x-7z-compressed",
    "application/gzip",
    "application/x-tar",
    "application/vnd.android.package-archive",
    "application/x-apple-diskimage",
    "application/x-iso9660-image",
    "image/tiff",
  ];
  const NEGATIVE_CONTENT_TYPES = ["text/html", "text/plain", "application/json", "text/css", "application/javascript", "text/javascript"];

  const NON_DOWNLOAD_DOMAINS = [
    "google.com", "youtube.com", "twitter.com", "facebook.com", "instagram.com", "reddit.com", "wikipedia.org", "linkedin.com", "notion.so", "figma.com",
  ];

  const SKIP_HEAD_DOMAIN_HINTS = ["twitter.com", "facebook.com", "linkedin.com", "youtube.com", "instagram.com", "reddit.com"];

  const CDN_HOST_PATTERNS = [
    /^cdn\./i, /^dl\./i, /^download\./i, /^files\./i, /^releases\./i,
  ];

  function isFunction(value) {
    return typeof value === "function";
  }

  function nowMs() {
    return Date.now();
  }

  function clampScore(score) {
    if (!Number.isFinite(score)) return 0;
    return Math.max(0, Math.min(100, Math.round(score)));
  }

  function lower(value) {
    return String(value || "").toLowerCase();
  }

  function trimText(value) {
    return String(value || "").trim();
  }

  function safeTextContainsFileInfo(text) {
    const hay = lower(String(text || ""));
    if (!hay) return false;
    const hasSize = /\b\d+(?:\.\d+)?\s?(?:gb|mb|kb)\b/i.test(hay);
    const hasVideoType = /(video\/x-matroska|video\/mp4|matroska|\.mkv\b|\.mp4\b|file\s*type)/i.test(hay);
    return hasSize && hasVideoType;
  }

  function hostMatches(hostname, baseDomain) {
    const host = lower(hostname);
    const domain = lower(baseDomain);
    return host === domain || host.endsWith(`.${domain}`);
  }

  function parseUrl(value, baseUrl) {
    try {
      return new URL(String(value || ""), baseUrl || undefined);
    } catch (_) {
      return null;
    }
  }

  class DownloadScorer {
    constructor(options = {}) {
      this.defaultThreshold = Number(options.defaultThreshold || 60);
      this.debug = Boolean(options.debug);
      this.maxConcurrentHead = Math.max(1, Number(options.maxConcurrentHead || 3));
      this.headTimeoutMs = Math.max(250, Number(options.headTimeoutMs || 2000));
      this.cacheTtlMs = Math.max(1000, Number(options.cacheTtlMs || 60000));
      this.fetchFn = isFunction(options.fetchFn) ? options.fetchFn : (globalScope.fetch ? globalScope.fetch.bind(globalScope) : null);
      this.nowFn = isFunction(options.nowFn) ? options.nowFn : nowMs;

      this.headCache = new Map();
      this.headQueue = [];
      this.activeHeadRequests = 0;
    }

    setDebug(enabled) {
      this.debug = Boolean(enabled);
    }

    _emitSignal(signals, runningTotalRef, points, description) {
      runningTotalRef.value += points;
      signals.push({ points, description, total: runningTotalRef.value });
      if (DEBUG_MODE) {
        const signed = points >= 0 ? `+${points}` : `${points}`;
        console.debug(`[IDM Scorer] Signal: ${signed} ${description}, total: ${runningTotalRef.value}`);
      }
    }

    _finalize(score, signals) {
      const clamped = clampScore(score);
      const verdict = clamped >= 60 ? "download" : (clamped >= 40 ? "unknown" : "page");
      return { score: clamped, signals, verdict };
    }

    _extractInput(linkLike, pageContext = {}, overrides = {}) {
      const pageUrl = String(pageContext.pageUrl || pageContext.url || (globalScope.location ? globalScope.location.href : ""));
      const pageParsed = parseUrl(pageUrl);

      let element = null;
      let hrefRaw = "";
      let href = "";
      let text = "";
      let title = "";
      let target = "";
      let hasDownloadAttribute = false;

      const looksLikeElement = typeof Element !== "undefined" && linkLike instanceof Element;
      if (looksLikeElement) {
        const anchor = linkLike;
        element = anchor;
        hrefRaw = trimText(anchor.getAttribute("href") || anchor.href || "");
        const parsed = parseUrl(hrefRaw, pageUrl);
        href = parsed ? parsed.toString() : "";
        text = trimText(anchor.textContent || "");
        title = trimText(anchor.getAttribute("title") || "");
        target = trimText(anchor.getAttribute("target") || "");
        hasDownloadAttribute = anchor.hasAttribute("download");
      } else {
        hrefRaw = trimText(overrides.hrefRaw || linkLike?.hrefRaw || "");
        href = trimText(overrides.url || linkLike?.url || "");
        if (!href && hrefRaw) {
          const parsed = parseUrl(hrefRaw, pageUrl);
          href = parsed ? parsed.toString() : "";
        }
        text = trimText(overrides.text || linkLike?.text || "");
        title = trimText(overrides.title || linkLike?.title || "");
        target = trimText(overrides.target || linkLike?.target || "");
        hasDownloadAttribute = Boolean(overrides.hasDownloadAttribute || linkLike?.hasDownloadAttribute);
      }

      const parsedUrl = parseUrl(href, pageUrl);

      return {
        element,
        pageUrl,
        pageParsed,
        hrefRaw,
        url: parsedUrl ? parsedUrl.toString() : "",
        parsedUrl,
        text,
        title,
        target,
        hasDownloadAttribute,
        headers: overrides.headers || linkLike?.headers || null,
      };
    }

    _isKnownCdnOrFileHost(parsedUrl) {
      if (!parsedUrl) return false;
      const host = lower(parsedUrl.hostname);
      const full = lower(parsedUrl.toString());
      if (full.includes("github.com/") && full.includes("/releases")) return true;
      if (host.includes("sourceforge.net")) return true;
      if (host === "s3.amazonaws.com" || host === "storage.googleapis.com") return true;
      if (host.endsWith(".blob.core.windows.net")) return true;
      if (host.includes("mediafire.com") || host === "mega.nz") return true;
      if (host === "drive.google.com") return true;
      return CDN_HOST_PATTERNS.some((pattern) => pattern.test(host));
    }

    _isKnownNonDownloadDomain(parsedUrl) {
      if (!parsedUrl) return false;
      const host = lower(parsedUrl.hostname);
      const full = lower(parsedUrl.toString());
      if (full.includes("drive.google.com/file/") || full.includes("drive.google.com/uc?export=download")) {
        return false;
      }
      return NON_DOWNLOAD_DOMAINS.some((domain) => hostMatches(host, domain));
    }

    _hasDownloadSectionAncestor(element) {
      if (!element || typeof element.closest !== "function") return false;
      let cursor = element;
      for (let i = 0; i < 7 && cursor; i += 1) {
        const idVal = lower(cursor.id || "");
        const classVal = lower(typeof cursor.className === "string" ? cursor.className : "");
        if (DOWNLOAD_SECTION_HINTS.some((hint) => idVal.includes(hint) || classVal.includes(hint))) {
          return true;
        }
        cursor = cursor.parentElement;
      }
      return false;
    }

    _hasNavigationAncestor(element) {
      if (!element || typeof element.closest !== "function") return false;
      return Boolean(element.closest(NAV_SELECTORS));
    }

    _findPathExtension(parsedUrl) {
      if (!parsedUrl) return "";
      const path = String(parsedUrl.pathname || "");
      const last = path.split("/").pop() || "";
      const idx = last.lastIndexOf(".");
      if (idx <= 0 || idx === last.length - 1) return "";
      return lower(last.slice(idx + 1));
    }

    _looksLikePagePath(parsedUrl) {
      if (!parsedUrl) return true;
      const path = lower(parsedUrl.pathname || "");
      if (!path || path === "/") return true;
      if (path.endsWith("/")) return true;
      if (/\.(html?|php|aspx?)$/.test(path)) return true;
      const ext = this._findPathExtension(parsedUrl);
      return !ext;
    }

    _containsPositiveText(text, title) {
      const hay = lower(`${text} ${title}`);
      return POSITIVE_TEXT_KEYWORDS.some((k) => hay.includes(k));
    }

    _hasDownloadServerText(text, title) {
      const hay = trimText(`${text} ${title}`);
      return /download\s*[\[\(].*[\]\)]/i.test(hay);
    }

    _startsWithDownload(text) {
      return lower(trimText(text)).startsWith("download");
    }

    _pageHasNearbyFileInfo(element) {
      if (typeof document === "undefined") return false;

      const localScope = element && typeof element.closest === "function"
        ? element.closest("article, section, main, div, li, td, tr, table")
        : null;

      const localRoot = localScope || document;
      const hasHintNode = Boolean(localRoot.querySelector?.('[class*="file"], [class*="info"], [class*="detail"], table'));
      const hasInfoText = safeTextContainsFileInfo(localRoot.textContent || "");
      if (hasHintNode && hasInfoText) {
        return true;
      }

      const pageHasHintNode = Boolean(document.querySelector?.('[class*="file"], [class*="info"], [class*="detail"], table'));
      const pageHasInfoText = safeTextContainsFileInfo(document.body?.textContent || "");
      return pageHasHintNode && pageHasInfoText;
    }

    _hasDownloadIcon(linkElement) {
      if (!linkElement || typeof linkElement.querySelector !== "function") {
        return false;
      }
      return Boolean(linkElement.querySelector('img, svg, i[class*="download"], span[class*="icon"]'));
    }

    _isCommonNavText(text, title) {
      const hay = lower(trimText(text || title));
      return NAV_TEXT_KEYWORDS.includes(hay);
    }

    _hasDownloadPathHint(parsedUrl) {
      if (!parsedUrl) return false;
      const path = lower(parsedUrl.pathname || "");
      return POSITIVE_URL_SEGMENTS.some((segment) => path.includes(segment));
    }

    _isScriptRedirectHandler(parsedUrl) {
      if (!parsedUrl) return false;
      const path = lower(parsedUrl.pathname || "");
      return SCRIPT_HANDLER_EXTENSIONS.some((ext) => path.endsWith(ext));
    }

    _hasFileQueryHint(parsedUrl) {
      if (!parsedUrl || !parsedUrl.searchParams) return false;
      const keys = ["file", "filename", "name", "download", "attachment", "target", "path", "id"];
      for (const key of keys) {
        const val = trimText(parsedUrl.searchParams.get(key) || "");
        if (!val) continue;
        if (/\.[a-z0-9]{2,6}(?:$|\?)/i.test(val) || /\.[a-z0-9]{2,6}$/i.test(val)) {
          return true;
        }
      }
      return false;
    }

    _matchesSpecialCase(parsedUrl) {
      if (!parsedUrl) return null;
      const full = lower(parsedUrl.toString());
      if (full.includes("drive.google.com/file/") || full.includes("drive.google.com/uc?export=download")) {
        return { points: 60, description: "Google Drive file link" };
      }
      if (/^https?:\/\/github\.com\/[^/]+\/[^/]+\/releases\/download\//i.test(parsedUrl.toString())) {
        return { points: 80, description: "GitHub releases asset" };
      }
      if (/^https?:\/\/([^.]+\.)?dropbox\.com\/s\//i.test(parsedUrl.toString())) {
        const dl = parsedUrl.searchParams.get("dl");
        if (dl === "1" || dl === "0") {
          return { points: 70, description: "Dropbox direct file link" };
        }
      }
      if (full.includes("onedrive.live.com/download")) {
        return { points: 60, description: "OneDrive direct download" };
      }
      return null;
    }

    _isNeverDownloadPattern(inputData) {
      const raw = lower(inputData.hrefRaw || inputData.url || "");
      const url = inputData.parsedUrl;
      const full = lower(url ? url.toString() : raw);

      if (!raw && !url) return true;
      if (raw === "#" || raw.startsWith("#")) return true;
      if (/^(mailto:|tel:|javascript:)/i.test(raw)) return true;
      if (full.includes("twitter.com/intent") || full.includes("facebook.com/sharer") || full.includes("linkedin.com/share")) return true;
      if (/\/(login|logout|signin|oauth|auth)\b/i.test(full)) return true;
      if (/utm_source=|[?&]ref=|\/click\?/i.test(full)) return true;
      if (full.startsWith("data:")) return true;
      return false;
    }

    _applyUrlAndDomSignals(inputData) {
      const signals = [];
      const total = { value: 0 };
      const parsedUrl = inputData.parsedUrl;
      const url = inputData.url;

      if (!url) {
        return this._finalize(0, signals);
      }

      const lowerUrl = lower(url);
      if (lowerUrl.startsWith("data:")) {
        this._emitSignal(signals, total, -100, "Data URI ignored");
        return this._finalize(0, signals);
      }

      if (hostMatches(parsedUrl?.hostname || "", "youtube.com") || hostMatches(parsedUrl?.hostname || "", "youtu.be")) {
        this._emitSignal(signals, total, -100, "YouTube URLs are excluded");
        return this._finalize(0, signals);
      }

      if (lowerUrl.startsWith("blob:")) {
        this._emitSignal(signals, total, 50, "Blob URL uncertain by default");
      }

      if (this._isNeverDownloadPattern(inputData)) {
        this._emitSignal(signals, total, -40, "Known non-download URL pattern");
      }

      const specialCase = this._matchesSpecialCase(parsedUrl);
      if (specialCase) {
        this._emitSignal(signals, total, specialCase.points, specialCase.description);
      }

      const ext = this._findPathExtension(parsedUrl);
      if (ext && KNOWN_EXTENSIONS.has(ext)) {
        this._emitSignal(signals, total, 40, `File extension .${ext}`);
      }

      if (this._hasDownloadPathHint(parsedUrl)) {
        this._emitSignal(signals, total, 15, "URL contains download keyword segment");
      }

      if (this._isScriptRedirectHandler(parsedUrl)) {
        this._emitSignal(signals, total, -18, "URL uses script redirect handler path");
        if (this._hasFileQueryHint(parsedUrl)) {
          this._emitSignal(signals, total, 12, "Query contains filename-like file parameter");
        }
      }

      if (this._containsPositiveText(inputData.text, inputData.title)) {
        this._emitSignal(signals, total, 10, "Link text/title contains download keyword");
      }

      if (this._hasDownloadServerText(inputData.text, inputData.title)) {
        this._emitSignal(signals, total, 35, "Link text contains download server pattern");
      }

      if (this._startsWithDownload(inputData.text)) {
        this._emitSignal(signals, total, 20, "Link text starts with download");
      }

      if (this._pageHasNearbyFileInfo(inputData.element)) {
        this._emitSignal(signals, total, 15, "Nearby page section shows file metadata");
      }

      if (this._hasDownloadIcon(inputData.element)) {
        this._emitSignal(signals, total, 10, "Link contains download icon element");
      }

      if (inputData.hasDownloadAttribute) {
        this._emitSignal(signals, total, 25, "Anchor has download attribute");
      }

      if (parsedUrl && !/\.(html?|php|aspx?)$/i.test(parsedUrl.pathname || "") && !(parsedUrl.pathname || "").endsWith("/")) {
        this._emitSignal(signals, total, 10, "File-like path with no page suffix");
      }

      if (this._isKnownCdnOrFileHost(parsedUrl)) {
        this._emitSignal(signals, total, 20, "Known CDN/file host domain");
      }

      if (this._hasDownloadSectionAncestor(inputData.element)) {
        this._emitSignal(signals, total, 10, "Link appears inside download/release section");
      }

      const sameHost = Boolean(inputData.pageParsed && parsedUrl && lower(inputData.pageParsed.hostname) === lower(parsedUrl.hostname));
      if (sameHost && this._looksLikePagePath(parsedUrl)) {
        this._emitSignal(signals, total, -40, "Same-domain page-style URL");
      }

      if (this._hasNavigationAncestor(inputData.element)) {
        this._emitSignal(signals, total, -50, "Link is inside navigation container");
      }

      if (this._isCommonNavText(inputData.text, inputData.title)) {
        this._emitSignal(signals, total, -30, "Link text matches common navigation phrase");
      }

      if (this._isKnownNonDownloadDomain(parsedUrl)) {
        this._emitSignal(signals, total, -30, "Known non-download domain");
      }

      const looksLikePage = this._looksLikePagePath(parsedUrl);
      const noDownloadHints = !this._hasDownloadPathHint(parsedUrl) && !inputData.hasDownloadAttribute;
      if (!inputData.target && looksLikePage && noDownloadHints) {
        this._emitSignal(signals, total, -15, "Same-tab navigation with no download hint");
      }

      return this._finalize(total.value, signals);
    }

    _applyHeadSignals(baseResult, headResult) {
      if (!headResult || !headResult.headers) {
        return baseResult;
      }

      const signals = [...baseResult.signals];
      const total = { value: baseResult.score };
      const disposition = lower(headResult.headers["content-disposition"] || "");
      const contentType = lower(headResult.headers["content-type"] || "").split(";")[0].trim();
      const contentLength = Number(headResult.headers["content-length"] || 0);

      if (disposition.includes("attachment")) {
        this._emitSignal(signals, total, 35, "Content-Disposition attachment header");
      }

      const isVideoAudio = contentType.startsWith("video/") || contentType.startsWith("audio/");
      const isBinaryType = BINARY_CONTENT_TYPES.includes(contentType) || isVideoAudio;
      const isNegativeType = NEGATIVE_CONTENT_TYPES.includes(contentType);

      if (isBinaryType) {
        this._emitSignal(signals, total, 30, `Binary/media content type ${contentType}`);
      }

      if (contentLength > 1024 * 1024) {
        this._emitSignal(signals, total, 15, "Content-Length above 1MB");
        if (contentLength > 10 * 1024 * 1024) {
          this._emitSignal(signals, total, 10, "Content-Length above 10MB bonus");
        }
      }

      if (contentType === "text/html") {
        this._emitSignal(signals, total, -50, "HEAD returned text/html");
      } else if (isNegativeType) {
        this._emitSignal(signals, total, -15, `HEAD returned page-like content type ${contentType}`);
      }

      return this._finalize(total.value, signals);
    }

    scoreSync(linkLike, pageContext = {}, overrides = {}) {
      const input = this._extractInput(linkLike, pageContext, overrides);
      let result = this._applyUrlAndDomSignals(input);
      if (overrides.headResult) {
        result = this._applyHeadSignals(result, overrides.headResult);
      }
      return result;
    }

    _shouldHeadRequest(scoreResult, input) {
      if (!this.fetchFn) return false;
      if (scoreResult.score < 30 || scoreResult.score > 70) return false;
      const parsedUrl = input.parsedUrl;
      if (!parsedUrl) return false;
      const protocol = lower(parsedUrl.protocol || "");
      if (!(protocol === "http:" || protocol === "https:")) return false;
      const raw = lower(input.hrefRaw || "");
      if (raw.startsWith("#") || raw === "#") return false;
      if (/^(mailto:|tel:|javascript:)/i.test(raw)) return false;
      if (this._isKnownNonDownloadDomain(parsedUrl) && !lower(parsedUrl.toString()).includes("drive.google.com/file/")) return false;
      if (SKIP_HEAD_DOMAIN_HINTS.some((domain) => hostMatches(parsedUrl.hostname, domain))) return false;
      if (this.headCache.has(input.url)) {
        const cached = this.headCache.get(input.url);
        if (cached && cached.expiresAt > this.nowFn()) {
          return true;
        }
      }
      return true;
    }

    _withTimeoutAbort(timeoutMs) {
      const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
      let timeoutId = null;
      if (controller && timeoutMs > 0) {
        timeoutId = setTimeout(() => controller.abort(), timeoutMs);
      }
      return {
        signal: controller ? controller.signal : undefined,
        dispose: () => {
          if (timeoutId) clearTimeout(timeoutId);
        },
      };
    }

    async _fetchHeadWithRedirects(url) {
      if (!this.fetchFn) {
        return { ok: false };
      }

      const abort = this._withTimeoutAbort(this.headTimeoutMs);
      try {
        let current = url;
        for (let i = 0; i < 3; i += 1) {
          const response = await this.fetchFn(current, {
            method: "HEAD",
            redirect: "manual",
            signal: abort.signal,
            credentials: "omit",
            cache: "no-store",
          });

          const status = Number(response?.status || 0);
          const location = response?.headers?.get ? response.headers.get("location") : "";
          const isRedirect = status >= 300 && status < 400 && location;
          if (isRedirect && i < 2) {
            current = new URL(location, current).toString();
            continue;
          }

          const headers = {};
          if (response?.headers?.forEach) {
            response.headers.forEach((value, key) => {
              headers[lower(key)] = String(value || "");
            });
          }

          return {
            ok: Boolean(response?.ok),
            status,
            finalUrl: current,
            headers,
          };
        }
        return { ok: false };
      } catch (_) {
        try {
          await this.fetchFn(url, {
            method: "HEAD",
            mode: "no-cors",
            signal: abort.signal,
            credentials: "omit",
            cache: "no-store",
          });
          return {
            ok: true,
            status: 0,
            finalUrl: url,
            headers: null,
            noCors: true,
          };
        } catch (_) {
          return { ok: false, finalUrl: url, headers: null };
        }
      } finally {
        abort.dispose();
      }
    }

    _enqueueHead(url) {
      return new Promise((resolve) => {
        this.headQueue.push({ url, resolve });
        this._drainHeadQueue();
      });
    }

    _drainHeadQueue() {
      if (this.activeHeadRequests >= this.maxConcurrentHead) {
        return;
      }
      const next = this.headQueue.shift();
      if (!next) return;

      this.activeHeadRequests += 1;
      this._fetchHeadWithRedirects(next.url)
        .then((result) => next.resolve(result || { ok: false }))
        .catch(() => next.resolve({ ok: false }))
        .finally(() => {
          this.activeHeadRequests = Math.max(0, this.activeHeadRequests - 1);
          this._drainHeadQueue();
        });
    }

    async _getHeadResult(url) {
      const cached = this.headCache.get(url);
      const now = this.nowFn();
      if (cached && cached.expiresAt > now) {
        return cached.value;
      }

      const fresh = await this._enqueueHead(url);
      this.headCache.set(url, {
        value: fresh,
        expiresAt: now + this.cacheTtlMs,
      });
      return fresh;
    }

    async score(linkLike, pageContext = {}, overrides = {}) {
      const input = this._extractInput(linkLike, pageContext, overrides);
      if (!input.url) {
        return this._finalize(0, []);
      }

      const baseResult = this.scoreSync(linkLike, pageContext, overrides);
      if (!this._shouldHeadRequest(baseResult, input)) {
        return baseResult;
      }

      const headResult = await this._getHeadResult(input.url);
      if (!headResult || (!headResult.headers && !headResult.finalUrl)) {
        return baseResult;
      }

      if (headResult.finalUrl && headResult.finalUrl !== input.url) {
        const rescored = this.scoreSync(linkLike, pageContext, {
          ...overrides,
          url: headResult.finalUrl,
          headResult,
        });
        return rescored;
      }

      return this.scoreSync(linkLike, pageContext, {
        ...overrides,
        headResult,
      });
    }
  }

  globalScope.DownloadScorer = DownloadScorer;
})(typeof globalThis !== "undefined" ? globalThis : window);
