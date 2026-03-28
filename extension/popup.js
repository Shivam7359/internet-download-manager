const pairButton = document.getElementById("pair");
const statusLabel = document.getElementById("status");

pairButton?.addEventListener("click", async () => {
  statusLabel.textContent = "Connecting...";
  try {
    await chrome.runtime.sendMessage({ type: "IDM_PAIR_REQUEST" });
    statusLabel.textContent = "Pair request sent";
  } catch (error) {
    statusLabel.textContent = "Failed to connect";
    console.error("Pair request failed", error);
  }
});