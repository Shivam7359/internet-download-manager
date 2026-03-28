// IDM v1.1 — options.js — last updated 2026-03-28
const DEFAULT_SETTINGS = {
  autoCaptureDownloads: true,
  fileTypes: ["zip", "rar", "7z", "tar", "gz", "exe", "msi", "pdf", "mp4", "mp3", "mkv", "avi", "webm", "m3u8", "mpd"],
  minFileSizeKb: 0,
  showInlineIdmButtons: true,
  videoStreamDetection: true,
  defaultSaveCategory: "Auto",
  bridgePort: 6800,
  downloadScoreThreshold: 60,
  showUncertainLinks: false,
  scorerDebugMode: false,
};

const state = {
  settings: { ...DEFAULT_SETTINGS },
  auth: { host: "localhost", port: 6800, token: "" },
  fileTypes: [],
};

const el = {
  nav: document.getElementById("sidebarNav"),
  status: document.getElementById("status"),
  host: document.getElementById("host"),
  port: document.getElementById("port"),
  pairCode: document.getElementById("pairCode"),
  pairNow: document.getElementById("pairNow"),
  pairDot: document.getElementById("pairDot"),
  pairState: document.getElementById("pairState"),
  autoCaptureSwitch: document.getElementById("autoCaptureSwitch"),
  autoCapture: document.getElementById("autoCapture"),
  minFileSizeKb: document.getElementById("minFileSizeKb"),
  newExt: document.getElementById("newExt"),
  addExt: document.getElementById("addExt"),
  extTags: document.getElementById("extTags"),
  inlineSwitch: document.getElementById("inlineSwitch"),
  inlineButtons: document.getElementById("inlineButtons"),
  streamSwitch: document.getElementById("streamSwitch"),
  streamDetect: document.getElementById("streamDetect"),
  uncertainSwitch: document.getElementById("uncertainSwitch"),
  uncertainLinks: document.getElementById("uncertainLinks"),
  scorerDebugSwitch: document.getElementById("scorerDebugSwitch"),
  scorerDebugMode: document.getElementById("scorerDebugMode"),
  scoreThreshold: document.getElementById("scoreThreshold"),
  scoreThresholdValue: document.getElementById("scoreThresholdValue"),
  defaultCategory: document.getElementById("defaultCategory"),
  saveAll: document.getElementById("saveAll"),
};

function setStatus(message, isError = false) {
  if (!el.status) return;
  el.status.textContent = message;
  el.status.classList.toggle("bad", Boolean(isError));
  el.status.classList.toggle("good", !isError);
}

function toMessage(type, payload = {}) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, ...payload }, (response) => {
      resolve(response || { ok: false, error: "No response" });
    });
  });
}

function normalizeExt(value) {
  return String(value || "").trim().toLowerCase().replace(/^\./, "").replace(/[^a-z0-9]/g, "");
}

function updateSwitchElement(wrapper, input, on) {
  if (input) input.checked = Boolean(on);
  if (wrapper) wrapper.setAttribute("data-on", on ? "true" : "false");
}

function bindSwitch(wrapper, input) {
  const apply = (on) => updateSwitchElement(wrapper, input, on);
  wrapper?.addEventListener("click", () => apply(!input.checked));
  input?.addEventListener("change", () => apply(input.checked));
}

function renderFileTags() {
  if (!el.extTags) return;
  el.extTags.innerHTML = state.fileTypes.map((ext) => (
    `<span class="tag">.${ext}<button type="button" data-ext="${ext}" aria-label="Remove ${ext}">✕</button></span>`
  )).join("");

  el.extTags.querySelectorAll("button[data-ext]").forEach((button) => {
    button.addEventListener("click", () => {
      const ext = button.getAttribute("data-ext") || "";
      state.fileTypes = state.fileTypes.filter((v) => v !== ext);
      renderFileTags();
    });
  });
}

function setPairState(paired) {
  el.pairDot?.classList.toggle("good", Boolean(paired));
  if (el.pairState) {
    el.pairState.textContent = paired ? "Paired" : "Not paired";
  }
}

function readFormIntoState() {
  state.auth.host = String(el.host?.value || "localhost").trim() || "localhost";
  state.auth.port = Number(el.port?.value || DEFAULT_SETTINGS.bridgePort) || DEFAULT_SETTINGS.bridgePort;

  state.settings.autoCaptureDownloads = Boolean(el.autoCapture?.checked);
  state.settings.minFileSizeKb = Math.max(0, Number(el.minFileSizeKb?.value || 0));
  state.settings.showInlineIdmButtons = Boolean(el.inlineButtons?.checked);
  state.settings.videoStreamDetection = Boolean(el.streamDetect?.checked);
  state.settings.showUncertainLinks = Boolean(el.uncertainLinks?.checked);
  state.settings.scorerDebugMode = Boolean(el.scorerDebugMode?.checked);
  state.settings.downloadScoreThreshold = Math.max(40, Math.min(80, Number(el.scoreThreshold?.value || 60)));
  state.settings.defaultSaveCategory = String(el.defaultCategory?.value || "Auto");
  state.settings.fileTypes = [...state.fileTypes];
}

