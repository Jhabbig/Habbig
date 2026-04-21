// Popup status + connect UI. Reads auth state from the background
// worker and swaps the CTA accordingly.

(async () => {
  const statusEl = document.getElementById("status");
  const connectEl = document.getElementById("connect");
  const dashboardEl = document.getElementById("dashboard");

  try {
    const resp = await chrome.runtime.sendMessage({ type: "getStatus" });
    const ok = resp && resp.authenticated;
    if (ok) {
      statusEl.textContent = "Connected to narve.ai.";
      statusEl.className = "status ok";
      connectEl.style.display = "none";
      dashboardEl.style.display = "block";
    } else {
      statusEl.textContent = "Not connected.";
      statusEl.className = "status warn";
    }
  } catch (e) {
    statusEl.textContent = "Couldn't reach the extension worker.";
    statusEl.className = "status err";
  }
})();
