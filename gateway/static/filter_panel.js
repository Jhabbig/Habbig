/* ══════════════════════════════════════════════════════════════════
   Filter panel — vanilla, no framework. Mount with:

     <div id="filter-mount" data-scope="markets" data-endpoint="/api/v1/predictions"></div>
     <script src="/_gateway_static/filter_panel.js" defer></script>
     <script>
       document.addEventListener("DOMContentLoaded", () => {
         NarveFilterPanel.mount("#filter-mount", {
           onApply: (filters, queryString) => {
             history.replaceState(null, "", "?" + queryString);
             fetch(panel.endpoint + "?" + queryString, {credentials: "same-origin"})
               .then(r => r.json())
               .then(renderResultsCallback);
           },
         });
       });
     </script>

   Public shape:
     NarveFilterPanel.mount(selector, options) → panel instance
     panel.setFilters(obj)       – programmatic state write
     panel.getFilters()           – current dict
     panel.queryString()          – URL-encoded string
     panel.previewCount()         – forces a /preview fetch now

   Reads the scope from `data-scope` on the mount node. Options:
     onApply(filters, queryString)     – fires when user clicks Apply
     onPreviewUpdate({count, total})   – fires after every preview fetch
     loadViewId (number | null)        – pre-apply this saved view on mount

   All network calls are scoped to /api/saved-views (preview + CRUD) and
   use `credentials: same-origin`. CSRF header is inserted from the
   `_csrf` cookie for non-GET requests.
   ══════════════════════════════════════════════════════════════════ */

