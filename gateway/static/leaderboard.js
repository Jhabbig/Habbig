// Private leaderboard. Paid-only. Tabs switch the time-window; body
// re-renders on tab click (no route change, URL stays /leaderboard).

(function () {
  let currentPeriod = "all";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function renderRows(rows) {
    const body = document.getElementById("lb-body");
    if (!rows || rows.length === 0) {
      body.innerHTML = `
        <tr><td colspan="5" class="lb-empty">
          No ranked users in this window yet. Opt in at
          <a href="/settings#privacy">Settings → Privacy</a> to appear here.
        </td></tr>`;
      return;
    }
    body.innerHTML = rows.map((r) => {
      const acc = r.accuracy != null ? `${r.accuracy.toFixed(1)}%` : "—";
      const you = r.is_you ? `<tr class="is-you">` : `<tr>`;
      return `${you}
        <td class="lb-rank">${esc(r.rank)}</td>
        <td class="lb-handle">@${esc(r.handle)}${r.is_you ? " <span style=\"color:var(--text-tertiary);font-weight:400\">← you</span>" : ""}</td>
        <td class="lb-numeric">${esc(r.total_predictions)}</td>
        <td class="lb-numeric">${esc(r.correct_predictions)}</td>
        <td class="lb-accuracy">${esc(acc)}</td>
      </tr>`;
    }).join("");
  }

  function renderFoot(data) {
    const foot = document.getElementById("lb-foot");
    const parts = [];
    if (data.participants != null && data.total_users_approx != null) {
      parts.push(
        `${data.participants} of ${data.total_users_approx} active subscribers are on the leaderboard.`
      );
    }
    parts.push(`Opt in at <a href="/settings#privacy">Settings → Privacy</a>.`);
    foot.innerHTML = parts.join(" ");
  }

  function renderMyRank(rank) {
    const host = document.getElementById("lb-myrank");
    if (!rank) {
      host.hidden = true;
      host.innerHTML = "";
      return;
    }
    host.hidden = false;
    const pct = (rank.accuracy * 100).toFixed(1);
    host.innerHTML = `Your position: <strong>#${esc(rank.rank)}</strong> · ${esc(pct)}% accuracy`;
  }

  async function load(period) {
    currentPeriod = period;
    document.querySelectorAll(".lb-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.period === period);
    });
    const body = document.getElementById("lb-body");
    body.innerHTML = `<tr><td colspan="5" class="lb-empty">Loading…</td></tr>`;

    let data;
    try {
      const r = await fetch(`/api/leaderboard?period=${encodeURIComponent(period)}`);
      if (r.status === 401) {
        window.location.href = "/token";
        return;
      }
      data = await r.json();
    } catch {
      body.innerHTML = `<tr><td colspan="5" class="lb-empty">Couldn't load leaderboard.</td></tr>`;
      return;
    }
    renderMyRank(data.my_rank);
    renderRows(data.rows || []);
    renderFoot(data);
  }

  document.querySelectorAll(".lb-tab").forEach((t) => {
    t.addEventListener("click", () => load(t.dataset.period));
  });
  load("all");
})();
