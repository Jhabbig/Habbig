// Annoyance dashboard — vanilla JS, Chart.js from CDN. No build step.
// Polls /api/* every 60s and repaints the four panels.
//
// Polish layer (P8) adds:
//   * 402 → redirect to billing (hard-paywall per DECISIONS.md #4)
//   * Confidence bar on spike cards (green / amber / red tiered)
//   * Blur sensitive sample excerpts until the user clicks to reveal
//   * ⚑ flag button → POST /api/fp-flag (FP feedback, decision #11)
//   * "▸ View related markets" expands a per-spike market list
//   * Entity names in spike cards link to /entity/<name>
//   * Paywall banner for unauthenticated users

const REFRESH_MS = 60_000;
const BILLING_URL = "https://narve.ai/billing";
const CATEGORY_ANNOYANCE_HOT = 70;

let chart = null;
let authenticated = false;  // hydrated by /api/me on load
const _marketCache = {};    // entity → markets[]

// ── Paywall-aware fetch wrapper ───────────────────────────────────

async function fetchJSON(path, init) {
  try {
    const res = await fetch(path, init);
    if (res.status === 402) {
      // Hard paywall (decision #4): the user isn't Pro and this route
      // requires it. Redirect to billing instead of silently failing.
      window.location.href = BILLING_URL;
      return null;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (e) {
    console.warn("fetch failed:", path, e);
    return null;
  }
}

// ── Formatting helpers ────────────────────────────────────────────

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function confidenceTier(score) {
  if (score == null) return "conf-unknown";
  if (score >= 70) return "conf-high";
  if (score >= 40) return "conf-mid";
  return "conf-low";
}

// ── Renderers ─────────────────────────────────────────────────────

function renderIndex(payload) {
  const data = (payload && payload.hours) || [];
  const valueEl = document.getElementById("index-value");
  const metaEl = document.getElementById("index-meta");
  const emptyEl = document.getElementById("chart-empty");

  if (!data.length) {
    valueEl.textContent = "—";
    metaEl.textContent = "collecting data…";
    emptyEl.classList.remove("hidden");
    if (chart) {
      chart.data.labels = [];
      chart.data.datasets[0].data = [];
      chart.update();
    }
    return;
  }

  const latest = data[data.length - 1];
  valueEl.textContent = Math.round(latest.score);
  metaEl.textContent = `${latest.post_count} posts in ${fmtTime(latest.hour)} bucket`;
  emptyEl.classList.add("hidden");

  const labels = data.map((d) => fmtTime(d.hour));
  const values = data.map((d) => d.score);

  if (!chart) {
    const ctx = document.getElementById("index-chart").getContext("2d");
    chart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Annoyance index",
          data: values,
          fill: true,
          borderColor: "#ffffff",
          backgroundColor: "rgba(255,255,255,0.08)",
          tension: 0.35,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#141414",
            borderColor: "#262626",
            borderWidth: 1,
            titleColor: "#ffffff",
            bodyColor: "#8a8a8a",
          },
        },
        scales: {
          x: {
            grid: { color: "#1a1a1a", drawBorder: false },
            ticks: { color: "#5a5a5a", maxRotation: 0, autoSkipPadding: 24 },
          },
          y: {
            beginAtZero: true,
            max: 100,
            grid: { color: "#1a1a1a", drawBorder: false },
            ticks: { color: "#5a5a5a" },
          },
        },
      },
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    chart.update();
  }
}

function renderSpikes(payload) {
  const list = document.getElementById("spikes-list");
  const spikes = (payload && payload.spikes) || [];
  if (!spikes.length) {
    list.innerHTML = '<div class="empty-small">no spikes detected yet</div>';
    return;
  }
  list.innerHTML = spikes.map(spikeCardHTML).join("");
  // Attach per-card handlers once; event delegation on the parent is
  // slightly nicer but this is fine for ~20 cards.
  list.querySelectorAll(".blur-wrap[data-sensitive='true']").forEach((el) => {
    el.addEventListener("click", onBlurReveal);
  });
  list.querySelectorAll(".flag-btn").forEach((el) => {
    el.addEventListener("click", onFlagClick);
  });
  list.querySelectorAll(".market-toggle").forEach((el) => {
    el.addEventListener("click", onMarketToggle);
  });
}

