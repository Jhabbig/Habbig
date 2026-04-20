// Entity drill-in page. Fetches 4 API endpoints and renders them as
// four panels: history chart, recent spikes, related markets, recent
// posts. Reuses confidence-bar + blur handlers from annoyance.js'
// CSS, but the JS here is standalone so the entity page works
// independently.
//
// Sensitive posts are blurred by default (decision #14); click to
// reveal. Reveal choice persists via sessionStorage per post ID.

const BILLING_URL = "https://narve.ai/billing";

// The entity name comes from the URL: /entity/{name}
const ENTITY = decodeURIComponent(location.pathname.replace(/^\/entity\//, "").replace(/\/$/, ""));

async function fetchJSON(path) {
  try {
    const res = await fetch(path);
    if (res.status === 402) {
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

function esc(s) {
  if (s == null) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch { return iso; }
}

function confTier(score) {
  if (score == null) return "conf-unknown";
  if (score >= 70) return "conf-high";
  if (score >= 40) return "conf-mid";
  return "conf-low";
}

// ── Title + summary ───────────────────────────────────────────────

function renderHeader() {
  document.title = `${ENTITY} — narve.ai`;
  document.getElementById("entity-name").textContent = ENTITY;
}

// ── History chart ─────────────────────────────────────────────────

let entityChart = null;

function renderHistory(payload) {
  const history = (payload && payload.history) || [];
  const emptyEl = document.getElementById("entity-chart-empty");
  const canvas = document.getElementById("entity-chart");
  if (!history.length) {
    emptyEl.classList.remove("hidden");
    if (entityChart) {
      entityChart.data.labels = [];
      entityChart.data.datasets[0].data = [];
      entityChart.update();
    }
    return;
  }
  emptyEl.classList.add("hidden");
  const labels = history.map((h) => {
    try { return new Date(h.hour).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit" }); }
    catch { return h.hour; }
  });
  const values = history.map((h) => (h.count || 0) * ((h.avg_annoyance || 0) / 50));

  if (!entityChart) {
    const ctx = canvas.getContext("2d");
    entityChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Signal",
          data: values,
          fill: true,
          borderColor: "#ffffff",
          backgroundColor: "rgba(255,255,255,0.06)",
          tension: 0.3,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: "#1a1a1a" }, ticks: { color: "#5a5a5a", maxRotation: 0, autoSkipPadding: 24 } },
          y: { beginAtZero: true, grid: { color: "#1a1a1a" }, ticks: { color: "#5a5a5a" } },
        },
      },
    });
  } else {
    entityChart.data.labels = labels;
    entityChart.data.datasets[0].data = values;
    entityChart.update();
  }
}

// ── Recent spikes for this entity ─────────────────────────────────

function renderSpikes(payload) {
  const list = document.getElementById("entity-spikes");
  const spikes = (payload && payload.spikes) || [];
  if (!spikes.length) {
    list.innerHTML = '<div class="empty-small">no spikes yet</div>';
    return;
  }
  list.innerHTML = spikes.map((s) => {
    const metric = s.z_score
      ? `z=${s.z_score.toFixed(1)} · ${s.multiple_of_baseline.toFixed(1)}×`
      : `warmup · ${s.count} posts`;
    const when = fmtTime(s.detected_at);
    const conf = s.confidence_score == null ? null : Math.round(s.confidence_score);
    const confBar = conf != null ? `
      <div class="conf-row" title="confidence ${conf}/100">
        <div class="conf-bar"><div class="conf-fill ${confTier(conf)}" style="width:${conf}%"></div></div>
        <div class="conf-label">${conf}</div>
      </div>` : "";
    const summary = s.summary || "spike detected — cause pending";
    return `
      <div class="spike-card">
        <div class="spike-top">
          <div class="spike-entity">${esc(when)}</div>
          <div class="spike-metric">${esc(metric)}</div>
        </div>
        ${confBar}
        <div class="spike-summary">${esc(summary)}</div>
      </div>`;
  }).join("");
}

// ── Markets panel ─────────────────────────────────────────────────

