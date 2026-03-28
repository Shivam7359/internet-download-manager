// IDM v1.1 — injected_network_hooks.js — last updated 2026-03-28
(() => {
  const STREAM_RE = /\.(m3u8|mpd|mp4|webm)(\?|$)/i;

  const post = (payload) => {
    window.postMessage({ __idmBridge: true, payload }, "*");
  };

  const maybePost = (url, source) => {
    const value = String(url || "");
    if (!value) {
      return;
    }

    if (value.startsWith("blob:")) {
      post({ type: "stream", url: value, streamType: "blob", source });
      return;
    }

    if (!STREAM_RE.test(value)) {
      return;
    }

    let streamType = "direct";
    const lower = value.toLowerCase();
    if (lower.includes(".m3u8")) {
      streamType = "hls";
    } else if (lower.includes(".mpd")) {
      streamType = "dash";
    }

    post({ type: "stream", url: value, streamType, source });
  };

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async (...args) => {
      const reqUrl = typeof args[0] === "string" ? args[0] : (args[0]?.url || "");
      maybePost(reqUrl, "fetch:req");
      const response = await originalFetch(...args);
      maybePost(response?.url || reqUrl, "fetch:resp");
      return response;
    };
  }

  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try {
      this.__idmUrl = url;
    } catch (_) {
      // noop
    }
    maybePost(url, "xhr:open");
    return open.call(this, method, url, ...rest);
  };

  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      maybePost(this.responseURL || this.__idmUrl || "", "xhr:load");
    });
    return send.call(this, ...args);
  };

  const emitYouTube = () => {
    try {
      const data = window.ytInitialPlayerResponse;
      const title = data?.videoDetails?.title || document.title || "";
      const fmts = data?.streamingData?.formats || [];
      const adaptive = data?.streamingData?.adaptiveFormats || [];
      const merged = [...fmts, ...adaptive];

      let best = "";
      let bestScore = 0;
      for (const item of merged) {
        const url = item?.url || "";
        if (!url) {
          continue;
        }
        const score = Number(item?.bitrate || 0);
        if (score >= bestScore) {
          bestScore = score;
          best = url;
        }
      }

      if (best) {
        post({
          type: "stream",
          url: best,
          streamType: "direct",
          source: "youtube",
          title,
        });
      }
    } catch (_) {
      // noop
    }
  };

  emitYouTube();
  setTimeout(emitYouTube, 1400);
})();