function spikeCardHTML(s) {
  const metric = s.z_score
    ? `z=${s.z_score.toFixed(1)} · ${s.multiple_of_baseline.toFixed(1)}× baseline`
    : `warmup · ${s.count} posts`;
  const summary = s.summary || "spike detected — cause pending";
  const firstSample = (s.sample_posts && s.sample_posts[0]) || null;
  const fallbackExcerpt = (s.sample_excerpts && s.sample_excerpts[0]) || "";
  const sampleText = firstSample
    ? (firstSample.content || fallbackExcerpt)
    : fallbackExcerpt;
  const isSensitive = firstSample ? !!firstSample.is_sensitive : false;
  // Preserve per-session reveal choice so the blur doesn't re-cover on
  // the 60s refresh repaint.
  const revealKey = `spike-reveal-${s.id}`;
  const preRevealed = isSensitive && sessionStorage.getItem(revealKey) === "1";
  const confidence = (s.confidence_score == null) ? null : Math.round(s.confidence_score);
  const tierClass = confidenceTier(confidence);
  const entityLink = `/entity/${encodeURIComponent(s.entity)}`;
  const hasMarkets = true;  // unknown until expanded; always show the toggle

  // Sample block: if the post is sensitive, wrap it in the blur container
  // so the user can click-to-reveal. Non-sensitive samples render plain.
  let sampleBlock = "";
  if (sampleText) {
    const esc = escapeHtml(sampleText.slice(0, 180));
    const quoted = `&ldquo;${esc}&rdquo;`;
    if (isSensitive) {
      const cls = preRevealed ? "blur-wrap revealed" : "blur-wrap";
      sampleBlock = `<div class="${cls}" data-sensitive="true" data-reveal-key="${revealKey}">
        <div class="blur-content">${quoted}</div>
        <div class="blur-hint">click to reveal sensitive content</div>
      </div>`;
    } else {
      sampleBlock = `<div class="spike-sample">${quoted}</div>`;
    }
  }

  const flagBtn = authenticated
    ? `<button class="flag-btn" data-spike-id="${s.id}" title="Flag as false positive">⚑ flag</button>`
    : "";

  return `
    <div class="spike-card" data-entity="${escapeHtml(s.entity)}" data-spike-id="${s.id}">
      <div class="spike-top">
        <a class="spike-entity" href="${entityLink}">${escapeHtml(s.entity)}</a>
        <div class="spike-metric">${escapeHtml(metric)}</div>
      </div>
      ${confidence != null ? `
        <div class="conf-row" title="confidence: ${confidence} / 100">
          <div class="conf-bar"><div class="conf-fill ${tierClass}" style="width:${confidence}%"></div></div>
          <div class="conf-label">${confidence}</div>
        </div>` : ""}
      <div class="spike-summary">${escapeHtml(summary)}</div>
      ${sampleBlock}
      ${hasMarkets ? `<button class="market-toggle" data-entity="${escapeHtml(s.entity)}" data-spike-id="${s.id}">▸ View related markets</button>
      <div class="market-expand" id="markets-${s.id}" hidden></div>` : ""}
      ${flagBtn}
    </div>
  `;
}

function renderEntities(payload) {
  const list = document.getElementById("entities-list");
  const entities = (payload && payload.entities) || [];
  if (!entities.length) {
    list.innerHTML = '<div class="empty-small">no entity data yet</div>';
    return;
  }
  list.innerHTML = entities.map((e) => {
    const hot = e.avg_annoyance >= CATEGORY_ANNOYANCE_HOT;
    const link = `/entity/${encodeURIComponent(e.entity)}`;
    return `
      <a class="entity-row" href="${link}">
        <div class="entity-name">${escapeHtml(e.entity)}</div>
        <div class="entity-count">${e.count} posts</div>
        <div class="entity-score${hot ? " hot" : ""}">${Math.round(e.avg_annoyance)}</div>
      </a>
    `;
  }).join("");
}

function renderSources(payload) {
  const pill = document.getElementById("source-health");
  const sources = (payload && payload.sources) || [];
  if (!sources.length) {
    pill.textContent = "sources: —";
    pill.className = "source-pill";
    return;
  }
  const ok = sources.filter((s) => s.last_ok === 1).length;
  const total = sources.length;
  pill.textContent = `sources: ${ok}/${total} ok`;
  pill.className = "source-pill " + (ok === total ? "ok" : ok > 0 ? "warn" : "err");
}

// ── Interaction handlers ──────────────────────────────────────────

function onBlurReveal(event) {
  const wrap = event.currentTarget;
  wrap.classList.add("revealed");
  const key = wrap.dataset.revealKey;
  if (key) {
    try { sessionStorage.setItem(key, "1"); } catch { /* quota: ignore */ }
  }
}

function onFlagClick(event) {
  event.stopPropagation();
  const btn = event.currentTarget;
  const spikeId = btn.dataset.spikeId;
  if (!spikeId) return;
  openFlagModal(spikeId);
}