function renderMarkets(payload) {
  const panel = document.getElementById("entity-markets");
  const markets = (payload && payload.markets) || [];
  if (!markets.length) {
    panel.innerHTML = `
      <div class="market-empty">
        No curated markets for <strong>${esc(ENTITY)}</strong> yet.
        <a href="#" class="market-suggest">Suggest a market</a>
      </div>`;
    panel.querySelector(".market-suggest").addEventListener("click", onSuggest);
    return;
  }
  panel.innerHTML = markets.map((m) => `
    <a class="market-entry" href="${esc(m.url)}" target="_blank" rel="noopener noreferrer">
      <span class="market-title">${esc(m.title || "Open →")}</span>
      <span class="market-source">${esc(m.source || "")}</span>
    </a>`).join("");
}

async function onSuggest(event) {
  event.preventDefault();
  const url = prompt(`Paste a narve.ai market URL for "${ENTITY}"`, "https://narve.ai/markets/");
  if (!url) return;
  await fetchJSON("/api/market-suggestions");  // GET for ping (will hit the POST path below via separate call)
  // Actual submission:
  try {
    await fetch("/api/market-suggestions", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ entity: ENTITY, url }),
    });
    event.target.replaceWith(document.createTextNode("thanks — we'll review it"));
  } catch { /* swallow */ }
}

// ── Recent posts ──────────────────────────────────────────────────

function renderPosts(payload) {
  const list = document.getElementById("entity-posts");
  const posts = (payload && payload.posts) || [];
  if (!posts.length) {
    list.innerHTML = '<div class="empty-small">no recent classified posts</div>';
    return;
  }
  list.innerHTML = posts.map((p) => {
    const body = (p.content || "").slice(0, 280);
    const hot = (p.annoyance_score || 0) >= 70;
    const source = p.source || "";
    const posted = fmtTime(p.posted_at);
    const link = p.url
      ? `<a class="post-link" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">↗</a>`
      : "";
    const revealKey = `post-reveal-${p.post_id}`;
    const preRevealed = p.is_sensitive && sessionStorage.getItem(revealKey) === "1";
    const bodyHTML = p.is_sensitive
      ? `<div class="blur-wrap ${preRevealed ? "revealed" : ""}" data-sensitive="true" data-reveal-key="${revealKey}">
           <div class="blur-content">${esc(body)}</div>
           <div class="blur-hint">click to reveal sensitive content</div>
         </div>`
      : esc(body);
    return `
      <div class="post-row">
        <div class="post-source" title="${esc(posted)}">${esc(source)}</div>
        <div class="post-body">${bodyHTML}</div>
        <div class="post-annoyance${hot ? " hot" : ""}">${
          p.annoyance_score == null ? "—" : Math.round(p.annoyance_score)
        }</div>
        ${link}
      </div>`;
  }).join("");

  list.querySelectorAll(".blur-wrap[data-sensitive='true']").forEach((el) => {
    el.addEventListener("click", () => {
      el.classList.add("revealed");
      const k = el.dataset.revealKey;
      if (k) { try { sessionStorage.setItem(k, "1"); } catch { /* ignore */ } }
    });
  });
}

// ── Bootstrap ─────────────────────────────────────────────────────

(async function init() {
  renderHeader();
  const [hist, spikes, markets, posts] = await Promise.all([
    fetchJSON(`/api/entity/${encodeURIComponent(ENTITY)}`),
    fetchJSON(`/api/entity/${encodeURIComponent(ENTITY)}/spikes`),
    fetchJSON(`/api/entity/${encodeURIComponent(ENTITY)}/markets`),
    fetchJSON(`/api/entity/${encodeURIComponent(ENTITY)}/recent-posts`),
  ]);
  renderHistory(hist);
  renderSpikes(spikes);
  renderMarkets(markets);
  renderPosts(posts);

  // Summary line
  const spikeCount = spikes && spikes.spikes ? spikes.spikes.length : 0;
  const postCount = posts && posts.posts ? posts.posts.length : 0;
  document.getElementById("entity-summary").textContent =
    `${spikeCount} recent spikes · ${postCount} recent posts · 7d history shown above`;
})();
