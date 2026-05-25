// Voters Atlas — slice 1 client.
//
// No framework, no build step. Vanilla DOM + fetch.
// All API responses come from the same origin (gateway proxies them).

(function () {
  "use strict";

  // ── Helpers ─────────────────────────────────────────────────────

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      if (c instanceof Node) {
        node.appendChild(c);
      } else {
        // Coerce numbers, booleans (true), etc. to text — keeps callers terse.
        node.appendChild(document.createTextNode(String(c)));
      }
    }
    return node;
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderMarkdownLite(s) {
    // Conservative markdown: links, bold, italic, code. Everything else escaped.
    const esc = escapeHtml(s);
    return esc
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  function relTime(ts) {
    const now = Math.floor(Date.now() / 1000);
    const d = now - ts;
    if (d < 60) return `${d}s ago`;
    if (d < 3600) return `${Math.floor(d / 60)}m ago`;
    if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
    if (d < 86400 * 30) return `${Math.floor(d / 86400)}d ago`;
    return new Date(ts * 1000).toISOString().slice(0, 10);
  }

  function daysUntil(dateStr) {
    if (!dateStr || dateStr.startsWith("TBD")) return null;
    const t = Date.parse(dateStr);
    if (Number.isNaN(t)) return null;
    return Math.floor((t - Date.now()) / 86400000);
  }

  function fmtCountdown(dateStr) {
    const d = daysUntil(dateStr);
    if (d == null) return "TBD";
    if (d < 0) return `${-d}d ago`;
    if (d < 90) return `in ${d}d`;
    if (d < 730) return `in ${Math.round(d / 30)}mo`;
    return `in ${Math.round(d / 365)}y`;
  }

  function showToast(msg, isError = false) {
    let t = $(".toast");
    if (!t) {
      t = el("div", { class: "toast" });
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.toggle("error", isError);
    requestAnimationFrame(() => t.classList.add("show"));
    setTimeout(() => t.classList.remove("show"), 2400);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (!res.ok) {
      let detail;
      try { detail = (await res.json()).detail; } catch { detail = res.statusText; }
      throw new Error(detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  // ── State ───────────────────────────────────────────────────────

  const state = {
    countriesSummary: [],
    countryDetails: new Map(),
    activeIso: null,
    myVotes: new Map(),     // thought_id -> -1 | 0 | 1
    myReactions: new Map(), // `${target_id}:${emoji}` -> thought_id
    me: null,               // {user_id, email, role}
    chainVotes: new Map(),  // chain_id -> -1 | 0 | 1
  };

  const REGIONS_ORDER = [
    "North America", "South America", "Europe",
    "Middle East", "Middle East / Africa", "Middle East / Europe",
    "Africa", "South Asia", "East Asia", "Southeast Asia", "Oceania",
  ];

  const REACTION_EMOJI = [
    { code: "thumbs_up",  glyph: "👍", label: "Agree" },
    { code: "thumbs_dn",  glyph: "👎", label: "Disagree" },
    { code: "source_q",   glyph: "❓", label: "Source?" },
    { code: "important",  glyph: "⭐", label: "Important" },
    { code: "wrong",      glyph: "🚩", label: "Misleading" },
  ];

  // ── Bootstrap ───────────────────────────────────────────────────

  async function bootstrap() {
    try {
      const [summary, calendar, me] = await Promise.all([
        api("/api/countries"),
        api("/api/elections/calendar?months=18"),
        api("/api/me").catch(() => null),
      ]);
      state.me = me;
      state.viewerEmail = me ? me.email : null;
      state.countriesSummary = summary.countries;
      $("#last-curated").textContent = summary.last_curated ? `Curated ${summary.last_curated}` : "";
      renderTimeline(calendar.items);
      renderGrid(summary.countries);
      renderTopbarRole();
    } catch (e) {
      $("#grid-root").innerHTML = "";
      $("#grid-root").appendChild(el("div", { class: "grid-loading" }, "Failed to load: " + e.message));
    }
  }

  function renderTopbarRole() {
    if (!state.me) return;
    const meta = $(".topbar-meta");
    if (!meta) return;
    // Avoid duplicates on re-render
    const existing = meta.querySelector(".role-pill");
    if (existing) existing.remove();
    const pill = el("span", { class: `role-pill role-${state.me.role}` }, state.me.role);
    meta.prepend(pill);
    if (state.me.role === "reviewer" || state.me.role === "admin") {
      const existingBtn = meta.querySelector(".reviewer-queue-btn");
      if (existingBtn) existingBtn.remove();
      meta.prepend(el("button", { class: "reviewer-queue-btn", onclick: openReviewerQueue }, "Review queue"));
    }
  }

  // ── Timeline ────────────────────────────────────────────────────

  function renderTimeline(items) {
    const root = $("#timeline");
    root.innerHTML = "";
    if (!items.length) {
      root.appendChild(el("div", { class: "hint" }, "No elections in window."));
      return;
    }

    // Group consecutive same-month items so the strip reads as a real timeline
    // rather than a long list of repeats. We keep at most 14 items visible —
    // anything beyond that the user reaches via the country grid.
    const visible = items.slice(0, 14);
    const monthOf = (d) => (d || "").slice(0, 7);
    let lastMonth = null;
    for (const it of visible) {
      const ym = monthOf(it.date);
      if (ym && ym !== lastMonth) {
        const monthLabel = el("div", { class: "tl-month" }, formatMonth(it.date));
        root.appendChild(monthLabel);
        lastMonth = ym;
      }
      const tile = el("div", {
        class: "tl-item",
        onclick: () => openCountry(it.iso),
        title: it.type || it.country,
      },
        el("div", { class: "tl-day" }, formatDay(it.date)),
        el("div", { class: "tl-country" }, it.country),
        el("div", { class: "tl-countdown" }, fmtCountdown(it.date)),
      );
      root.appendChild(tile);
    }
  }

  function formatMonth(dateStr) {
    if (!dateStr || dateStr.startsWith("TBD")) return "—";
    const d = new Date(dateStr + "T00:00:00Z");
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString("en", { month: "short", year: "2-digit", timeZone: "UTC" });
  }

  function formatDay(dateStr) {
    if (!dateStr || dateStr.startsWith("TBD")) return "TBD";
    const d = new Date(dateStr + "T00:00:00Z");
    if (isNaN(d.getTime())) return "TBD";
    return d.toLocaleString("en", { day: "numeric", month: "short", timeZone: "UTC" });
  }

  // ── Grid ────────────────────────────────────────────────────────

  function renderGrid(countries) {
    const root = $("#grid-root");
    root.innerHTML = "";

    // Group by region
    const byRegion = new Map();
    for (const c of countries) {
      if (!byRegion.has(c.region)) byRegion.set(c.region, []);
      byRegion.get(c.region).push(c);
    }
    // Order regions roughly geographically
    const ordered = REGIONS_ORDER.filter((r) => byRegion.has(r))
      .concat([...byRegion.keys()].filter((r) => !REGIONS_ORDER.includes(r)));

    for (const region of ordered) {
      const list = byRegion.get(region).slice().sort((a, b) => {
        if (a.tier !== b.tier) return a.tier.localeCompare(b.tier);
        return a.name.localeCompare(b.name);
      });
      const card = el("div", { class: "region" }, el("h3", {}, region));
      for (const c of list) card.appendChild(countryCard(c));
      root.appendChild(card);
    }
  }

  function countryCard(c) {
    const issueLine = c.top_issue
      ? el("div", { class: "country-issue" },
          el("span", { class: "pct" }, `${c.top_issue_pct ?? "?"}%`),
          c.top_issue,
        )
      : null;
    const electionLine = c.next_election
      ? el("div", { class: "country-election" },
          `Next vote: ${c.next_election} (${fmtCountdown(c.next_election)})`)
      : null;

    return el("div", { class: "country-card", onclick: () => openCountry(c.iso) },
      el("div", { class: "country-head" },
        el("span", { class: "country-name" }, c.name),
        el("span", { class: `tier-badge tier-${c.tier}` }, `Tier ${c.tier}`),
      ),
      el("div", { class: "country-meta" },
        c.population_m != null ? el("span", {}, `${c.population_m}M people`) : null,
        c.region ? el("span", {}, c.region) : null,
      ),
      issueLine,
      electionLine,
    );
  }

  // ── Drawer / country detail ─────────────────────────────────────

  async function openCountry(iso) {
    if (!iso) return;
    state.activeIso = iso;
    showDrawer();
    const body = $("#drawer-body");
    body.innerHTML = '<div class="grid-loading">Loading…</div>';

    let detail = state.countryDetails.get(iso);
    if (!detail) {
      try {
        detail = await api(`/api/country/${iso}`);
        state.countryDetails.set(iso, detail);
      } catch (e) {
        body.innerHTML = "";
        body.appendChild(el("p", {}, "Failed to load: " + e.message));
        return;
      }
    }
    renderDrawer(detail);
    await loadComments(iso);
  }

  function showDrawer() {
    $("#drawer").classList.remove("drawer-closed");
    $("#drawer").setAttribute("aria-hidden", "false");
    $("#drawer-scrim").classList.add("show");
  }
  function hideDrawer() {
    $("#drawer").classList.add("drawer-closed");
    $("#drawer").setAttribute("aria-hidden", "true");
    $("#drawer-scrim").classList.remove("show");
    state.activeIso = null;
  }
  $("#drawer-close").addEventListener("click", hideDrawer);
  $("#drawer-scrim").addEventListener("click", hideDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideDrawer(); });

  function renderDrawer(c) {
    const body = $("#drawer-body");
    body.innerHTML = "";

    body.appendChild(el("h2", {}, c.name));
    const subParts = [
      c.region,
      `Tier ${c.tier}`,
      c.population_m != null ? `${c.population_m}M people` : null,
      c.median_age != null ? `median age ${c.median_age}` : null,
    ].filter(Boolean);
    body.appendChild(el("div", { class: "subtle" }, subParts.join(" · ")));

    // Current leader (most-glanced; goes right under the header)
    const pol = c._political || {};
    if (pol.leader) {
      body.appendChild(leaderSection(pol.leader, pol.parties || []));
    }

    // Alliances chips
    if (c.alliances && c.alliances.length) {
      body.appendChild(el("div", { class: "section" },
        el("h3", {}, "Alliances & blocs"),
        el("div", { class: "chips" },
          ...c.alliances.map((a) => el("span", { class: "chip" }, a)),
        ),
      ));
    }

    // Demographics
    body.appendChild(demographicsSection(c));

    // Democracy
    if (c.democracy) {
      body.appendChild(democracySection(c.democracy));
    }

    // Issue salience
    if (c.issue_salience && c.issue_salience.length) {
      body.appendChild(issueSection(c.issue_salience));
    }

    // What's pushing voters — economic, trust, identity, info, crisis
    const influences = c._influences || {};
    if (influences && Object.keys(influences).some((k) => influences[k] && (Array.isArray(influences[k]) ? influences[k].length : Object.keys(influences[k]).length))) {
      body.appendChild(influencesSection(influences));
    }

    // Political spectrum — parties placed on a left/right axis
    if (pol.parties && pol.parties.length) {
      body.appendChild(spectrumSection(pol.parties));
    }

    // Regional breakdown — what each province / state wants
    if (pol.regions && pol.regions.length) {
      body.appendChild(regionsSection(pol.regions));
    }

    // Elections
    if (c.elections && c.elections.length) {
      body.appendChild(electionsSection(c.elections));
    }

    // News headlines (lazy-loaded)
    const newsSec = el("div", { class: "section", id: "news-section" },
      el("h3", {}, "Latest news"),
      el("div", { class: "news-loading" }, "Loading headlines…"),
    );
    body.appendChild(newsSec);
    loadNews(c.iso, newsSec);

    // Polling time-series (lazy-loaded)
    if (c._polling_summary && c._polling_summary.has_polls) {
      const pollSec = el("div", { class: "section", id: "polling-section" },
        el("h3", {}, c._polling_summary.election_label
          ? `Polling — ${c._polling_summary.election_label}`
          : "Polling"),
        el("div", { class: "polling-chart-loading" }, "Loading polls…"),
      );
      body.appendChild(pollSec);
      // Fire-and-forget; renders into pollSec when ready.
      loadPolling(c.iso, pollSec);
    }

    // Prediction markets
    if (c._markets) {
      body.appendChild(marketsSection(c._markets));
    }

    // Impact chains (lazy-loaded)
    const chainsSec = el("div", { class: "section", id: "chains-section" },
      el("div", { class: "chains-head" },
        el("h3", {}, "Impact chains"),
        el("button", {
          class: "chain-new-btn",
          onclick: () => openChainComposer(c.iso, null, null),
        }, "+ New chain"),
      ),
      el("div", { class: "chains-loading" }, "Loading chains…"),
    );
    body.appendChild(chainsSec);
    loadChains(c.iso, chainsSec);

    // Cross-dashboard links
    body.appendChild(crossSection(c));

    // Comments + reactions
    const comments = el("div", { class: "comments", id: "comments-root" },
      el("h3", {}, "Discussion"),
      el("div", { class: "reactions-row", id: "reactions-row" }),
      composerBlock(c.iso, null),
      el("div", { class: "thread", id: "thread-root" },
        el("div", { class: "empty" }, "Loading comments…"),
      ),
    );
    body.appendChild(comments);

    renderReactionsRow(c.iso, []);
  }

  function demographicsSection(c) {
    const cells = [];
    if (c.population_m != null) cells.push(kvCell("Population", `${c.population_m}M`));
    if (c.median_age != null) cells.push(kvCell("Median age", c.median_age));
    if (c.urban_pct != null) cells.push(kvCell("Urban", `${c.urban_pct}%`));
    if (c.gdp_per_capita_usd != null) cells.push(kvCell("GDP / capita", "$" + Math.round(c.gdp_per_capita_usd).toLocaleString()));
    if (c.inflation_cpi_pct != null) cells.push(kvCell("Inflation", `${c.inflation_cpi_pct}%`));
    if (c.unemployment_pct != null) cells.push(kvCell("Unemployment", `${c.unemployment_pct}%`));
    if (!cells.length) return el("div");
    return el("div", { class: "section" },
      el("h3", {}, "Who they are"),
      el("div", { class: "kv-grid" }, ...cells),
    );
  }

  function kvCell(label, value, trend) {
    return el("div", { class: "kv" },
      el("div", { class: "kv-label" }, label),
      el("div", { class: "kv-value" }, value),
      trend ? el("div", { class: "kv-trend" }, trend) : null,
    );
  }

  function democracySection(d) {
    const edi = d.vdem_edi;
    const ediPct = edi != null ? Math.round(edi * 100) : null;
    return el("div", { class: "section" },
      el("h3", {}, "Democratic state"),
      el("div", { class: "kv-grid" },
        edi != null
          ? el("div", { class: "kv" },
              el("div", { class: "kv-label" }, "V-Dem EDI"),
              el("div", { class: "gauge" },
                el("div", { class: "gauge-bar" },
                  el("div", { class: "gauge-fill", style: `width: ${ediPct}%;` })),
                el("div", { class: "gauge-num" }, edi.toFixed(2)),
              ),
              d.trend_10y ? el("div", { class: "kv-trend" }, `10y: ${d.trend_10y}`) : null,
            )
          : null,
        d.freedom_house_score != null ? kvCell("Freedom House", `${d.freedom_house_score}/100`) : null,
        d.press_freedom_rank != null ? kvCell("Press freedom rank", `#${d.press_freedom_rank}`) : null,
      ),
    );
  }

  function issueSection(issues) {
    const max = Math.max(...issues.map((i) => i.pct || 0)) || 100;
    return el("div", { class: "section" },
      el("h3", {}, "What they want — top voter concerns"),
      ...issues.map((i) => el("div", { class: "issue-row" },
        el("div", {},
          el("div", { class: "issue-name" }, i.issue),
          el("div", { class: "issue-bar-wrap" },
            el("div", { class: "issue-bar", style: `width: ${(100 * (i.pct || 0)) / max}%;` })),
          i.source ? el("div", { class: "issue-source" }, i.source) : null,
        ),
        el("div", { class: "issue-pct" }, `${i.pct ?? "?"}%`),
      )),
    );
  }

  function electionsSection(elections) {
    return el("div", { class: "section" },
      el("h3", {}, "Election calendar"),
      ...elections.map((e) => el("div", { class: "election-row" },
        el("div", { class: "election-date" }, `${e.date || "TBD"} · ${fmtCountdown(e.date)}`),
        el("div", { class: "election-type" }, e.type || ""),
        e.stakes ? el("div", { class: "election-stakes" }, e.stakes) : null,
      )),
    );
  }

  // ── What's pushing voters (5-dimension influence panel) ─────────

  function influencesSection(inf) {
    const blocks = [];
    if (inf.economic_pressure && Object.keys(inf.economic_pressure).length) {
      blocks.push(economicPressureBlock(inf.economic_pressure));
    }
    if (inf.trust && Object.keys(inf.trust).length) {
      blocks.push(trustBlock(inf.trust));
    }
    if (inf.identity && Object.keys(inf.identity).length) {
      blocks.push(identityBlock(inf.identity));
    }
    if (inf.information && Object.keys(inf.information).length) {
      blocks.push(informationBlock(inf.information));
    }
    if (inf.crisis_memory && inf.crisis_memory.length) {
      blocks.push(crisisBlock(inf.crisis_memory));
    }
    if (inf.security && Object.keys(inf.security).length) {
      blocks.push(securityBlock(inf.security));
    }
    if (inf.demographic_shifts && Object.keys(inf.demographic_shifts).length) {
      blocks.push(demoShiftBlock(inf.demographic_shifts));
    }
    if (!blocks.length) return el("div");
    return el("div", { class: "section influences-section" },
      el("h3", {}, "What's pushing voters"),
      el("div", { class: "influences-help" },
        "The forces political-science research links to vote choice — economic, institutional, identity, information, recent crises."),
      el("div", { class: "influence-grid" }, ...blocks),
    );
  }

  // Tone-of-number helpers — colour gauges by direction.
  // For things where higher is better (real wage growth, trust): green high.
  // For things where higher is worse (unemployment, inflation): red high.
  function tone(value, kind) {
    if (value == null || !Number.isFinite(value)) return "var(--text-faint)";
    if (kind === "good_high") {
      if (value >= 50) return "var(--ok)";
      if (value >= 25) return "var(--warn)";
      return "var(--bad)";
    }
    if (kind === "bad_high") {
      if (value <= 4) return "var(--ok)";
      if (value <= 8) return "var(--warn)";
      return "var(--bad)";
    }
    if (kind === "wage") {
      if (value >= 1.5) return "var(--ok)";
      if (value >= 0) return "var(--warn)";
      return "var(--bad)";
    }
    if (kind === "cpi") {
      if (value >= 60) return "var(--ok)";
      if (value >= 40) return "var(--warn)";
      return "var(--bad)";
    }
    return "var(--text)";
  }

  function trendArrow(s) {
    if (!s) return "";
    const t = String(s).toLowerCase();
    if (t.includes("rising")) return "↑";
    if (t.includes("falling") || t.includes("easing")) return "↓";
    if (t.includes("stable")) return "→";
    return "";
  }

  function economicPressureBlock(d) {
    const fmt = (v, suf) => v == null ? "—" : `${v}${suf || ""}`;
    return el("div", { class: "influence-card" },
      el("div", { class: "influence-head" }, "💰 Economic pressure"),
      el("div", { class: "stat-grid" },
        statTile("Real wage YoY", fmt(d.real_wage_growth_yoy_pct, "%"), tone(d.real_wage_growth_yoy_pct, "wage")),
        statTile("Unemployment " + trendArrow(d.unemployment_trend), fmt(d.unemployment_pct, "%"), tone(d.unemployment_pct, "bad_high")),
        statTile("Inflation " + trendArrow(d.inflation_trend), fmt(d.inflation_pct, "%"), tone(d.inflation_pct, "bad_high")),
        statTile("Housing", labelize(d.housing_unaffordability), pressureColor(d.housing_unaffordability)),
        statTile("Consumer mood", labelize(d.consumer_confidence), pressureColor(d.consumer_confidence)),
      ),
      d.key_pressure ? el("div", { class: "influence-key" }, d.key_pressure) : null,
      d.source ? el("div", { class: "influence-source" }, d.source) : null,
    );
  }

  function pressureColor(s) {
    if (!s) return "var(--text-faint)";
    const t = String(s).toLowerCase();
    if (t.includes("severe") || t.includes("well_below")) return "var(--bad)";
    if (t.includes("strained") || t.includes("below_avg")) return "var(--warn)";
    if (t.includes("manageable") || t.includes("above_avg") || t.includes("strong")) return "var(--ok)";
    return "var(--text)";
  }

  function labelize(s) {
    if (!s) return "—";
    return String(s).replace(/_/g, " ");
  }

  function trustBlock(d) {
    const fmt = (v) => v == null ? "—" : `${v}%`;
    // Estimated-value markers: append a small ◇ to the label when the value
    // is `_estimated: true` so the user sees what was modelled vs surveyed.
    const est = (label, key) => d[key + "_estimated"] ? label + " ◇" : label;
    return el("div", { class: "influence-card" },
      el("div", { class: "influence-head" }, "🏛️ Trust & legitimacy"),
      el("div", { class: "stat-grid" },
        statTile(est("Trust in govt", "trust_in_govt_pct"), fmt(d.trust_in_govt_pct), tone(d.trust_in_govt_pct, "good_high")),
        statTile(est("Right track", "right_track_pct"), fmt(d.right_track_pct), tone(d.right_track_pct, "good_high")),
        statTile(est("Trust in media", "trust_in_media_pct"), fmt(d.trust_in_media_pct), tone(d.trust_in_media_pct, "good_high")),
        statTile("Democracy satisfied", fmt(d.satisfaction_with_democracy_pct), tone(d.satisfaction_with_democracy_pct, "good_high")),
        statTile("Corruption (CPI)",
          d.corruption_perception_score != null
            ? `${d.corruption_perception_score}` + (d.corruption_perception_rank ? ` · #${d.corruption_perception_rank}` : "")
            : "—",
          tone(d.corruption_perception_score, "cpi")),
        statTile(est("Trust military", "trust_in_military_pct"), fmt(d.trust_in_military_pct), tone(d.trust_in_military_pct, "good_high")),
        statTile(est("Trust courts", "trust_in_courts_pct"), fmt(d.trust_in_courts_pct), tone(d.trust_in_courts_pct, "good_high")),
        statTile(est("Trust police", "trust_in_police_pct"), fmt(d.trust_in_police_pct), tone(d.trust_in_police_pct, "good_high")),
      ),
      hasEstimatedFields(d) ? el("div", { class: "influence-detail estimated-note" },
        "◇ values estimated (independent polling unavailable) — source string lists the modeller.") : null,
      d.source ? el("div", { class: "influence-source" }, d.source) : null,
    );
  }

  function hasEstimatedFields(d) {
    return Object.keys(d || {}).some((k) => k.endsWith("_estimated") && d[k] === true);
  }

  function identityBlock(d) {
    const fmt = (v, suf) => v == null ? "—" : `${v}${suf || ""}`;
    const fmtSigned = (v, suf) => v == null ? "—" : `${v >= 0 ? "+" : ""}${v}${suf || ""}`;
    const est = (label, key) => d[key + "_estimated"] ? label + " ◇" : label;

    // Gini: lower = more equal. Tone reverses: <0.30 ok, 0.30-0.40 warn, >0.40 bad.
    const giniTone = (g) => {
      if (g == null) return "var(--text-faint)";
      if (g < 0.30) return "var(--ok)";
      if (g < 0.40) return "var(--warn)";
      return "var(--bad)";
    };
    return el("div", { class: "influence-card identity-card" },
      el("div", { class: "influence-head" }, "🧬 Identity & cleavages"),
      el("div", { class: "stat-grid" },
        statTile("University-educated", fmt(d.university_educated_pct, "%"), "var(--text)"),
        statTile("Religious attendance", fmt(d.religious_attendance_weekly_pct, "%"), "var(--text)"),
        statTile("Urban", fmt(d.urban_pct, "%"), "var(--text)"),
        statTile("Median age", fmt(d.median_age), "var(--text)"),
        statTile("Gini (inequality)", fmt(d.gini_coefficient), giniTone(d.gini_coefficient)),
        statTile("Last-election turnout", fmt(d.voter_turnout_last_pct, "%"), tone(d.voter_turnout_last_pct, "good_high")),
        statTile(est("Gender gap (left lead, women)", "gender_gap_pp"), fmtSigned(d.gender_gap_pp, "pp"), tone(Math.abs(d.gender_gap_pp || 0), "good_high")),
        statTile(est("Diploma divide (left lead, college)", "diploma_divide_pp"), fmtSigned(d.diploma_divide_pp, "pp"), tone(Math.abs(d.diploma_divide_pp || 0), "good_high")),
      ),
      // Generational mix bar
      genMixBar(d),
      d.dominant_religion ? el("div", { class: "influence-detail" }, "Religion: " + d.dominant_religion) : null,
      d.key_cleavage ? el("div", { class: "influence-key" }, d.key_cleavage) : null,
      d.identity_source ? el("div", { class: "influence-source" }, d.identity_source) : null,
      d.voter_turnout_source ? el("div", { class: "influence-source" }, "Turnout: " + d.voter_turnout_source) : null,
    );
  }

  function genMixBar(d) {
    if (d.gen_z_share_pct == null && d.millennial_share_pct == null) return null;
    const segments = [
      { label: "Gen Z (≤27)",       pct: d.gen_z_share_pct,        color: "#f59e0b" },
      { label: "Millennial (28-43)", pct: d.millennial_share_pct,   color: "#22c55e" },
      { label: "Gen X (44-59)",      pct: d.gen_x_share_pct,        color: "#00b4d8" },
      { label: "Boomer+ (60+)",      pct: d.boomer_plus_share_pct,  color: "#7c5cff" },
    ];
    const total = segments.reduce((s, x) => s + (x.pct || 0), 0) || 1;
    return el("div", { class: "gen-mix" },
      el("div", { class: "gen-mix-label" }, "Generational mix"),
      el("div", { class: "gen-mix-bar" },
        ...segments.map((s) => s.pct ? el("div", {
          class: "gen-mix-seg",
          style: `width: ${(s.pct / total) * 100}%; background: ${s.color};`,
          title: `${s.label}: ${s.pct}%`,
        }, s.pct >= 12 ? `${s.pct}%` : "") : null),
      ),
      el("div", { class: "gen-mix-legend" },
        ...segments.map((s) => s.pct ? el("span", {},
          el("span", { class: "gen-mix-swatch", style: `background: ${s.color};` }),
          s.label,
        ) : null),
      ),
    );
  }

  function informationBlock(d) {
    const fmt = (v, suf) => v == null ? "—" : `${v}${suf || ""}`;
    const misColor = (s) => {
      const t = (s || "").toLowerCase();
      if (t.includes("very high")) return "var(--bad)";
      if (t.includes("high")) return "var(--bad)";
      if (t.includes("moderate")) return "var(--warn)";
      return "var(--ok)";
    };
    return el("div", { class: "influence-card" },
      el("div", { class: "influence-head" }, "📡 Information environment"),
      el("div", { class: "stat-grid" },
        statTile("Internet penetration", fmt(d.internet_pct, "%"), tone(d.internet_pct, "good_high")),
        statTile("Social-media users", fmt(d.social_media_users_pct, "%"), "var(--text)"),
        statTile("Press freedom rank",
          d.press_freedom_rank != null ? `#${d.press_freedom_rank}` : "—",
          tone(180 - (d.press_freedom_rank || 0), "good_high")),
        statTile("Misinformation pressure", labelize(d.misinformation_exposure), misColor(d.misinformation_exposure)),
      ),
      d.top_platforms && d.top_platforms.length ? el("div", { class: "platform-chips" },
        ...d.top_platforms.map((p) => el("span", { class: "platform-chip" }, p)),
      ) : null,
      d.news_consumption ? el("div", { class: "influence-detail" }, d.news_consumption) : null,
    );
  }

  function crisisBlock(items) {
    return el("div", { class: "influence-card crisis-card" },
      el("div", { class: "influence-head" }, "⚡ Crisis memory"),
      el("ul", { class: "crisis-list" },
        ...items.map((s) => el("li", {}, s)),
      ),
    );
  }

  function securityBlock(d) {
    const fmt = (v, suf) => v == null ? "—" : `${v}${suf || ""}`;
    // Homicide tone: world median ~5/100k. Below 2 ok, 2-10 warn, >10 bad.
    const homTone = (v) => {
      if (v == null) return "var(--text-faint)";
      if (v < 2) return "var(--ok)";
      if (v < 10) return "var(--warn)";
      return "var(--bad)";
    };
    const sevTone = (s) => {
      const t = (s || "").toLowerCase();
      if (t.includes("very high") || t.includes("decisive")) return "var(--bad)";
      if (t.includes("high") || t.includes("elevated") || t.includes("rising")) return "var(--warn)";
      if (t.includes("low") || t.includes("quiet")) return "var(--ok)";
      return "var(--text)";
    };
    // Internet freedom: 0-100 (Freedom House), higher = freer
    const ifTone = (v) => {
      if (v == null) return "var(--text-faint)";
      if (v >= 70) return "var(--ok)";
      if (v >= 40) return "var(--warn)";
      return "var(--bad)";
    };
    // Energy/food import dependence: + = importer (vulnerable), - = exporter
    const fmtSigned = (v, suf) => {
      if (v == null) return "—";
      if (v >= 0) return `+${v}${suf || ""}`;
      return `${v}${suf || ""}`;
    };
    const importTone = (v) => {
      if (v == null) return "var(--text-faint)";
      if (v < 0) return "var(--ok)";       // exporter
      if (v < 30) return "var(--warn)";
      return "var(--bad)";                  // heavy import dependence
    };

    return el("div", { class: "influence-card security-card" },
      el("div", { class: "influence-head" }, "🛡️ Security & disorder"),
      el("div", { class: "stat-grid" },
        statTile("Homicide / 100k", fmt(d.homicide_per_100k), homTone(d.homicide_per_100k)),
        statTile("Crime perception", labelize(d.violent_crime_perception), sevTone(d.violent_crime_perception)),
        statTile("Terror salience", labelize(d.terror_salience), sevTone(d.terror_salience)),
        statTile("Military's role", labelize(d.military_role), sevTone(d.military_role)),
        statTile("Internet freedom", d.internet_freedom_score == null ? "—" : `${d.internet_freedom_score}/100`, ifTone(d.internet_freedom_score)),
        statTile("Energy import-dep", fmtSigned(d.energy_import_dependence_pct, "%"), importTone(d.energy_import_dependence_pct)),
        statTile("Food import-dep", fmtSigned(d.food_import_dependence_pct, "%"), importTone(d.food_import_dependence_pct)),
      ),
      d.climate_disasters_recent && d.climate_disasters_recent.length ? el("div", { class: "disaster-list" },
        el("div", { class: "disaster-head" }, "Recent climate disasters:"),
        el("ul", {},
          ...d.climate_disasters_recent.map((s) => el("li", {}, s)),
        ),
      ) : null,
      d.source ? el("div", { class: "influence-source" }, d.source) : null,
    );
  }

  function demoShiftBlock(d) {
    const fmt = (v, suf) => v == null ? "—" : `${v >= 0 ? "+" : ""}${v}${suf || ""}`;
    return el("div", { class: "influence-card" },
      el("div", { class: "influence-head" }, "📊 Demographic shifts"),
      el("div", { class: "stat-grid" },
        statTile("Net migration / 1k", fmt(d.net_migration_per_1000), "var(--text)"),
        statTile("Population 5y", fmt(d.population_growth_5y_pct, "%"), "var(--text)"),
      ),
      d.key_shift ? el("div", { class: "influence-key" }, d.key_shift) : null,
    );
  }

  function statTile(label, value, color) {
    return el("div", { class: "stat-tile" },
      el("div", { class: "stat-label" }, label),
      el("div", { class: "stat-value", style: `color: ${color || "var(--text)"};` }, value),
    );
  }

  // ── Political context (leader, spectrum, regions) ───────────────

  function leaderSection(leader, parties) {
    const partyMatch = parties.find((p) => p.abbr === leader.party_abbr);
    const partyColor = partyMatch ? partyMatch.color : "#7c5cff";
    const partyName = partyMatch ? partyMatch.name : leader.party_abbr;
    const initials = (leader.name || "")
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((s) => s[0])
      .join("")
      .toUpperCase();
    const ap = leader.approval_pct;
    const apColor = ap == null ? "var(--text-faint)"
      : ap >= 50 ? "var(--ok)"
      : ap >= 35 ? "var(--warn)"
      : "var(--bad)";
    return el("div", { class: "section leader-section" },
      el("h3", {}, "Currently in power"),
      el("div", { class: "leader-card" },
        el("div", { class: "leader-avatar", style: `background: ${partyColor};` }, initials || "—"),
        el("div", { class: "leader-info" },
          el("div", { class: "leader-name" }, leader.name),
          el("div", { class: "leader-role" },
            leader.role,
            " · ",
            el("span", { class: "leader-party", style: `color: ${partyColor};` }, partyName),
          ),
          el("div", { class: "leader-meta" },
            leader.since ? `Since ${leader.since}` : null,
            leader.bio ? " · " + leader.bio : null,
          ),
        ),
        ap != null ? el("div", { class: "leader-approval" },
          el("div", { class: "approval-num", style: `color: ${apColor};` }, `${ap}%`),
          el("div", { class: "approval-label" }, "approval"),
          leader.approval_source ? el("div", { class: "approval-source" }, leader.approval_source) : null,
        ) : null,
      ),
    );
  }

  function spectrumSection(parties) {
    // 2D political compass. Economic axis (left/right) on X, social axis
    // (libertarian/authoritarian) on Y. Each party is a circle sized by
    // vote_share_pct and coloured by brand.
    //
    // Convention:
    //   position (econ): -10 = far left, +10 = far right
    //   social:           -10 = libertarian, +10 = authoritarian
    // SVG y-axis is flipped so libertarian is at the bottom.
    const W = 560, H = 360;
    const padL = 30, padR = 12, padT = 14, padB = 30;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;
    const cxAxis = padL + innerW / 2;
    const cyAxis = padT + innerH / 2;
    const xOf = (econ) => padL + ((econ + 10) / 20) * innerW;
    const yOf = (social) => padT + ((10 - social) / 20) * innerH;
    // 1D fallback (only used for the legend label position-class)
    const yMid = cyAxis;

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "spectrum-svg compass-svg");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Political compass — economic axis × social axis");

    // Quadrant tints. Top = authoritarian, bottom = libertarian.
    // Order: TL (auth-left), TR (auth-right), BL (lib-left), BR (lib-right).
    const quads = [
      { x: padL,           y: padT,            w: innerW / 2, h: innerH / 2, fill: "#dd0000", opacity: 0.10 },
      { x: padL + innerW/2, y: padT,            w: innerW / 2, h: innerH / 2, fill: "#0046ad", opacity: 0.10 },
      { x: padL,           y: padT + innerH/2, w: innerW / 2, h: innerH / 2, fill: "#dd0000", opacity: 0.05 },
      { x: padL + innerW/2, y: padT + innerH/2, w: innerW / 2, h: innerH / 2, fill: "#0046ad", opacity: 0.05 },
    ];
    for (const q of quads) {
      const r = document.createElementNS(svgNS, "rect");
      r.setAttribute("x", q.x); r.setAttribute("y", q.y);
      r.setAttribute("width", q.w); r.setAttribute("height", q.h);
      r.setAttribute("fill", q.fill); r.setAttribute("opacity", q.opacity);
      svg.appendChild(r);
    }

    // Outer frame
    const frame = document.createElementNS(svgNS, "rect");
    frame.setAttribute("x", padL); frame.setAttribute("y", padT);
    frame.setAttribute("width", innerW); frame.setAttribute("height", innerH);
    frame.setAttribute("fill", "none");
    frame.setAttribute("class", "compass-frame");
    svg.appendChild(frame);

    // Cross-hair axes
    const xAxis = document.createElementNS(svgNS, "line");
    xAxis.setAttribute("x1", padL); xAxis.setAttribute("x2", padL + innerW);
    xAxis.setAttribute("y1", cyAxis); xAxis.setAttribute("y2", cyAxis);
    xAxis.setAttribute("class", "compass-axis");
    svg.appendChild(xAxis);
    const yAxis = document.createElementNS(svgNS, "line");
    yAxis.setAttribute("x1", cxAxis); yAxis.setAttribute("x2", cxAxis);
    yAxis.setAttribute("y1", padT); yAxis.setAttribute("y2", padT + innerH);
    yAxis.setAttribute("class", "compass-axis");
    svg.appendChild(yAxis);

    // Quadrant labels
    const quadLabels = [
      { x: padL + 6,           y: padT + 14,            anchor: "start", text: "Authoritarian-left" },
      { x: padL + innerW - 6,  y: padT + 14,            anchor: "end",   text: "Authoritarian-right" },
      { x: padL + 6,           y: padT + innerH - 6,    anchor: "start", text: "Libertarian-left" },
      { x: padL + innerW - 6,  y: padT + innerH - 6,    anchor: "end",   text: "Libertarian-right" },
    ];
    for (const q of quadLabels) {
      const t = document.createElementNS(svgNS, "text");
      t.setAttribute("x", q.x); t.setAttribute("y", q.y);
      t.setAttribute("class", "compass-quad-lbl");
      t.setAttribute("text-anchor", q.anchor);
      t.textContent = q.text;
      svg.appendChild(t);
    }

    // Axis end-labels
    function axisLbl(x, y, anchor, text) {
      const t = document.createElementNS(svgNS, "text");
      t.setAttribute("x", x); t.setAttribute("y", y);
      t.setAttribute("class", "compass-axis-lbl");
      t.setAttribute("text-anchor", anchor);
      t.textContent = text;
      svg.appendChild(t);
    }
    axisLbl(padL - 4, cyAxis + 4, "end", "left ←");
    axisLbl(padL + innerW + 4, cyAxis + 4, "start", "→ right");
    axisLbl(cxAxis, padT - 4, "middle", "↑ authoritarian");
    axisLbl(cxAxis, padT + innerH + 16, "middle", "↓ libertarian");

    // Place parties. De-overlap by 2D collision: if two parties land too
    // close, nudge the second along the gradient direction.
    const placed = [];
    const sorted = parties.slice().sort((a, b) => (b.vote_share_pct || 0) - (a.vote_share_pct || 0));
    for (const p of sorted) {
      const econ = (p.position == null) ? 0 : p.position;
      const soc = (p.social == null) ? 0 : p.social;
      let cx = xOf(econ), cy = yOf(soc);
      const r = Math.max(6, Math.min(22, Math.sqrt(Math.max(p.vote_share_pct || 1, 1)) * 2.4));
      // Push apart from any already-placed dot
      for (let iter = 0; iter < 5; iter++) {
        let collided = false;
        for (const pp of placed) {
          const dx = cx - pp.cx, dy = cy - pp.cy;
          const dist = Math.hypot(dx, dy);
          const min = pp.r + r + 2;
          if (dist < min && dist > 0.1) {
            const push = (min - dist) / 2;
            cx += (dx / dist) * push;
            cy += (dy / dist) * push;
            collided = true;
          } else if (dist <= 0.1) {
            cx += 8; cy += 8; collided = true;
          }
        }
        if (!collided) break;
      }
      // Clamp inside the frame
      cx = Math.max(padL + r, Math.min(padL + innerW - r, cx));
      cy = Math.max(padT + r, Math.min(padT + innerH - r, cy));
      placed.push({ ...p, cx, cy, r });

      const c = document.createElementNS(svgNS, "circle");
      c.setAttribute("cx", cx); c.setAttribute("cy", cy); c.setAttribute("r", r);
      c.setAttribute("fill", p.color || "#7c5cff");
      c.setAttribute("opacity", p.in_government ? "1" : "0.75");
      c.setAttribute("stroke", p.in_government ? "#ffffff" : "transparent");
      c.setAttribute("stroke-width", p.in_government ? "2" : "0");
      const title = document.createElementNS(svgNS, "title");
      title.textContent = `${p.name} (${p.abbr}) · econ ${p.position} / social ${p.social} · ${p.vote_share_pct}% · ${p.seats || 0}/${p.total_seats || "?"} seats${p.in_government ? " · in govt" : ""}`;
      c.appendChild(title);
      svg.appendChild(c);
      // Abbr label inside or right of dot depending on space
      const lbl = document.createElementNS(svgNS, "text");
      lbl.setAttribute("x", cx);
      lbl.setAttribute("y", cy + r + 10);
      lbl.setAttribute("class", "spectrum-party-lbl");
      lbl.setAttribute("text-anchor", "middle");
      lbl.textContent = p.abbr;
      svg.appendChild(lbl);
    }

    // Legend grid below
    const legend = el("div", { class: "spectrum-legend" },
      ...parties.map((p) => el("div", { class: "spectrum-legend-row" },
        el("span", { class: "spectrum-swatch", style: `background: ${p.color || "#7c5cff"};` }),
        el("span", { class: "spectrum-name" }, p.name),
        p.in_government ? el("span", { class: "spectrum-tag" }, "in govt") : null,
        el("span", { class: "spectrum-share" }, `${p.vote_share_pct}%`),
        p.seats != null && p.total_seats != null
          ? el("span", { class: "spectrum-seats" }, `${p.seats}/${p.total_seats} seats`)
          : null,
        p.leader ? el("span", { class: "spectrum-leader" }, p.leader) : null,
      )),
    );

    const wrap = el("div", { class: "section spectrum-section" },
      el("h3", {}, "Political compass — economic × social"),
      el("div", { class: "compass-chart" }),
      el("div", { class: "compass-help" },
        "Horizontal: economic policy · Vertical: social policy · Dot size = vote share · ",
        el("span", { class: "compass-govt-key" }, "white outline"),
        " = currently in government",
      ),
      legend,
    );
    wrap.querySelector(".compass-chart").appendChild(svg);
    return wrap;
  }

  function regionsSection(regions) {
    return el("div", { class: "section regions-section" },
      el("h3", {}, "By region — what each part wants"),
      el("div", { class: "regions-grid" },
        ...regions.map((r) => el("div", { class: "region-card" },
          el("div", { class: "region-head" },
            el("span", { class: "region-name" }, r.name),
            r.lean ? el("span", { class: "region-lean" }, r.lean) : null,
          ),
          r.top_issue ? el("div", { class: "region-issue" }, r.top_issue) : null,
          r.note ? el("div", { class: "region-note" }, r.note) : null,
        )),
      ),
    );
  }

  // ── News feed ───────────────────────────────────────────────────

  async function loadNews(iso, sectionEl) {
    let data;
    try {
      data = await api(`/api/country/${iso}/news`);
    } catch (e) {
      const loading = sectionEl.querySelector(".news-loading");
      if (loading) loading.textContent = "News unavailable: " + e.message;
      return;
    }
    if (state.activeIso !== iso) return;
    const loading = sectionEl.querySelector(".news-loading");
    if (loading) loading.remove();
    const items = (data.items || []);
    if (!items.length) {
      sectionEl.appendChild(el("div", { class: "hint" }, "No headlines available."));
      return;
    }
    const list = el("div", { class: "news-list" });
    for (const it of items.slice(0, 8)) {
      list.appendChild(newsItem(it));
    }
    sectionEl.appendChild(list);

    // Status footer + refresh
    const status = data._status || {};
    const ageS = status.fetched_at ? Math.max(0, Math.floor(Date.now() / 1000) - status.fetched_at) : null;
    sectionEl.appendChild(el("div", { class: "news-meta" },
      `${items.length} headlines${ageS != null ? " · cached " + relTime(status.fetched_at) : ""} · `,
      el("a", { href: "#", onclick: (e) => { e.preventDefault(); refreshNews(iso); } }, "refresh"),
    ));
  }

  function newsItem(it) {
    const time = parseRssDate(it.published);
    const timeLabel = time ? relTime(Math.floor(time / 1000)) : (it.published || "");
    return el("a", {
      class: "news-item",
      href: it.link,
      target: "_blank",
      rel: "noopener noreferrer",
    },
      el("div", { class: "news-title" }, it.title.replace(/\s+-\s+[^-]+$/, "")),
      el("div", { class: "news-meta-row" },
        it.source ? el("span", { class: "news-source" }, it.source) : null,
        timeLabel ? el("span", { class: "news-time" }, timeLabel) : null,
      ),
      it.summary ? el("div", { class: "news-summary" }, it.summary) : null,
    );
  }

  function parseRssDate(s) {
    if (!s) return null;
    const t = Date.parse(s);
    return Number.isFinite(t) ? t : null;
  }

  async function refreshNews(iso) {
    const sec = $("#news-section");
    if (!sec || state.activeIso !== iso) return;
    sec.innerHTML = "";
    sec.appendChild(el("h3", {}, "Latest news"));
    sec.appendChild(el("div", { class: "news-loading" }, "Refreshing…"));
    // Bypass any potential client-side cache by appending a cachebuster
    try {
      const data = await api(`/api/country/${iso}/news?_=${Date.now()}`);
      if (state.activeIso !== iso) return;
      sec.querySelector(".news-loading")?.remove();
      const items = data.items || [];
      const list = el("div", { class: "news-list" });
      items.slice(0, 8).forEach((it) => list.appendChild(newsItem(it)));
      sec.appendChild(list);
    } catch (e) {
      showToast("News refresh failed: " + e.message, true);
    }
  }

  // ── Polling chart ───────────────────────────────────────────────

  // Stable palette for series labels. Cycles if we run out.
  const SERIES_COLORS = [
    "#7c5cff", "#00b4d8", "#22c55e", "#f59e0b", "#ef4444",
    "#ec4899", "#10b981", "#8b5cf6", "#f97316", "#06b6d4",
  ];

  async function loadPolling(iso, sectionEl) {
    let data;
    try {
      data = await api(`/api/country/${iso}/polling`);
    } catch (e) {
      const loading = sectionEl.querySelector(".polling-chart-loading");
      if (loading) loading.textContent = "Polls unavailable: " + e.message;
      return;
    }
    // If the drawer was closed or switched countries, bail.
    if (state.activeIso !== iso) return;
    const loading = sectionEl.querySelector(".polling-chart-loading");
    if (loading) loading.remove();
    sectionEl.appendChild(pollingChart(data));
  }

  function pollingChart(data) {
    const series = data.series || [];
    if (!series.length) {
      return el("div", { class: "hint" }, "No polling data.");
    }

    // Collect all dates across series, then build x-domain.
    const allPoints = series.flatMap((s) => s.points);
    const allDates = [...new Set(allPoints.map((p) => p.date))].sort();
    if (allDates.length < 2) {
      return el("div", { class: "hint" }, "Need at least 2 polls to chart.");
    }

    const t0 = Date.parse(allDates[0]);
    const t1 = Date.parse(allDates[allDates.length - 1]);
    const tSpan = Math.max(1, t1 - t0);

    // y-domain: snap to nearest 5 around the data, with at least 0..50.
    let yMin = Math.min(...allPoints.map((p) => p.pct));
    let yMax = Math.max(...allPoints.map((p) => p.pct));
    yMin = Math.max(0, Math.floor((yMin - 3) / 5) * 5);
    yMax = Math.min(100, Math.ceil((yMax + 3) / 5) * 5);
    if (yMax - yMin < 15) yMax = yMin + 15;

    // SVG layout
    const W = 580, H = 240;
    const padL = 32, padR = 12, padT = 8, padB = 28;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;

    const xOf = (dateStr) => padL + ((Date.parse(dateStr) - t0) / tSpan) * innerW;
    const yOf = (pct) => padT + (1 - (pct - yMin) / (yMax - yMin)) * innerH;

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "polling-chart-svg");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Polling time-series");

    // Y-axis gridlines + labels
    const ticks = 5;
    for (let i = 0; i <= ticks; i++) {
      const v = yMin + ((yMax - yMin) * i) / ticks;
      const y = yOf(v);
      const line = document.createElementNS(svgNS, "line");
      line.setAttribute("x1", padL);
      line.setAttribute("x2", W - padR);
      line.setAttribute("y1", y);
      line.setAttribute("y2", y);
      line.setAttribute("class", "chart-grid");
      svg.appendChild(line);
      const lbl = document.createElementNS(svgNS, "text");
      lbl.setAttribute("x", padL - 6);
      lbl.setAttribute("y", y + 3);
      lbl.setAttribute("text-anchor", "end");
      lbl.setAttribute("class", "chart-axis-label");
      lbl.textContent = `${Math.round(v)}%`;
      svg.appendChild(lbl);
    }

    // X-axis date labels (first, middle, last)
    const xTicks = [allDates[0], allDates[Math.floor(allDates.length / 2)], allDates[allDates.length - 1]];
    for (const d of xTicks) {
      const lbl = document.createElementNS(svgNS, "text");
      lbl.setAttribute("x", xOf(d));
      lbl.setAttribute("y", H - 8);
      lbl.setAttribute("text-anchor", "middle");
      lbl.setAttribute("class", "chart-axis-label");
      lbl.textContent = d.slice(2); // "26-04-08"
      svg.appendChild(lbl);
    }

    // Series lines + dots
    series.forEach((s, idx) => {
      const color = SERIES_COLORS[idx % SERIES_COLORS.length];
      const path = document.createElementNS(svgNS, "path");
      const d = s.points
        .map((p, i) => `${i === 0 ? "M" : "L"} ${xOf(p.date).toFixed(1)} ${yOf(p.pct).toFixed(1)}`)
        .join(" ");
      path.setAttribute("d", d);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", color);
      path.setAttribute("stroke-width", "2");
      path.setAttribute("stroke-linejoin", "round");
      path.setAttribute("stroke-linecap", "round");
      svg.appendChild(path);

      for (const p of s.points) {
        const c = document.createElementNS(svgNS, "circle");
        c.setAttribute("cx", xOf(p.date));
        c.setAttribute("cy", yOf(p.pct));
        c.setAttribute("r", "2.5");
        c.setAttribute("fill", color);
        const title = document.createElementNS(svgNS, "title");
        title.textContent = `${s.label}: ${p.pct}% (${p.pollster}, ${p.date})`;
        c.appendChild(title);
        svg.appendChild(c);
      }
    });

    // Legend
    const legend = el("div", { class: "polling-legend" },
      ...series.map((s, idx) => {
        const color = SERIES_COLORS[idx % SERIES_COLORS.length];
        const last = s.points[s.points.length - 1];
        return el("span", { class: "legend-item" },
          el("span", { class: "legend-swatch", style: `background: ${color};` }),
          el("span", { class: "legend-label" }, s.label),
          el("span", { class: "legend-last" }, `${last.pct}%`),
        );
      }),
    );

    const meta = el("div", { class: "polling-meta" },
      `${data.polls.length} polls · latest ${data.polls[data.polls.length - 1].date} (${data.polls[data.polls.length - 1].pollster})`,
    );

    const wrap = el("div", { class: "polling-chart" });
    wrap.appendChild(svg);
    wrap.appendChild(legend);
    wrap.appendChild(meta);
    return wrap;
  }

  // ── Prediction markets ──────────────────────────────────────────

  function marketsSection(m) {
    const poly = m.polymarket || [];
    const kal = m.kalshi || [];
    if (!poly.length && !kal.length) {
      const status = m._status || {};
      const polyOk = status.polymarket && status.polymarket.ok;
      const kalOk = status.kalshi && status.kalshi.ok;
      const note = (polyOk || kalOk)
        ? "No matching markets on Polymarket or Kalshi yet."
        : "Markets unavailable (provider down).";
      return el("div", { class: "section" },
        el("h3", {}, "Prediction markets"),
        el("div", { class: "hint" }, note),
      );
    }
    return el("div", { class: "section" },
      el("h3", {}, "Prediction markets"),
      poly.length ? el("div", { class: "market-group" },
        el("div", { class: "market-group-head" }, "Polymarket"),
        ...poly.map((mk) => marketRow(mk)),
      ) : null,
      kal.length ? el("div", { class: "market-group" },
        el("div", { class: "market-group-head" }, "Kalshi"),
        ...kal.map((mk) => marketRow(mk)),
      ) : null,
    );
  }

  function marketRow(mk) {
    const yes = mk.yes_price;
    const yesPct = (yes != null && Number.isFinite(yes)) ? Math.round(yes * 100) : null;
    const vol = mk.volume;
    const volStr = (vol != null && Number.isFinite(vol))
      ? (vol >= 1e6 ? `$${(vol / 1e6).toFixed(1)}M` : vol >= 1e3 ? `$${(vol / 1e3).toFixed(0)}k` : `$${Math.round(vol)}`)
      : null;
    return el("a", {
      class: "market-row",
      href: mk.url, target: "_blank", rel: "noopener noreferrer",
    },
      el("div", { class: "market-question" }, mk.question),
      el("div", { class: "market-stats" },
        yesPct != null ? el("span", { class: "market-yes" }, `${yesPct}% YES`) : null,
        volStr ? el("span", { class: "market-vol" }, `vol ${volStr}`) : null,
        mk.end_date ? el("span", { class: "market-end" }, `ends ${String(mk.end_date).slice(0, 10)}`) : null,
      ),
    );
  }

  function crossSection(c) {
    const cross = c._cross || {};
    const blocks = [];

    if (cross.commodity_links && cross.commodity_links.length) {
      blocks.push(el("div", { class: "chips" },
        ...cross.commodity_links.slice(0, 12).map((cl) =>
          el("span", { class: "chip cmd" }, `${cl.commodity} → ${cl.symbol}`)
        ),
      ));
    }

    if (cross.midterm_races && cross.midterm_races.length) {
      blocks.push(el("div", {},
        el("p", { class: "subtle", style: "margin: 8px 0 4px;" }, "Top US races (from midterm dashboard):"),
        el("div", { class: "chips" },
          ...cross.midterm_races.slice(0, 6).map((r) =>
            el("span", { class: "chip" },
              `${r.state || r.race || "?"}${r.lean ? " · " + r.lean : ""}`
            )
          ),
        ),
      ));
    }

    if (!blocks.length) return el("div");
    return el("div", { class: "section" },
      el("h3", {}, "Cross-links"),
      ...blocks,
    );
  }

  // ── Impact chains (slice 3) + counter-chains (slice 4) ─────────

  const CHAIN_KIND_META = {
    concern:  { label: "Concern",  icon: "❓", desc: "What voters care about" },
    actor:    { label: "Actor",    icon: "👤", desc: "Politician / party / movement" },
    policy:   { label: "Policy",   icon: "📜", desc: "Concrete action" },
    market:   { label: "Market",   icon: "📈", desc: "Observable outcome" },
    evidence: { label: "Evidence", icon: "📎", desc: "Source / citation" },
  };

  const COUNTER_KIND_META = {
    refute: "Refute",
    fork:   "Fork",
    extend: "Extend",
  };

  async function loadChains(iso, sectionEl) {
    let data;
    try {
      data = await api(`/api/country/${iso}/chains`);
    } catch (e) {
      const loading = sectionEl.querySelector(".chains-loading");
      if (loading) loading.textContent = "Failed to load chains: " + e.message;
      return;
    }
    if (state.activeIso !== iso) return;
    const loading = sectionEl.querySelector(".chains-loading");
    if (loading) loading.remove();

    const list = el("div", { class: "chains-list" });
    if (!data.chains.length) {
      list.appendChild(el("div", { class: "hint" },
        "No chains yet. Click ‘+ New chain’ to draft one connecting voter concern → actor → policy → market."));
    } else {
      for (const c of data.chains) {
        list.appendChild(chainCard(c, iso));
      }
    }
    sectionEl.appendChild(list);
  }

  function chainCard(c, iso) {
    const isCounter = !!c.parent_chain_id;
    const card = el("div", { class: `chain-card chain-status-${c.status}${isCounter ? " chain-counter" : ""}` },
      chainCardHeader(c),
      chainCardSummary(c),
      chainSteps(c),
      chainCardActions(c, iso),
    );
    return card;
  }

  function chainCardHeader(c) {
    const tags = [];
    if (c.source_kind === "seed") tags.push(el("span", { class: "chain-tag chain-tag-curated" }, "curated"));
    if (c.parent_chain_id) tags.push(el("span", { class: "chain-tag chain-tag-counter" }, COUNTER_KIND_META[c.counter_kind] || "counter"));
    if (c.status !== "approved") tags.push(el("span", { class: `chain-tag chain-tag-${c.status}` }, c.status.replace("_", " ")));
    return el("div", { class: "chain-head" },
      el("div", { class: "chain-title" }, c.title),
      el("div", { class: "chain-tags" }, ...tags),
    );
  }

  function chainCardSummary(c) {
    if (!c.summary) return null;
    return el("div", { class: "chain-summary" }, c.summary);
  }

  function chainSteps(c) {
    const steps = c.steps || [];
    const wrap = el("div", { class: "chain-steps" });
    steps.forEach((s, i) => {
      const meta = CHAIN_KIND_META[s.kind] || { label: s.kind, icon: "•" };
      const stepEl = el("div", { class: `chain-step chain-kind-${s.kind}` },
        el("div", { class: "chain-step-icon", title: meta.desc }, meta.icon),
        el("div", { class: "chain-step-body" },
          el("div", { class: "chain-step-kind" }, meta.label),
          el("div", { class: "chain-step-text" }, s.text),
          s.detail ? el("div", { class: "chain-step-detail" }, s.detail) : null,
          s.ref_url ? el("a", { class: "chain-step-link", href: s.ref_url, target: "_blank", rel: "noopener noreferrer" },
            (s.ref_provider || "source") + " ↗") : null,
        ),
      );
      wrap.appendChild(stepEl);
      if (i < steps.length - 1) {
        wrap.appendChild(el("div", { class: "chain-arrow" }, "↓"));
      }
    });
    return wrap;
  }

  function chainCardActions(c, iso) {
    const role = (state.me && state.me.role) || "subscriber";
    const isReviewer = role === "reviewer" || role === "admin";
    const actions = el("div", { class: "chain-actions" });

    // Vote (approved chains only, not your own)
    if (c.status === "approved" && !c.is_author) {
      const score = (c.upvotes || 0) - (c.downvotes || 0);
      const my = state.chainVotes.get(c.id) || (c.my_vote || 0);
      actions.appendChild(el("span", { class: "chain-vote" },
        el("button", {
          class: `vote-btn up${my === 1 ? " active" : ""}`,
          onclick: () => chainVote(iso, c.id, my === 1 ? 0 : 1),
        }, "▲"),
        el("span", { class: "vote-score" }, String(score)),
        el("button", {
          class: `vote-btn down${my === -1 ? " active" : ""}`,
          onclick: () => chainVote(iso, c.id, my === -1 ? 0 : -1),
        }, "▼"),
      ));
    }

    // Counter-chain (slice 4): only on approved chains
    if (c.status === "approved") {
      actions.appendChild(el("button", {
        class: "chain-counter-btn",
        onclick: () => openChainComposer(iso, c.id, c.title),
      }, "Counter ↪"));
    }

    // Author actions on draft
    if (c.is_author && c.status === "draft") {
      actions.appendChild(el("button", {
        class: "chain-submit-btn",
        onclick: () => submitChain(iso, c.id),
      }, "Submit for review"));
    }

    // Reviewer actions on under_review
    if (isReviewer && c.status === "under_review") {
      actions.appendChild(el("button", {
        class: "chain-review-btn approve",
        onclick: () => reviewChain(iso, c.id, "approve"),
      }, "Approve"));
      actions.appendChild(el("button", {
        class: "chain-review-btn reject",
        onclick: () => reviewChain(iso, c.id, "reject"),
      }, "Reject"));
      actions.appendChild(el("button", {
        class: "chain-review-btn changes",
        onclick: () => reviewChain(iso, c.id, "request_changes"),
      }, "Request changes"));
    }

    // Show review notes if author/reviewer
    if (c.review_notes) {
      actions.appendChild(el("div", { class: "chain-review-notes" }, "📝 " + c.review_notes));
    }

    if (!actions.children.length) return null;
    return actions;
  }

  async function chainVote(iso, chainId, v) {
    try {
      const r = await api(`/api/chains/${chainId}/vote`, {
        method: "POST",
        body: JSON.stringify({ vote: v }),
      });
      state.chainVotes.set(chainId, r.your_vote);
      await reloadChainsFor(iso);
    } catch (e) {
      showToast(e.message, true);
    }
  }

  async function submitChain(iso, chainId) {
    try {
      await api(`/api/chains/${chainId}/submit`, { method: "POST" });
      showToast("Submitted for review");
      await reloadChainsFor(iso);
    } catch (e) {
      showToast(e.message, true);
    }
  }

  async function reviewChain(iso, chainId, decision) {
    const notes = prompt(`${decision} — notes for the author?`) || "";
    try {
      await api(`/api/chains/${chainId}/review`, {
        method: "POST",
        body: JSON.stringify({ decision, notes }),
      });
      showToast(`Chain ${decision}`);
      await reloadChainsFor(iso);
    } catch (e) {
      showToast(e.message, true);
    }
  }

  async function reloadChainsFor(iso) {
    const sec = $("#chains-section");
    if (!sec || state.activeIso !== iso) return;
    sec.innerHTML = "";
    sec.appendChild(el("div", { class: "chains-head" },
      el("h3", {}, "Impact chains"),
      el("button", {
        class: "chain-new-btn",
        onclick: () => openChainComposer(iso, null, null),
      }, "+ New chain"),
    ));
    sec.appendChild(el("div", { class: "chains-loading" }, "Reloading…"));
    await loadChains(iso, sec);
  }

  // ── Chain composer (modal) ──────────────────────────────────────

  function openChainComposer(iso, parentChainId, parentTitle) {
    const overlay = el("div", { class: "modal-overlay", onclick: (e) => { if (e.target === overlay) closeModal(); } });
    const modal = el("div", { class: "modal" });
    overlay.appendChild(modal);

    const stepsState = [
      { kind: "concern", text: "", detail: "", ref_url: "" },
      { kind: "market", text: "", detail: "", ref_url: "" },
    ];

    let titleVal = "";
    let summaryVal = "";
    let counterKind = parentChainId ? "refute" : null;

    function rerender() {
      modal.innerHTML = "";
      modal.appendChild(el("div", { class: "modal-head" },
        el("h2", {}, parentChainId ? `Counter-chain to: ${parentTitle || "#" + parentChainId}` : `New chain — ${iso}`),
        el("button", { class: "modal-close", onclick: closeModal }, "×"),
      ));

      if (parentChainId) {
        modal.appendChild(el("div", { class: "modal-row" },
          el("label", {}, "Counter type"),
          el("select", {
            onchange: (e) => { counterKind = e.target.value; },
          },
            ...Object.entries(COUNTER_KIND_META).map(([k, label]) =>
              el("option", { value: k, selected: k === counterKind ? "selected" : false }, label)
            ),
          ),
          el("div", { class: "hint" },
            "Refute: argues the parent chain is wrong. Fork: same start, different conclusion. Extend: adds steps."),
        ));
      }

      modal.appendChild(el("div", { class: "modal-row" },
        el("label", {}, "Title"),
        el("input", {
          type: "text", maxlength: "200", value: titleVal,
          oninput: (e) => { titleVal = e.target.value; },
          placeholder: "e.g. Lula's third-term fiscal slip → BRL underperforms",
        }),
      ));

      modal.appendChild(el("div", { class: "modal-row" },
        el("label", {}, "Summary (optional)"),
        el("textarea", {
          maxlength: "2000", placeholder: "One paragraph framing the chain.",
          oninput: (e) => { summaryVal = e.target.value; },
        }, summaryVal),
      ));

      modal.appendChild(el("div", { class: "modal-row" },
        el("label", {}, `Steps (${stepsState.length}/8)`),
        el("div", { class: "steps-editor" },
          ...stepsState.map((s, i) => stepEditor(s, i, rerender, stepsState)),
          stepsState.length < 8 ? el("button", {
            class: "step-add-btn",
            onclick: () => { stepsState.push({ kind: "policy", text: "", detail: "", ref_url: "" }); rerender(); },
          }, "+ Add step") : null,
        ),
      ));

      modal.appendChild(el("div", { class: "modal-foot" },
        el("button", { onclick: closeModal }, "Cancel"),
        el("button", { class: "primary", onclick: submit }, parentChainId ? "Post counter-chain (draft)" : "Save draft"),
      ));
    }

    async function submit() {
      if (!titleVal.trim()) { showToast("Title required", true); return; }
      if (stepsState.length < 2) { showToast("Need at least 2 steps", true); return; }
      const payload = {
        iso, title: titleVal.trim(), summary: summaryVal.trim() || null,
        steps: stepsState.map((s) => ({
          kind: s.kind, text: s.text.trim(), detail: s.detail.trim() || null, ref_url: s.ref_url.trim() || null,
        })),
      };
      if (parentChainId) {
        payload.parent_chain_id = parentChainId;
        payload.counter_kind = counterKind;
      }
      try {
        await api("/api/chains", { method: "POST", body: JSON.stringify(payload) });
        showToast("Draft saved — submit when ready");
        closeModal();
        await reloadChainsFor(iso);
      } catch (e) {
        showToast(e.message, true);
      }
    }

    function closeModal() {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
    }
    function onKey(e) { if (e.key === "Escape") closeModal(); }
    document.addEventListener("keydown", onKey);

    document.body.appendChild(overlay);
    rerender();
  }

  function stepEditor(s, i, rerender, stepsState) {
    return el("div", { class: "step-editor" },
      el("div", { class: "step-editor-head" },
        el("span", { class: "step-num" }, `#${i + 1}`),
        el("select", {
          onchange: (e) => { s.kind = e.target.value; },
        },
          ...Object.entries(CHAIN_KIND_META).map(([k, m]) =>
            el("option", { value: k, selected: k === s.kind ? "selected" : false }, `${m.icon} ${m.label}`)
          ),
        ),
        stepsState.length > 2 ? el("button", {
          class: "step-rm",
          onclick: () => { stepsState.splice(i, 1); rerender(); },
        }, "✕") : null,
      ),
      el("input", {
        type: "text", maxlength: "240", placeholder: "Short text (≤240 chars)",
        value: s.text, oninput: (e) => { s.text = e.target.value; },
      }),
      el("input", {
        type: "text", maxlength: "1000", placeholder: "Detail (optional)",
        value: s.detail, oninput: (e) => { s.detail = e.target.value; },
      }),
      el("input", {
        type: "url", placeholder: "Source URL (optional, https://…)",
        value: s.ref_url, oninput: (e) => { s.ref_url = e.target.value; },
      }),
    );
  }

  // ── Reviewer queue (modal) ──────────────────────────────────────

  async function openReviewerQueue() {
    let data;
    try {
      data = await api("/api/reviewer/queue");
    } catch (e) {
      showToast(e.message, true);
      return;
    }
    const overlay = el("div", { class: "modal-overlay", onclick: (e) => { if (e.target === overlay) overlay.remove(); } });
    const modal = el("div", { class: "modal modal-wide" });
    modal.appendChild(el("div", { class: "modal-head" },
      el("h2", {}, `Review queue — ${data.count} pending`),
      el("button", { class: "modal-close", onclick: () => overlay.remove() }, "×"),
    ));
    if (!data.items.length) {
      modal.appendChild(el("div", { class: "hint" }, "Nothing pending. Inbox zero."));
    } else {
      for (const c of data.items) {
        const card = chainCard(c, c.iso);
        // Add ISO label since we're outside a country drawer
        card.prepend(el("div", { class: "queue-iso" }, c.iso));
        modal.appendChild(card);
      }
    }
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
  }

  // ── Comments ────────────────────────────────────────────────────

  async function loadComments(iso) {
    try {
      const data = await api(`/api/thoughts?target_type=country&target_id=${iso}`);
      renderThread(iso, data.items);
      renderReactionsRow(iso, data.items);
    } catch (e) {
      $("#thread-root").innerHTML = "";
      $("#thread-root").appendChild(el("div", { class: "empty" }, "Failed to load comments: " + e.message));
    }
  }

  function renderReactionsRow(iso, allThoughts) {
    const root = $("#reactions-row");
    if (!root) return;
    root.innerHTML = "";
    const counts = new Map();
    state.myReactions.clear();
    for (const t of allThoughts) {
      if (t.kind !== "reaction" || t.hidden) continue;
      counts.set(t.body, (counts.get(t.body) || 0) + 1);
      // Track our own active reactions
      if (t.user_email && state.viewerEmail === t.user_email) {
        state.myReactions.set(`${iso}:${t.body}`, t.id);
      }
    }
    for (const r of REACTION_EMOJI) {
      const isMine = state.myReactions.has(`${iso}:${r.code}`);
      const btn = el("button", {
        class: `reaction-btn${isMine ? " active" : ""}`,
        title: r.label,
        onclick: () => toggleReaction(iso, r.code),
      },
        el("span", {}, r.glyph),
        el("span", { class: "count" }, String(counts.get(r.code) || 0)),
      );
      root.appendChild(btn);
    }
  }

  async function toggleReaction(iso, code) {
    try {
      await api("/api/thoughts", {
        method: "POST",
        body: JSON.stringify({
          target_type: "country", target_id: iso,
          kind: "reaction", body: code,
        }),
      });
      await loadComments(iso);
    } catch (e) {
      showToast(e.message, true);
    }
  }

  function composerBlock(iso, parentId) {
    const ta = el("textarea", { placeholder: parentId ? "Reply…" : "Add to the discussion. Cite sources where you can.", maxlength: "4000" });
    const submitBtn = el("button", { onclick: submit }, parentId ? "Reply" : "Post");
    const cancelBtn = parentId ? el("button", { onclick: () => parentId && removeReplyBox(parentId) }, "Cancel") : null;

    async function submit() {
      const text = ta.value.trim();
      if (!text) { showToast("Empty comment", true); return; }
      submitBtn.disabled = true;
      try {
        await api("/api/thoughts", {
          method: "POST",
          body: JSON.stringify({
            target_type: "country",
            target_id: iso,
            kind: "comment",
            body: text,
            parent_id: parentId,
          }),
        });
        ta.value = "";
        if (parentId) removeReplyBox(parentId);
        await loadComments(iso);
      } catch (e) {
        showToast(e.message, true);
      } finally {
        submitBtn.disabled = false;
      }
    }
    return el("div", { class: "composer" },
      ta,
      el("div", { class: "composer-buttons" },
        el("span", { class: "composer-hint" }, "Markdown: **bold**, *italic*, [link](https://…), `code`"),
        el("div", {}, cancelBtn, " ", submitBtn),
      ),
    );
  }

  function removeReplyBox(parentId) {
    const box = document.getElementById(`reply-box-${parentId}`);
    if (box) box.remove();
  }

  function renderThread(iso, items) {
    const root = $("#thread-root");
    root.innerHTML = "";
    const comments = items.filter((t) => t.kind === "comment");
    if (!comments.length) {
      root.appendChild(el("div", { class: "empty" }, "No comments yet — start the discussion."));
      return;
    }

    // Index by parent_id
    const byParent = new Map();
    for (const c of comments) {
      const p = c.parent_id || 0;
      if (!byParent.has(p)) byParent.set(p, []);
      byParent.get(p).push(c);
    }

    function renderOne(c, depth) {
      const node = el("div", { class: depth ? "comment reply" : "comment" });
      node.appendChild(el("div", { class: "comment-head" },
        el("span", { class: "comment-author" }, c.user_email),
        el("span", {}, relTime(c.created_at) + (c.edited_at ? " · edited" : "")),
      ));
      const bodyEl = el("div", { class: c.hidden ? "comment-body hidden" : "comment-body" });
      if (c.hidden) {
        bodyEl.textContent = `[hidden — ${c.hidden_reason || "moderation"}]`;
      } else {
        bodyEl.innerHTML = renderMarkdownLite(c.body || "");
      }
      node.appendChild(bodyEl);

      // Actions row
      const score = (c.upvotes || 0) - (c.downvotes || 0);
      const my = state.myVotes.get(c.id) || 0;
      const actions = el("div", { class: "comment-actions" },
        el("span", { class: "vote-controls" },
          el("button", {
            class: `vote-btn up${my === 1 ? " active" : ""}`,
            onclick: () => vote(iso, c.id, my === 1 ? 0 : 1),
          }, "▲"),
          el("span", { class: "vote-score" }, String(score)),
          el("button", {
            class: `vote-btn down${my === -1 ? " active" : ""}`,
            onclick: () => vote(iso, c.id, my === -1 ? 0 : -1),
          }, "▼"),
        ),
        depth < 6 && !c.hidden ? el("button", { onclick: () => spawnReply(iso, c.id, node) }, "Reply") : null,
        !c.hidden ? el("button", { onclick: () => flag(iso, c.id) }, "Flag") : null,
      );
      node.appendChild(actions);

      // Children
      const kids = byParent.get(c.id) || [];
      for (const k of kids) node.appendChild(renderOne(k, Math.min(depth + 1, 6)));
      return node;
    }

    const top = byParent.get(0) || [];
    for (const c of top) root.appendChild(renderOne(c, 0));
  }

  function spawnReply(iso, parentId, anchorNode) {
    if (document.getElementById(`reply-box-${parentId}`)) return;
    const box = composerBlock(iso, parentId);
    box.id = `reply-box-${parentId}`;
    anchorNode.appendChild(box);
  }

  async function vote(iso, thoughtId, v) {
    try {
      const res = await api(`/api/thoughts/${thoughtId}/vote`, {
        method: "POST",
        body: JSON.stringify({ vote: v }),
      });
      state.myVotes.set(thoughtId, res.your_vote);
      await loadComments(iso);
    } catch (e) {
      showToast(e.message, true);
    }
  }

  async function flag(iso, thoughtId) {
    const reason = prompt("Reason for flagging? (optional)") || "";
    try {
      const res = await api(`/api/thoughts/${thoughtId}/flag`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      });
      showToast(res.auto_hidden ? "Flagged — auto-hidden" : `Flagged (${res.flag_count} so far)`);
      await loadComments(iso);
    } catch (e) {
      showToast(e.message, true);
    }
  }

  // ── Go ─────────────────────────────────────────────────────────

  bootstrap();
})();
