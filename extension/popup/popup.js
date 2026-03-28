// IDM v1.1 — popup.js — last updated 2026-03-28
const ui = {
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  openOptions: document.getElementById("openOptions"),
  scanBatch: document.getElementById("scanBatch"),
  detectedCount: document.getElementById("detectedCount"),
  activeList: document.getElementById("activeList"),
  pauseAll: document.getElementById("pauseAll"),
  resumeAll: document.getElementById("resumeAll"),
  speedLimit: document.getElementById("speedLimit"),
  speedText: document.getElementById("speedText"),
  applySpeed: document.getElementById("applySpeed"),
  captureStateTitle: document.getElementById("captureStateTitle"),
  captureStateDetail: document.getElementById("captureStateDetail"),
  toggleCapturePause: document.getElementById("toggleCapturePause"),
};

let queuePollTimer = null;
let detectedLinks = 0;
let captureStatePollTimer = null;
let captureCountdownTimer = null;
let latestCaptureState = null;
const IDM_OFFLINE_MESSAGE = "IDM not running";

function msg(type, payload = {}) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, ...payload }, (res) => {
      resolve(res || { ok: false, error: "No response" });
    });
  });
}

function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (!n || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const idx = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  const value = n / Math.pow(1024, idx);
  return `${value.toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`;
}

function formatEta(seconds) {
  const n = Number(seconds || 0);
  if (!n || !Number.isFinite(n) || n <= 0) return "-";
  if (n < 60) return `${Math.round(n)}s`;
  if (n < 3600) return `${Math.floor(n / 60)}m ${Math.round(n % 60)}s`;
  return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
}

function setConnection(connected, message = "") {
  if (ui.statusDot) {
    ui.statusDot.classList.remove("good", "bad");
    ui.statusDot.classList.add(connected ? "good" : "bad");
  }
  if (ui.statusText) {
    ui.statusText.textContent = connected ? "Connected" : (message || IDM_OFFLINE_MESSAGE);
  }
}

function updateDetectedCount(count) {
  detectedLinks = Math.max(0, Number(count || 0));
  if (ui.detectedCount) {
    ui.detectedCount.textContent = `${detectedLinks}`;
    ui.detectedCount.title = `${detectedLinks} files detected on this page`;
  }
}

function formatCountdown(ms) {
  const totalSeconds = Math.max(0, Math.floor(Number(ms || 0) / 1000));
  const mins = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function renderCaptureState(state) {
  if (!state) {
    return;
  }
  latestCaptureState = state;

  const mode = String(state.mode || "offline");
  const emoji = mode === "active" ? "🟢" : mode === "offline" ? "🔴" : mode === "paused" ? "🟡" : "⚪";
  const remainingMs = mode === "paused"
    ? Math.max(0, Number(state.pausedUntil || 0) - Date.now())
    : 0;

  if (ui.captureStateTitle) {
    let title = `${emoji} ${String(state.title || "Capture State")}`;
    if (mode === "active") {
      title += " — Browser downloads are redirected to IDM";
    }
    if (mode === "paused") {
      title += ` — Resuming in ${formatCountdown(remainingMs)}`;
    }
    ui.captureStateTitle.textContent = title;
  }

  if (ui.captureStateDetail) {
    ui.captureStateDetail.textContent = String(state.detail || "");
  }

  if (ui.toggleCapturePause) {
    if (mode === "disabled") {
      ui.toggleCapturePause.disabled = true;
      ui.toggleCapturePause.textContent = "Enable auto-capture in settings";
    } else if (mode === "paused") {
      ui.toggleCapturePause.disabled = false;
      ui.toggleCapturePause.textContent = "Resume IDM capture now";
    } else {
      ui.toggleCapturePause.disabled = false;
      ui.toggleCapturePause.textContent = "Pause IDM capture for 5 min";
    }
  }
}

async function refreshCaptureState() {
  const response = await msg("getCaptureBlockingStatus");
  if (!response.ok || !response.status) {
    return;
  }
  renderCaptureState(response.status);
}

function renderEmpty() {
  if (!ui.activeList) return;
  ui.activeList.innerHTML = '<div class="empty">No active downloads</div>';
}

async function queueAction(id, action) {
  await msg("queueAction", { id, action });
  await refreshQueue();
}

function renderQueue(items) {
  if (!ui.activeList) return;
  if (!Array.isArray(items) || !items.length) {
    renderEmpty();
    return;
  }

  ui.activeList.innerHTML = items.map((item) => {
    const progress = Math.max(0, Math.min(100, Number(item.progress_percent || 0)));
    const speed = Number(item.speed || 0);
    const speedLabel = speed > 0 ? `${formatBytes(speed)}/s` : "-";
    const filename = String(item.filename || item.url || "unnamed").replace(/[<>"]/g, "");
    const id = String(item.id || "");
    const status = String(item.status || "queued");
    return `
      <article class="item">
        <div class="item-top">
          <div class="name" title="${filename}">${filename}</div>
          <div class="actions">
            <button class="action" data-id="${id}" data-action="pause" title="Pause">❚❚</button>
            <button class="action" data-id="${id}" data-action="resume" title="Resume">▶</button>
            <button class="action" data-id="${id}" data-action="cancel" title="Cancel">✕</button>
          </div>
        </div>
        <div class="mini-bar"><div class="mini-fill" style="width:${progress}%;"></div></div>
        <div class="meta">${status} · ${progress.toFixed(1)}% · ${speedLabel} · ETA ${formatEta(item.eta_seconds)}</div>
      </article>
    `;
  }).join("");

  ui.activeList.querySelectorAll("button[data-id][data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      await queueAction(button.dataset.id || "", button.dataset.action || "");
    });
  });
}

