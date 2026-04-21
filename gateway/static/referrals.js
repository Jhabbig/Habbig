// /settings/referrals — private referrer panel.
//
// Fetches /api/referrals/me once on load, renders the invite link, reward
// list, progress bar, and invitee feed. Copy-to-clipboard uses the
// Clipboard API with a <textarea> fallback for insecure contexts.

(function () {
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function fmtDate(ts) {
    if (!ts) return "—";
    try {
      return new Date(ts * 1000).toLocaleDateString([], {
        month: "short", day: "numeric",
      });
    } catch { return "—"; }
  }

  async function copyToClipboard(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Fallback for insecure contexts / older browsers.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch {}
      document.body.removeChild(ta);
    }
    const prev = btn.textContent;
    btn.textContent = "Copied ✓";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = prev;
      btn.classList.remove("copied");
    }, 1600);
  }

  function renderRewards(stats, progress) {
    const ul = document.getElementById("ref-rewards");
    if (!stats || stats.total_rewarded === 0) {
      ul.innerHTML = `
        <li class="ref-empty">
          No rewards yet — invite your first trader to unlock a free month
          (${esc(progress.remaining)} of ${esc(progress.next_milestone || 1)} needed).
        </li>`;
      return;
    }
    const parts = [];
    if (stats.total_reward_months > 0) {
      parts.push(`
        <li>✓ ${esc(stats.total_reward_months)} month${stats.total_reward_months === 1 ? "" : "s"} free
        (${esc(stats.total_rewarded)} successful referral${stats.total_rewarded === 1 ? "" : "s"})
        — applied to your subscription.</li>
      `);
    }
    ul.innerHTML = parts.join("") || `<li class="ref-empty">Reward processing — you'll see this update within 24 hours.</li>`;
  }

  function renderProgress(progress) {
    const text = document.getElementById("ref-progress-text");
    const fill = document.getElementById("ref-progress-fill");
    if (!progress.next_milestone) {
      text.textContent = progress.next_reward_label;
      fill.style.width = "100%";
      return;
    }
    const pct = Math.min(100, Math.round(
      (progress.current / progress.next_milestone) * 100
    ));
    fill.style.width = `${pct}%`;
    text.textContent = `${progress.current} of ${progress.next_milestone} — unlock ${progress.next_reward_label}`;
  }

  function renderInvitees(referrals) {
    const host = document.getElementById("ref-invitees");
    if (!referrals || referrals.length === 0) {
      host.innerHTML = `<div class="ref-empty">No invitees yet. Share your link above.</div>`;
      return;
    }
    const rows = referrals.map((r) => {
      const cls =
        r.reward_granted ? "rewarded" :
        r.converted_to_paid ? "paying" : "";
      const reward = r.reward_label ? ` · ${esc(r.reward_label)}` : "";
      return `
        <div class="ref-invitee-row">
          <div class="ref-invitee-email">${esc(r.email)}</div>
          <div class="ref-invitee-date">Invited ${esc(fmtDate(r.created_at))}</div>
          <div class="ref-invitee-status ${cls}">${esc(r.status)}${reward}</div>
        </div>
      `;
    });
    host.innerHTML = rows.join("");
  }

  async function load() {
    let data;
    try {
      const r = await fetch("/api/referrals/me");
      if (r.status === 401) {
        window.location.href = "/token";
        return;
      }
      data = await r.json();
    } catch {
      document.getElementById("ref-rewards").innerHTML =
        `<li class="ref-empty">Couldn't load your referral data. Refresh to retry.</li>`;
      return;
    }

    const linkInput = document.getElementById("ref-link");
    linkInput.value = data.share_url || "";

    const copyBtn = document.getElementById("ref-copy");
    copyBtn.addEventListener("click", () => {
      if (!linkInput.value) return;
      copyToClipboard(linkInput.value, copyBtn);
    });
    linkInput.addEventListener("click", () => linkInput.select());

    renderRewards(data.stats, data.progress);
    renderProgress(data.progress);
    renderInvitees(data.referrals);
  }

  load();
})();
