// Public invite-acceptance flow.
//
// Server rendered invite_public.html with data-valid="1"/"" to tell us
// up-front whether the referrer code is live. We re-validate via API on
// load to catch the rare case where the inviter got suspended between
// page render and submit. The form itself posts email → /api/invite/{code}/accept.

(function () {
  const card = document.getElementById("invite-card");
  const code = card.dataset.code || "";
  const valid = card.dataset.valid === "1";

  function escapeHTML(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function renderInvalid() {
    card.innerHTML = `
      <div class="invite-invalid">
        <strong>This invite link is not valid.</strong>
        <span>If the sender told you they sent one, ask them to regenerate it.</span>
      </div>
    `;
  }

  function renderForm(referrerName) {
    card.innerHTML = `
      <h1 class="invite-title">${escapeHTML(referrerName)} invited you to narve.ai</h1>
      <p class="invite-sub">
        narve.ai is an invite-only prediction market intelligence platform
        used by serious Polymarket and Kalshi traders.
      </p>
      <div id="invite-msg"></div>
      <form class="invite-form" id="invite-form">
        <div>
          <label for="email">Your email</label>
          <input id="email" name="email" type="email" class="invite-input"
                 autocomplete="email" required placeholder="you@example.com">
        </div>
        <button type="submit" class="invite-btn" id="invite-btn">
          Request invite →
        </button>
      </form>
      <p class="invite-foot">
        Subscription plans start at £75 / $99 per month.
        Tokens are single-use and tied to your account.
      </p>
    `;
    document.getElementById("invite-form").addEventListener("submit", onSubmit);
  }

  function showMsg(kind, text) {
    const msg = document.getElementById("invite-msg");
    if (!msg) return;
    msg.innerHTML = `<div class="invite-notice ${escapeHTML(kind)}">${escapeHTML(text)}</div>`;
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const emailInput = document.getElementById("email");
    const btn = document.getElementById("invite-btn");
    const email = (emailInput.value || "").trim();
    if (!email || email.indexOf("@") === -1) {
      showMsg("error", "Enter a valid email address.");
      return;
    }
    btn.disabled = true;
    btn.textContent = "Sending…";
    try {
      const r = await fetch(`/api/invite/${encodeURIComponent(code)}/accept`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.ok) {
        card.innerHTML = `
          <h1 class="invite-title">Check your email</h1>
          <p class="invite-sub">
            We just sent a single-use access token to
            <strong>${escapeHTML(email)}</strong>. Enter it at
            <a href="/gate">narve.ai/gate</a> to create your account.
          </p>
          <p class="invite-foot">Didn't arrive within 5 minutes? Check your spam folder, then ask your inviter to regenerate the link.</p>
        `;
        return;
      }
      showMsg("error", data.error || "Couldn't send invite. Try again.");
    } catch (e) {
      showMsg("error", "Network error. Try again.");
    } finally {
      btn.disabled = false;
      btn.textContent = "Request invite →";
    }
  }

  async function start() {
    // Server already told us valid/invalid. Re-validate in case state
    // changed between render and JS run.
    try {
      const r = await fetch(`/api/invite/${encodeURIComponent(code)}`);
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.valid) {
        renderInvalid();
        return;
      }
      renderForm(data.referrer_display_name || "A narve.ai member");
    } catch (e) {
      // Fall back to the server-rendered verdict.
      if (valid) {
        renderForm("A narve.ai member");
      } else {
        renderInvalid();
      }
    }
  }

  start();
})();