async function refreshQueue() {
  const health = await msg("pingBridge");
  if (!health.ok) {
    setConnection(false, IDM_OFFLINE_MESSAGE);
    renderEmpty();
    return;
  }
  setConnection(true);

  const response = await msg("queueGet");
  if (!response.ok) {
    renderEmpty();
    return;
  }

  const downloads = Array.isArray(response.data?.downloads) ? response.data.downloads : [];
  const visible = downloads.filter((d) => {
    const status = String(d.status || "");
    return ["downloading", "queued", "paused", "failed"].includes(status);
  });
  renderQueue(visible);
}

async function scanActiveTab() {
  const res = await msg("scanActiveTab");
  if (!res.ok) {
    updateDetectedCount(0);
    return;
  }
  const items = Array.isArray(res.items) ? res.items : [];
  updateDetectedCount(items.length);
  if (items.length) {
    await msg("batchDownload", {
      urls: items.map((i) => String(i.url || "")).filter(Boolean),
      category: "Auto",
      save_path: "",
    });
    await refreshQueue();
  }
}

function updateSpeedLabel(value) {
  const limit = Number(value || 0);
  if (!ui.speedText) return;
  ui.speedText.textContent = limit <= 0 ? "Unlimited" : `${limit} KB/s`;
}

async function applySpeedLimit() {
  const value = Number(ui.speedLimit?.value || 0);
  await msg("setSpeedLimit", { limit_kbps: value });
  updateSpeedLabel(value);
}

function bind() {
  ui.openOptions?.addEventListener("click", () => chrome.runtime.openOptionsPage());
  ui.scanBatch?.addEventListener("click", () => { scanActiveTab().catch(() => {}); });
  ui.pauseAll?.addEventListener("click", async () => {
    await msg("queuePauseAll");
    await refreshQueue();
  });
  ui.resumeAll?.addEventListener("click", async () => {
    await msg("queueResumeAll");
    await refreshQueue();
  });
  ui.speedLimit?.addEventListener("input", () => updateSpeedLabel(ui.speedLimit.value));
  ui.applySpeed?.addEventListener("click", () => { applySpeedLimit().catch(() => {}); });
  ui.toggleCapturePause?.addEventListener("click", async () => {
    const response = await msg("toggleCapturePause");
    if (response.ok && response.status) {
      renderCaptureState(response.status);
    } else {
      await refreshCaptureState();
    }
  });
}

async function init() {
  bind();
  updateSpeedLabel(0);
  await refreshQueue();
  await refreshCaptureState();
  const scan = await msg("scanActiveTab");
  updateDetectedCount(Array.isArray(scan.items) ? scan.items.length : 0);

  queuePollTimer = setInterval(() => {
    refreshQueue().catch(() => {});
  }, 2000);

  captureStatePollTimer = setInterval(() => {
    refreshCaptureState().catch(() => {});
  }, 3000);

  captureCountdownTimer = setInterval(() => {
    if (latestCaptureState?.mode === "paused") {
      renderCaptureState(latestCaptureState);
    }
  }, 1000);
}

window.addEventListener("unload", () => {
  if (queuePollTimer) {
    clearInterval(queuePollTimer);
    queuePollTimer = null;
  }
  if (captureStatePollTimer) {
    clearInterval(captureStatePollTimer);
    captureStatePollTimer = null;
  }
  if (captureCountdownTimer) {
    clearInterval(captureCountdownTimer);
    captureCountdownTimer = null;
  }
});

init().catch(() => {
  setConnection(false, IDM_OFFLINE_MESSAGE);
});