async function onMarketToggle(event) {
  event.stopPropagation();
  const btn = event.currentTarget;
  const entity = btn.dataset.entity;
  const spikeId = btn.dataset.spikeId;
  const panel = document.getElementById(`markets-${spikeId}`);
  if (!panel) return;
  if (!panel.hasAttribute("hidden")) {
    // Collapse
    panel.setAttribute("hidden", "");
    btn.textContent = "▸ View related markets";
    return;
  }
  // Expand — fetch (with cache) + render up to 3 entries.
  btn.textContent = "… loading";
  let markets = _marketCache[entity];
  if (markets === undefined) {
    const data = await fetchJSON(`/api/entity/${encodeURIComponent(entity)}/markets`);
    markets = (data && data.markets) || [];
    _marketCache[entity] = markets;
  }
  if (markets.length === 0) {
    panel.innerHTML = `
      <div class="market-empty">
        No curated markets yet for <strong>${escapeHtml(entity)}</strong>.
        <a href="#" class="market-suggest" data-entity="${escapeHtml(entity)}">Suggest one</a>
      </div>`;
    panel.querySelector(".market-suggest").addEventListener("click", onMarketSuggest);
  } else {
    panel.innerHTML = markets.slice(0, 3).map((m) => `
      <a class="market-entry" href="${escapeHtml(m.url)}" target="_blank" rel="noopener noreferrer">
        <span class="market-title">${escapeHtml(m.title || "Open →")}</span>
        <span class="market-source">${escapeHtml(m.source || "")}</span>
      </a>
    `).join("");
  }
  panel.removeAttribute("hidden");
  btn.textContent = "▾ Hide markets";
}

async function onMarketSuggest(event) {
  event.preventDefault();
  const a = event.currentTarget;
  const entity = a.dataset.entity;
  const url = prompt(`Paste a narve.ai market URL for "${entity}"`, "https://narve.ai/markets/");
  if (!url) return;
  await fetchJSON("/api/market-suggestions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ entity, url }),
  });
  a.replaceWith(document.createTextNode("thanks — we'll review it"));
}

// ── Flag modal ────────────────────────────────────────────────────

function openFlagModal(spikeId) {
  if (document.getElementById("flag-modal")) return;  // already open
  const backdrop = document.createElement("div");
  backdrop.id = "flag-modal";
  backdrop.className = "flag-modal-backdrop";
  backdrop.innerHTML = `
    <div class="flag-modal">
      <div class="flag-modal-title">Flag as false positive</div>
      <p class="flag-modal-sub">Why does this spike look wrong? Reviewers will see your note.</p>
      <textarea class="flag-modal-text" placeholder="Optional reason — up to 500 chars" maxlength="500"></textarea>
      <div class="flag-modal-actions">
        <button class="flag-cancel">Cancel</button>
        <button class="flag-submit">Submit flag</button>
      </div>
      <div class="flag-modal-result" hidden></div>
    </div>
  `;
  document.body.appendChild(backdrop);

  const close = () => backdrop.remove();
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });
  backdrop.querySelector(".flag-cancel").addEventListener("click", close);

  backdrop.querySelector(".flag-submit").addEventListener("click", async () => {
    const reason = backdrop.querySelector(".flag-modal-text").value.trim();
    const result = await fetchJSON("/api/fp-flag", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        target_type: "spike",
        target_id: String(spikeId),
        reason,
      }),
    });
    const resultEl = backdrop.querySelector(".flag-modal-result");
    resultEl.hidden = false;
    resultEl.textContent = (result && result.ok) ? "Thanks — flagged." : "Couldn't flag. Try again later.";
    setTimeout(close, 1200);
  });
}

// ── Paywall banner ────────────────────────────────────────────────

async function refreshAuth() {
  const me = await fetchJSON("/api/me");
  authenticated = !!(me && me.authenticated);
  const banner = document.getElementById("paywall-banner");
  if (banner) banner.hidden = authenticated;
}

// ── Main loop ─────────────────────────────────────────────────────

