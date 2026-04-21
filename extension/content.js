// Runs on polymarket.com/event/* pages. Extracts the market slug from
// the URL, asks the background worker for the narve bundle, and injects
// a small overlay card. Re-runs on SPA URL changes because Polymarket
// is client-routed.

(() => {
  const OVERLAY_ID = "narve-extension-overlay";
  const LAST_SLUG = { value: null };

  function slugFromUrl(href) {
    try {
      const u = new URL(href);
      const m = u.pathname.match(/^\/event\/([^\/?#]+)/);
      return m ? decodeURIComponent(m[1]) : null;
    } catch (e) {
      return null;
    }
  }

  function renderOverlay(bundle, error) {
    let el = document.getElementById(OVERLAY_ID);
    if (!el) {
      el = document.createElement("div");
      el.id = OVERLAY_ID;
      document.body.appendChild(el);
    }
    if (error === "not_authenticated") {
      el.innerHTML = `
        <div class="narve-head">narve.ai</div>
        <div class="narve-empty">Connect the extension to see narve signals.</div>
        <a class="narve-cta" href="https://narve.ai/extension/auth" target="_blank">Connect narve →</a>`;
      el.dataset.state = "auth";
      return;
    }
    if (error === "rate_limited") {
      el.innerHTML = `
        <div class="narve-head">narve.ai</div>
        <div class="narve-empty">Slow down — rate limit hit. Try again in a minute.</div>`;
      el.dataset.state = "rate";
      return;
    }
    if (!bundle || typeof bundle !== "object") {
      el.innerHTML = `
        <div class="narve-head">narve.ai</div>
        <div class="narve-empty">No narve coverage for this market.</div>`;
      el.dataset.state = "empty";
      return;
    }
    const narveProb = toPct(bundle.betyc_yes_probability);
    const marketProb = toPct(bundle.market_yes_price);
    const edge = bundle.betyc_edge != null
      ? (bundle.betyc_edge * 100).toFixed(1) + "%"
      : "—";
    const conf = bundle.betyc_confidence || "—";
    const flag = bundle.risk_flag
      ? `<div class="narve-flag">${escapeHtml(bundle.risk_flag)}</div>`
      : "";
    const sources = Array.isArray(bundle.top_sources)
      ? bundle.top_sources.slice(0, 3).map((s) =>
          `<span class="narve-src">@${escapeHtml(s.handle || "")} · ${
            Math.round((s.credibility || 0) * 100)
          }</span>`,
        ).join("")
      : "";
    el.innerHTML = `
      <div class="narve-head">narve.ai signal</div>
      <div class="narve-row"><span>Market YES</span><b>${marketProb}</b></div>
      <div class="narve-row"><span>narve YES</span><b>${narveProb}</b></div>
      <div class="narve-row"><span>Edge</span><b>${edge}</b></div>
      <div class="narve-row"><span>Confidence</span><b>${escapeHtml(conf)}</b></div>
      <div class="narve-row"><span>Sources</span><b>${bundle.source_count ?? 0}</b></div>
      ${sources ? `<div class="narve-srcs">${sources}</div>` : ""}
      ${flag}
      <a class="narve-cta" href="https://narve.ai/markets/${encodeURIComponent(bundle.market_slug || "")}" target="_blank">Open on narve.ai →</a>`;
    el.dataset.state = "ok";
  }

  function toPct(x) {
    if (x == null) return "—";
    return (Number(x) * 100).toFixed(1) + "%";
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }

  async function refresh() {
    const slug = slugFromUrl(location.href);
    if (!slug) {
      const existing = document.getElementById(OVERLAY_ID);
      if (existing) existing.remove();
      LAST_SLUG.value = null;
      return;
    }
    if (slug === LAST_SLUG.value) return;
    LAST_SLUG.value = slug;
    try {
      const resp = await chrome.runtime.sendMessage({
        type: "getMarketBundle", slug,
      });
      renderOverlay(resp && resp.bundle, resp && resp.error);
    } catch (e) {
      // Service worker can go idle between messages — one retry.
      try {
        const resp = await chrome.runtime.sendMessage({
          type: "getMarketBundle", slug,
        });
        renderOverlay(resp && resp.bundle, resp && resp.error);
      } catch (e2) {
        renderOverlay(null, "fetch_failed");
      }
    }
  }

  // Re-run on history API navigations (Polymarket is SPA).
  const origPush = history.pushState;
  history.pushState = function () {
    origPush.apply(this, arguments);
    queueMicrotask(refresh);
  };
  window.addEventListener("popstate", refresh);
  window.addEventListener("load", refresh);
  refresh();
})();