(function (window) {
  "use strict";

  // ── Schema (mirror of saved_views_schema.py; kept in sync by hand) ──
  // Only the minimum needed to render widgets. The server remains the
  // single source of truth on validation + SQL.

  const SCHEMAS = {
    markets: [
      { name: "categories", kind: "chips", label: "Category", options: [
        "politics", "geopolitics", "economics", "crypto", "sports",
        "tech", "climate", "elections", "ai", "health", "science"
      ] },
      { name: "platform", kind: "select", label: "Platform", options: [
        "", "polymarket", "kalshi", "both"
      ], placeholder: "Any" },
      { name: "close_within", kind: "duration", label: "Closing within", options: [
        { value: "24h", label: "24h" },
        { value: "7d", label: "7d" },
        { value: "30d", label: "30d" },
        { value: "90d", label: "90d" },
      ] },
      { name: "min_edge", kind: "number", label: "Min edge (pp)",
        min: 0, max: 1, step: 0.01, placeholder: "0.10" },
      { name: "min_source_count", kind: "number", label: "Min sources",
        min: 0, max: 50, step: 1, placeholder: "3" },
      { name: "min_source_cred", kind: "number", label: "Min source credibility",
        min: 0, max: 1, step: 0.01, placeholder: "0.70" },
      { name: "market_prob_range", kind: "range", label: "Market prob range",
        min: 0, max: 1, step: 0.01 },
      { name: "narve_prob_range", kind: "range", label: "narve prob range",
        min: 0, max: 1, step: 0.01 },
      { name: "has_insider_signal", kind: "toggle", label: "Has insider signal" },
      { name: "has_environmental", kind: "toggle", label: "Has environmental" },
    ],
    feed: [
      { name: "categories", kind: "chips", label: "Category", options: [
        "politics", "geopolitics", "economics", "crypto", "sports",
        "tech", "climate", "elections", "ai", "health"
      ] },
      { name: "posted_within", kind: "duration", label: "Posted within", options: [
        { value: "24h", label: "24h" },
        { value: "7d", label: "7d" },
        { value: "30d", label: "30d" },
      ] },
      { name: "sources", kind: "text_list", label: "Source handles (comma)",
        placeholder: "@fedwatcher, @zerohedge" },
      { name: "source_cred_range", kind: "range", label: "Source credibility",
        min: 0, max: 1, step: 0.01 },
      { name: "resolution", kind: "select", label: "Resolution",
        options: ["", "pending", "resolved", "any"], placeholder: "Any" },
    ],
    sources: [
      { name: "min_credibility", kind: "number", label: "Min credibility",
        min: 0, max: 1, step: 0.01, placeholder: "0.70" },
      { name: "max_credibility", kind: "number", label: "Max credibility",
        min: 0, max: 1, step: 0.01, placeholder: "" },
      { name: "min_predictions", kind: "number", label: "Min predictions",
        min: 0, max: 10000, step: 1, placeholder: "20" },
      { name: "categories_active", kind: "chips", label: "Active in category",
        options: ["politics", "crypto", "tech", "economics", "sports", "ai"] },
    ],
    predictions: [
      { name: "categories", kind: "chips", label: "Category", options: [
        "politics", "crypto", "tech", "economics", "sports", "ai"
      ] },
      { name: "sources", kind: "text_list", label: "Source handles",
        placeholder: "@fedwatcher" },
      { name: "posted_within", kind: "duration", label: "Posted within", options: [
        { value: "24h", label: "24h" },
        { value: "7d", label: "7d" },
        { value: "30d", label: "30d" },
      ] },
      { name: "resolution", kind: "select", label: "Resolution",
        options: ["", "pending", "resolved", "any"], placeholder: "Any" },
      { name: "source_cred_range", kind: "range", label: "Source credibility",
        min: 0, max: 1, step: 0.01 },
    ],
  };

  // ── Cookie → CSRF header ──────────────────────────────────────────

  function csrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function apiHeaders(hasBody) {
    const h = { "X-CSRF-Token": csrfToken() };
    if (hasBody) h["Content-Type"] = "application/json";
    return h;
  }

  // ── Filter ↔ URL encoding (mirrors filters_to_query in the schema) ──

  function serialize(filters) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(filters || {})) {
      if (v === null || v === undefined) continue;
      if (Array.isArray(v)) {
        if (v.length === 2 && v.every(x => typeof x === "number")) {
          params.set(k, `${v[0]},${v[1]}`);
        } else if (v.length) {
          params.set(k, v.join(","));
        }
      } else if (typeof v === "boolean") {
        params.set(k, v ? "1" : "0");
      } else if (v !== "") {
        params.set(k, String(v));
      }
    }
    return params.toString();
  }

  function parseFromQuery(scope) {
    const params = new URLSearchParams(window.location.search);
    const out = {};
    const schema = SCHEMAS[scope] || [];
    for (const field of schema) {
      const raw = params.get(field.name);
      if (raw === null) continue;
      if (field.kind === "chips" || field.kind === "text_list") {
        const list = raw.split(",").map(s => s.trim()).filter(Boolean);
        if (list.length) out[field.name] = list;
      } else if (field.kind === "toggle") {
        out[field.name] = raw === "1" || raw === "true";
      } else if (field.kind === "range") {
        const parts = raw.split(",").map(Number);
        if (parts.length === 2 && !Number.isNaN(parts[0]) && !Number.isNaN(parts[1])) {
          out[field.name] = parts;
        }
      } else if (field.kind === "number") {
        const n = parseFloat(raw);
        if (!Number.isNaN(n)) out[field.name] = n;
      } else {
        out[field.name] = raw;
      }
    }
    return out;
  }

  // ── Widget renderers ──────────────────────────────────────────────

  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k.startsWith("on") && typeof v === "function") {
        e.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (v === true) {
        e.setAttribute(k, "");
      } else if (v !== null && v !== undefined && v !== false) {
        e.setAttribute(k, String(v));
      }
    }
    for (const c of children) {
      if (c === null || c === undefined || c === false) continue;
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
  }

  function renderField(field, value, onChange) {
    const label = el("div", { class: "filter-section__label" }, field.label);
    let body;

    switch (field.kind) {
      case "chips":
        body = el("div", { class: "filter-chips" });
        const picked = new Set(Array.isArray(value) ? value : []);
        for (const opt of field.options) {
          const chip = el(
            "button",
            {
              type: "button",
              class: "filter-chip",
              "aria-pressed": picked.has(opt) ? "true" : "false",
              "data-value": opt,
            },
            opt,
          );
          chip.addEventListener("click", () => {
            if (picked.has(opt)) picked.delete(opt);
            else picked.add(opt);
            chip.setAttribute("aria-pressed", picked.has(opt) ? "true" : "false");
            onChange(picked.size ? Array.from(picked) : null);
          });
          body.appendChild(chip);
        }
        break;

      case "select":
        body = el("select", { class: "filter-select" });
        for (const opt of field.options) {
          body.appendChild(el("option", { value: opt }, opt || (field.placeholder || "Any")));
        }
        body.value = value || "";
        body.addEventListener("change", () => onChange(body.value || null));
        break;

      case "duration":
        body = el("div", { class: "filter-duration" });
        for (const opt of field.options) {
          const btn = el(
            "button",
            {
              type: "button",
              "aria-pressed": (typeof value === "string" && value === opt.value) ? "true" : "false",
              "data-value": opt.value,
            },
            opt.label,
          );
          btn.addEventListener("click", () => {
            const already = btn.getAttribute("aria-pressed") === "true";
            body.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", "false"));
            if (!already) {
              btn.setAttribute("aria-pressed", "true");
              onChange(opt.value);
            } else {
              onChange(null);
            }
          });
          body.appendChild(btn);
        }
        break;

      case "number": {
        const input = el("input", {
          type: "number",
          class: "filter-range__input",
          min: field.min, max: field.max, step: field.step,
          placeholder: field.placeholder || "",
        });
        if (typeof value === "number") input.value = value;
        input.addEventListener("input", () => {
          if (input.value === "") return onChange(null);
          const n = parseFloat(input.value);
          onChange(Number.isNaN(n) ? null : n);
        });
        body = el("div", { class: "filter-range" }, input);
        break;
      }

      case "range": {
        const lo = el("input", {
          type: "number", class: "filter-range__input",
          min: field.min, max: field.max, step: field.step, placeholder: "lo",
        });
        const hi = el("input", {
          type: "number", class: "filter-range__input",
          min: field.min, max: field.max, step: field.step, placeholder: "hi",
        });
        if (Array.isArray(value) && value.length === 2) {
          lo.value = value[0]; hi.value = value[1];
        }
        const emit = () => {
          if (lo.value === "" && hi.value === "") return onChange(null);
          const a = lo.value === "" ? field.min : parseFloat(lo.value);
          const b = hi.value === "" ? field.max : parseFloat(hi.value);
          if (Number.isNaN(a) || Number.isNaN(b)) return;
          onChange([a, b]);
        };
        lo.addEventListener("input", emit);
        hi.addEventListener("input", emit);
        body = el("div", { class: "filter-range" }, lo, el("span", { class: "filter-range__sep" }, "–"), hi);
        break;
      }

      case "text_list": {
        const input = el("input", {
          type: "text",
          class: "filter-range__input",
          placeholder: field.placeholder || "",
        });
        if (Array.isArray(value)) input.value = value.join(", ");
        input.addEventListener("input", () => {
          const list = input.value.split(",").map(s => s.trim()).filter(Boolean);
          onChange(list.length ? list : null);
        });
        body = el("div", { class: "filter-range" }, input);
        break;
      }

      case "toggle": {
        const input = el("input", { type: "checkbox" });
        input.checked = !!value;
        input.addEventListener("change", () => onChange(input.checked || null));
        body = el(
          "label", { class: "filter-toggle" },
          el("span", { class: "filter-toggle__label" }, field.label),
          input,
        );
        // The label already contains the text; return it directly (no heading).
        return body;
      }

      default:
        body = el("div", null, `unsupported: ${field.kind}`);
    }

    return el("div", { class: "filter-section" }, label, body);
  }

  // ── Panel instance ────────────────────────────────────────────────

  function Panel(mount, options) {
    const scope = mount.dataset.scope;
    if (!SCHEMAS[scope]) {
      console.warn("[filter_panel] unknown scope:", scope);
      return;
    }

    const schema = SCHEMAS[scope];
    const state = { filters: parseFromQuery(scope) };
    let previewTimer = null;
    let countEl = null;

    const self = {
      scope,
      endpoint: mount.dataset.endpoint || "",
      getFilters() { return JSON.parse(JSON.stringify(state.filters)); },
      queryString() { return serialize(state.filters); },
      setFilters(obj) {
        state.filters = JSON.parse(JSON.stringify(obj || {}));
        renderBody();
        schedulePreview();
      },
      previewCount: fetchPreview,
    };

    function setField(name, value) {
      if (value === null || value === undefined || (Array.isArray(value) && !value.length)) {
        delete state.filters[name];
      } else {
        state.filters[name] = value;
      }
      schedulePreview();
    }

    function schedulePreview() {
      if (options && typeof options.onPreviewUpdate === "function") {
        countEl && countEl.classList.add("filter-panel__count--loading");
      }
      if (previewTimer) clearTimeout(previewTimer);
      previewTimer = setTimeout(fetchPreview, 180);
    }

    async function fetchPreview() {
      try {
        const r = await fetch("/api/saved-views/preview", {
          method: "POST",
          credentials: "same-origin",
          headers: apiHeaders(true),
          body: JSON.stringify({ scope, filters: state.filters }),
        });
        if (!r.ok) return;
        const data = await r.json();
        if (countEl) {
          countEl.innerHTML =
            `Showing <strong>${Number(data.count || 0).toLocaleString()}</strong> of ` +
            `${Number(data.total || 0).toLocaleString()}`;
        }
        if (options && typeof options.onPreviewUpdate === "function") {
          options.onPreviewUpdate(data);
        }
      } catch (e) {
        console.warn("[filter_panel] preview failed:", e);
      }
    }

    function apply() {
      const qs = serialize(state.filters);
      if (options && typeof options.onApply === "function") {
        options.onApply(self.getFilters(), qs);
      } else {
        // Default: rewrite URL and reload (server re-renders with filters).
        const base = window.location.pathname;
        window.location.assign(qs ? `${base}?${qs}` : base);
      }
    }

    function reset() {
      state.filters = {};
      renderBody();
      schedulePreview();
      if (options && typeof options.onApply === "function") {
        options.onApply({}, "");
      }
    }

    function copyLink() {
      const qs = serialize(state.filters);
      const href = `${window.location.origin}${window.location.pathname}${qs ? "?" + qs : ""}`;
      navigator.clipboard && navigator.clipboard.writeText(href);
    }

    async function save() {
      const name = prompt("Name this view:");
      if (!name || !name.trim()) return;
      const isDefault = confirm("Make this the default for this tab?");
      const isPinned = confirm("Pin to sidebar?");
      const r = await fetch("/api/saved-views", {
        method: "POST",
        credentials: "same-origin",
        headers: apiHeaders(true),
        body: JSON.stringify({
          scope, name: name.trim(), filters: state.filters,
          is_default: isDefault, is_pinned: isPinned,
        }),
      });
      if (!r.ok) {
        alert("Save failed. " + (r.status === 403 ? "Subscription required or limit reached." : ""));
        return;
      }
      const body = await r.json();
      const shareUrl = `${window.location.origin}/v/${body.view.share_token}`;
      if (navigator.clipboard) navigator.clipboard.writeText(shareUrl);
      alert(`Saved. Share link copied:\n${shareUrl}`);
    }

    // ── Render ────────────────────────────────────────────────────
    function renderBody() {
      const header = el(
        "div", { class: "filter-panel__header" },
        el("div", { class: "filter-panel__title" }, "Filters"),
        (countEl = el("div", { class: "filter-panel__count" },
                      "Loading…")),
      );

      const body = el("div", { class: "filter-panel__body" });
      for (const field of schema) {
        body.appendChild(renderField(
          field,
          state.filters[field.name],
          (v) => setField(field.name, v),
        ));
      }

      const actions = el(
        "div", { class: "filter-actions" },
        el("div", { class: "filter-actions__row" },
          el("button", {
            type: "button", class: "filter-btn filter-btn--primary",
            onClick: apply,
          }, "Apply"),
          el("button", {
            type: "button", class: "filter-btn filter-btn--ghost",
            onClick: reset,
          }, "Reset"),
        ),
        el("div", { class: "filter-actions__row" },
          el("button", {
            type: "button", class: "filter-btn",
            onClick: save,
          }, "Save view"),
          el("button", {
            type: "button", class: "filter-btn",
            onClick: copyLink,
          }, "Copy link"),
        ),
      );

      mount.textContent = "";
      mount.className = "filter-panel";
      mount.appendChild(header);
      mount.appendChild(body);
      mount.appendChild(actions);
    }

    renderBody();
    // Kick preview once on mount so the count is accurate even with no
    // query params applied.
    schedulePreview();

    return self;
  }

  // ── Pinned-views sidebar injector ────────────────────────────────
  //
  // Call NarveFilterPanel.mountSidebar() from any dashboard that wants
  // pinned saved views rendered under the main nav. The template
  // supplies an empty <div id="pinned-views"></div> slot; this function
  // fetches /api/saved-views/pinned and replaces the slot with a list.
  //
  // Fails silently when the user is signed-out — the endpoint returns
  // 401, we just leave the slot empty. No broken UI.
  //
  // The scope→URL mapping mirrors saved_views_routes._SCOPE_URL so a
  // user clicking a pinned view lands on the right tab with the saved
  // filters already applied via URL query params.

  const SCOPE_URL = {
    markets:     "/dashboards",
    feed:        "/signal-search",
    sources:     "/leaderboard",
    predictions: "/predictions",
  };

  function applyFiltersToUrl(base, filters) {
    const qs = serialize(filters || {});
    return qs ? `${base}?${qs}` : base;
  }

  async function mountSidebar(selector) {
    const node = typeof selector === "string"
      ? document.querySelector(selector)
      : (selector || document.querySelector("#pinned-views"));
    if (!node) return;

    let rows = [];
    try {
      const r = await fetch("/api/saved-views/pinned", {
        credentials: "same-origin",
      });
      if (!r.ok) return;
      const data = await r.json();
      rows = Array.isArray(data.views) ? data.views : [];
    } catch (_) {
      return;
    }
    if (!rows.length) return;

    const wrap = el("div", { class: "sidebar-pinned-views" });
    wrap.appendChild(
      el("div", { class: "nav-section-header" }, "Pinned views"),
    );
    for (const view of rows) {
      const href = applyFiltersToUrl(
        SCOPE_URL[view.scope] || "/dashboards",
        view.filters,
      );
      const href_with_id = href.includes("?")
        ? `${href}&view_id=${view.id}`
        : `${href}?view_id=${view.id}`;
      wrap.appendChild(
        el("a", {
          class: "nav-item",
          href: href_with_id,
          title: `${view.scope} · ${view.name}`,
        }, view.name),
      );
    }
    wrap.appendChild(
      el("a", {
        class: "sidebar-pinned-manage",
        href: "/settings/saved-views",
      }, "Manage views"),
    );

    node.replaceWith(wrap);
  }

  // ── Public surface ───────────────────────────────────────────────

  window.NarveFilterPanel = {
    mount(selector, options) {
      const node = typeof selector === "string"
        ? document.querySelector(selector)
        : selector;
      if (!node) {
        console.warn("[filter_panel] mount node not found:", selector);
        return null;
      }
      return Panel(node, options || {});
    },
    mountSidebar,
    SCHEMAS,
  };
})(window);