async function refresh() {
  const [idx, spikes, entities, sources] = await Promise.all([
    fetchJSON("/api/index?hours=24"),
    fetchJSON("/api/spikes?limit=10"),
    fetchJSON("/api/entities/top?limit=10"),
    fetchJSON("/api/sources"),
  ]);
  renderIndex(idx);
  renderSpikes(spikes);
  renderEntities(entities);
  renderSources(sources);

  // Repoll happiness data if that view is currently open.
  const happinessView = document.getElementById("happiness-view");
  if (happinessView && !happinessView.hasAttribute("hidden")) {
    refreshHappiness();
  }

  document.getElementById("last-update").textContent =
    "updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}



// ── Happiness view (decision #7 unlock, 2026-05-14) ───────────────
//
// Reuses spikeCardHTML for spike rendering, adds the `.positive` class
// for the thicker-border CSS treatment. Monochrome — no colour change.

function happinessSpikeCardHTML(s) {
  const inner = spikeCardHTML(s);
  return inner.replace('class="spike-card"', 'class="spike-card positive"');
}

function renderHappinessSpikes(payload) {
  const list = document.getElementById("happiness-spikes-list");
  if (!list) return;
  const spikes = (payload && payload.spikes) || [];
  if (!spikes.length) {
    list.innerHTML = '<div class="empty-small">no happiness spikes detected yet</div>';
    return;
  }
  list.innerHTML = spikes.map(happinessSpikeCardHTML).join("");
  list.querySelectorAll(".blur-wrap[data-sensitive='true']").forEach((el) => {
    el.addEventListener("click", onBlurReveal);
  });
  list.querySelectorAll(".flag-btn").forEach((el) => {
    el.addEventListener("click", onFlagClick);
  });
  list.querySelectorAll(".market-toggle").forEach((el) => {
    el.addEventListener("click", onMarketToggle);
  });

  const valueEl = document.getElementById("happiness-value");
  const metaEl = document.getElementById("happiness-meta");
  if (valueEl) {
    const latest = spikes[0];
    if (latest && latest.avg_annoyance != null) {
      valueEl.textContent = Math.round(latest.avg_annoyance);
      if (metaEl) metaEl.textContent = `${spikes.length} positive spikes in view`;
    } else {
      valueEl.textContent = spikes.length;
      if (metaEl) metaEl.textContent = "positive-polarity spikes (last 24h)";
    }
  }
}

function renderHappinessEntities(payload) {
  const list = document.getElementById("happiness-entities-list");
  if (!list) return;
  const entities = (payload && payload.entities) || [];
  if (!entities.length) {
    list.innerHTML = '<div class="empty-small">no positive entity data yet</div>';
    return;
  }
  list.innerHTML = entities.map((e) => {
    const link = `/entity/${encodeURIComponent(e.entity)}`;
    return `
      <a class="entity-row positive" href="${link}">
        <div class="entity-name">${escapeHtml(e.entity)}</div>
        <div class="entity-count">${e.positive_count} mentions</div>
        <div class="entity-score">${Math.round(e.avg_score)}</div>
      </a>
    `;
  }).join("");
}

async function refreshHappiness() {
  const [spikes, entities] = await Promise.all([
    fetchJSON("/api/happiness/spikes?limit=10"),
    fetchJSON("/api/happiness/entities?limit=10"),
  ]);
  renderHappinessSpikes(spikes);
  renderHappinessEntities(entities);
}

// ── View toggle (annoyance ↔ happiness) ───────────────────────────

function activateView(view) {
  const annoyance = document.getElementById("annoyance-view");
  const happiness = document.getElementById("happiness-view");
  const tabA = document.getElementById("tab-annoyance");
  const tabH = document.getElementById("tab-happiness");
  if (!annoyance || !happiness || !tabA || !tabH) return;

  if (view === "happiness") {
    annoyance.setAttribute("hidden", "");
    annoyance.classList.remove("view-active");
    happiness.removeAttribute("hidden");
    happiness.classList.add("view-active");
    tabA.classList.remove("tab-active");
    tabH.classList.add("tab-active");
    refreshHappiness();
  } else {
    happiness.setAttribute("hidden", "");
    happiness.classList.remove("view-active");
    annoyance.removeAttribute("hidden");
    annoyance.classList.add("view-active");
    tabH.classList.remove("tab-active");
    tabA.classList.add("tab-active");
  }
}

function wireTabHandlers() {
  document.querySelectorAll(".tab[data-view]").forEach((tab) => {
    tab.addEventListener("click", (e) => {
      e.preventDefault();
      const v = tab.dataset.view;
      if (!v) return;
      activateView(v);
      try { history.replaceState(null, "", `#${v}`); } catch { /* ignore */ }
    });
  });
}

(async function init() {
  // Auth first so spike cards know whether to show the flag button.
  await refreshAuth();
  wireTabHandlers();
  const initialView = (location.hash || "").replace(/^#/, "") === "happiness"
    ? "happiness" : "annoyance";
  activateView(initialView);
  await refresh();
  // Happiness data is fetched lazily inside activateView('happiness') and on
  // every refresh tick if the happiness view is currently open.
  setInterval(refresh, REFRESH_MS);
})();