function renderStateToForm() {
  if (el.host) el.host.value = state.auth.host;
  if (el.port) el.port.value = String(state.auth.port);

  updateSwitchElement(el.autoCaptureSwitch, el.autoCapture, state.settings.autoCaptureDownloads);
  if (el.minFileSizeKb) el.minFileSizeKb.value = String(state.settings.minFileSizeKb || 0);

  updateSwitchElement(el.inlineSwitch, el.inlineButtons, state.settings.showInlineIdmButtons !== false);
  updateSwitchElement(el.streamSwitch, el.streamDetect, state.settings.videoStreamDetection !== false);
  updateSwitchElement(el.uncertainSwitch, el.uncertainLinks, state.settings.showUncertainLinks === true);
  updateSwitchElement(el.scorerDebugSwitch, el.scorerDebugMode, state.settings.scorerDebugMode === true);

  const threshold = Math.max(40, Math.min(80, Number(state.settings.downloadScoreThreshold || 60)));
  if (el.scoreThreshold) el.scoreThreshold.value = String(threshold);
  if (el.scoreThresholdValue) el.scoreThresholdValue.textContent = String(threshold);

  if (el.defaultCategory) el.defaultCategory.value = String(state.settings.defaultSaveCategory || "Auto");
  renderFileTags();
  setPairState(Boolean(state.auth.token));
}

async function loadInitialState() {
  const [settingsData, authData] = await Promise.all([
    chrome.storage.sync.get("settings"),
    chrome.storage.local.get("bridgeAuth"),
  ]);

  state.settings = {
    ...DEFAULT_SETTINGS,
    ...(settingsData.settings || {}),
  };

  state.auth = {
    host: String(authData.bridgeAuth?.host || "localhost").trim() || "localhost",
    port: Number(authData.bridgeAuth?.port || DEFAULT_SETTINGS.bridgePort) || DEFAULT_SETTINGS.bridgePort,
    token: String(authData.bridgeAuth?.token || "").trim(),
  };

  state.fileTypes = Array.from(new Set((Array.isArray(state.settings.fileTypes) ? state.settings.fileTypes : DEFAULT_SETTINGS.fileTypes)
    .map((v) => normalizeExt(v))
    .filter(Boolean)));

  renderStateToForm();
}

async function saveAll() {
  readFormIntoState();
  await chrome.storage.sync.set({ settings: state.settings });
  await chrome.storage.local.set({ bridgeAuth: state.auth });
  await toMessage("saveSettings", {
    settings: {
      ...state.settings,
      host: state.auth.host,
      port: state.auth.port,
    },
  });
  setStatus("Settings saved.");
}

async function pairNow() {
  const host = String(el.host?.value || "localhost").trim() || "localhost";
  const port = Number(el.port?.value || DEFAULT_SETTINGS.bridgePort) || DEFAULT_SETTINGS.bridgePort;
  const code = String(el.pairCode?.value || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (!code) {
    setStatus("Pairing code is required.", true);
    return;
  }

  setStatus("Pairing in progress...");
  const response = await toMessage("pairWithCode", {
    host,
    port,
    pairing_code: code,
  });

  if (!response.ok) {
    setPairState(false);
    setStatus(response.error || "Pairing failed.", true);
    return;
  }

  state.auth = { host, port, token: String(response.token || "") };
  await chrome.storage.local.set({ bridgeAuth: state.auth });
  if (el.pairCode) el.pairCode.value = "";
  setPairState(Boolean(state.auth.token));
  setStatus("Paired successfully.");
}

function bindTabNavigation() {
  const navButtons = Array.from(document.querySelectorAll("#sidebarNav button[data-tab]"));
  const sections = Array.from(document.querySelectorAll(".section"));

  const activate = (tab) => {
    navButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tab));
    sections.forEach((section) => {
      section.classList.toggle("active", section.id === `tab-${tab}`);
    });
  };

  navButtons.forEach((button) => {
    button.addEventListener("click", () => activate(button.dataset.tab || "general"));
  });
}

function bindEvents() {
  bindSwitch(el.autoCaptureSwitch, el.autoCapture);
  bindSwitch(el.inlineSwitch, el.inlineButtons);
  bindSwitch(el.streamSwitch, el.streamDetect);
  bindSwitch(el.uncertainSwitch, el.uncertainLinks);
  bindSwitch(el.scorerDebugSwitch, el.scorerDebugMode);

  el.scoreThreshold?.addEventListener("input", () => {
    const value = Math.max(40, Math.min(80, Number(el.scoreThreshold?.value || 60)));
    if (el.scoreThresholdValue) {
      el.scoreThresholdValue.textContent = String(value);
    }
  });

  el.addExt?.addEventListener("click", () => {
    const ext = normalizeExt(el.newExt?.value || "");
    if (!ext) return;
    if (!state.fileTypes.includes(ext)) {
      state.fileTypes.push(ext);
      state.fileTypes.sort();
      renderFileTags();
    }
    if (el.newExt) el.newExt.value = "";
  });

  el.newExt?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      el.addExt?.click();
    }
  });

  el.saveAll?.addEventListener("click", () => { saveAll().catch((err) => setStatus(err?.message || "Save failed", true)); });
  el.pairNow?.addEventListener("click", () => { pairNow().catch((err) => setStatus(err?.message || "Pairing failed", true)); });
}

(async function init() {
  bindTabNavigation();
  bindEvents();
  await loadInitialState();
  setStatus(state.auth.token ? "Extension is paired." : "Not paired. Enter code to pair.");
})();
