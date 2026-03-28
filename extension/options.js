const apiBaseInput = document.getElementById("apiBase");
const saveButton = document.getElementById("save");
const status = document.getElementById("status");

chrome.storage.sync.get({ apiBase: "http://127.0.0.1:8765" }, (result) => {
  apiBaseInput.value = result.apiBase;
});

saveButton?.addEventListener("click", () => {
  chrome.storage.sync.set({ apiBase: apiBaseInput.value.trim() }, () => {
    status.textContent = "Saved";
    setTimeout(() => {
      status.textContent = "";
    }, 1200);
  });
});