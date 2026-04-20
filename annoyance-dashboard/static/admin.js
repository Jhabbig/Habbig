// Admin FP-review queue. Localhost + super_admin only per auth.py.
// If auth fails we'll get a 401 or 402 and the grid just shows "no data".

async function fetchJSON(path, init) {
  try {
    const res = await fetch(path, init);
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

async function loadQueue() {
  const grid = document.getElementById("fp-queue");
  const data = await fetchJSON("/admin/fp-queue");
  const flags = (data && data.flags) || [];
  if (!flags.length) {
    grid.innerHTML = '<div class="empty-small" style="grid-column:1 / -1;">No open flags — queue is clear.</div>';
    return;
  }
  grid.innerHTML = flags.map((f) => {
    const conf = f.confidence_score != null
      ? ` · conf=${Number(f.confidence_score).toFixed(0)}`
      : "";
    // Corroboration pills. A source with posts/authors >= 3 is flagged
    // "suspicious" — that's a strong hint of coordinated gaming (one account
    // driving most of the signal from a single platform). See
    // SECURITY_AUDIT_2026-04-20.md P4.1 for why this matters.
    const sources = Array.isArray(f.sources_breakdown) ? f.sources_breakdown : [];
    const sourcesHTML = sources.length
      ? `<div class="fp-sources">${sources.map((s) => {
          const posts = Number(s.count || 0);
          const authors = s.unique_authors != null ? Number(s.unique_authors) : null;
          const ratioOK = authors != null && authors > 0;
          const suspicious = ratioOK && authors > 0 && (posts / authors) >= 3;
          const label = ratioOK
            ? `${posts} posts · ${authors} author${authors === 1 ? "" : "s"}`
            : `${posts} posts`;
          const tip = suspicious
            ? `Possible gaming: ${posts} posts from only ${authors} account${authors === 1 ? "" : "s"} — review manually.`
            : `${posts} posts from ${authors ?? "?"} distinct accounts on ${s.source}.`;
          return `<span class="fp-src-pill${suspicious ? " suspicious" : ""}" title="${esc(tip)}">
            <span class="fp-src-name">${esc(s.source || "?")}</span>
            <span class="fp-src-ratio">${esc(label)}</span>
          </span>`;
        }).join("")}</div>`
      : "";
    return `
    <div class="fp-row" data-flag-id="${f.id}">
      <div class="fp-cell">
        <div class="fp-entity">${esc(f.entity || "—")}</div>
        <span class="fp-meta">spike #${f.spike_id} · z=${(f.z_score || 0).toFixed(1)}${conf}</span>
        <span class="fp-meta">detected ${esc(fmtTime(f.detected_at))}</span>
      </div>
      <div class="fp-cell">
        <div class="${f.reason ? "fp-reason" : "fp-reason fp-reason-empty"}">
          ${f.reason ? esc(f.reason) : "(no reason given)"}
        </div>
        ${f.summary ? `<div class="fp-submitter" style="margin-top:8px;color:var(--text-dim)">${esc(f.summary)}</div>` : ""}
        ${sourcesHTML}
        <div class="fp-submitter">
          flagged ${esc(fmtTime(f.flagged_at))} by ${esc(f.user_email || "anonymous")}
        </div>
      </div>
      <div class="fp-cell" style="border-left:none;">
        <button class="fp-resolve" data-flag-id="${f.id}">Resolve</button>
      </div>
    </div>
  `;
  }).join("");

  grid.querySelectorAll(".fp-resolve").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const flagId = btn.dataset.flagId;
      const note = prompt("Resolution note (optional)");
      if (note === null) return;  // cancelled
      btn.disabled = true;
      btn.textContent = "…";
      const result = await fetchJSON("/admin/fp-resolve", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ flag_id: Number(flagId), note: note.trim() || null }),
      });
      if (result && result.ok) {
        // Fade out the row then reload the queue
        btn.textContent = "resolved";
        const row = btn.closest(".fp-row");
        if (row) row.style.opacity = "0.4";
        setTimeout(loadQueue, 500);
      } else {
        btn.disabled = false;
        btn.textContent = "Resolve";
        alert("Couldn't resolve — check server logs.");
      }
    });
  });
}

loadQueue();
