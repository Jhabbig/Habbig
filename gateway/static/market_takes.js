/* Community Takes — market detail page widget.
 *
 * All endpoints on /api/v1/. CSRF via the `_csrf` cookie (double-submit).
 * Zero build step: vanilla JS + DOM templating. Matches the rest of the
 * product's client style (narveAff in affiliate, etc.).
 */
(function () {
  "use strict";

  var root = document.getElementById("market-takes");
  if (!root) return; // page doesn't have takes section — nothing to do

  var marketSlug = root.getAttribute("data-market-slug") || "";
  var currentUserId = parseInt(root.getAttribute("data-user-id") || "0", 10);
  if (!marketSlug) return;

  var state = {
    takes: [],
    total: 0,
    canPost: false,
    sort: "quality",
    positionFilter: null, // null | yes | no | neutral
  };

  // ── Helpers ──────────────────────────────────────────────────────────
  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function timeAgo(ts) {
    if (!ts) return "";
    var diff = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (diff < 60) return diff + "s ago";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    if (diff < 2592000) return Math.floor(diff / 86400) + "d ago";
    return Math.floor(diff / 2592000) + "mo ago";
  }

  function request(method, url, body) {
    var opts = {
      method: method,
      headers: { "Content-Type": "application/json", "x-csrf-token": csrf() },
    };
    if (body !== undefined && body !== null) opts.body = JSON.stringify(body);
    return fetch(url, opts).then(function (r) {
      return r.json().then(function (j) {
        return { ok: r.ok, status: r.status, body: j };
      }).catch(function () {
        return { ok: r.ok, status: r.status, body: null };
      });
    });
  }

  // ── Rendering ────────────────────────────────────────────────────────
  function positionBadge(pos, confidence) {
    var label = pos === "yes" ? "YES" : pos === "no" ? "NO" : "Neutral";
    var tone = pos === "yes"
      ? "var(--semantic-high)"
      : pos === "no"
      ? "var(--semantic-low)"
      : "var(--text-secondary)";
    // "conf 8" reads better than "8/10" — nobody's confused about the
    // scale once they've seen the post modal, and it's one fewer token
    // to parse on every row.
    var conf = confidence
      ? ' <span style="color:var(--text-tertiary);font-size:11px">' +
        'conf ' + '<strong style="color:' + tone + '">' + confidence + "</strong></span>"
      : "";
    return (
      '<span style="color:' + tone + ';font-weight:600">' + esc(label) +
      "</span>" + conf
    );
  }

  function resolvedBadge(resolvedCorrect) {
    // role="status" so assistive tech treats the symbol + text as a live
    // announcement rather than loose punctuation. aria-label re-states the
    // glyph-free meaning for screen readers (the "✓" / "✗" glyphs read as
    // "check mark" / "cross mark" which is ambiguous out of context).
    if (resolvedCorrect === 1) {
      return (
        '<span class="take-resolved take-resolved-correct" role="status" ' +
        'aria-label="Correct: this take\'s position matched the market\'s outcome">' +
        '✓ correct</span>'
      );
    }
    if (resolvedCorrect === 0) {
      return (
        '<span class="take-resolved take-resolved-wrong" role="status" ' +
        'aria-label="Incorrect: this take\'s position did not match the outcome">' +
        '✗ incorrect</span>'
      );
    }
    return "";
  }

  function takeRowHTML(t) {
    var ownerChip = t.is_own
      ? '<span style="font-size:11px;color:var(--text-tertiary);margin-left:8px">(your take)</span>'
      : "";
    var editBtn = t.can_edit
      ? ' <button class="btn take-edit" data-id="' + t.id + '">edit</button>'
      : "";
    var reportBtn = !t.is_own && currentUserId
      ? ' <button class="btn take-report" data-id="' + t.id + '">report</button>'
      : "";
    var shadowNote = t.shadow_hidden && t.is_own
      ? '<div class="take-shadow-note" role="note">' +
        "This take is shadow-hidden from other users (low quality score). " +
        "Only you see it." +
        "</div>"
      : "";

    // Vote buttons. Every button has an explicit aria-label and
    // aria-pressed so screen readers announce "Upvote, 23 votes, pressed"
    // (or similar) rather than "up-triangle, 23". Authors see the same
    // counts as a static span — no buttons to tab through.
    var vote = t.viewer_vote;
    var canVote = currentUserId && !t.is_own;
    var upPressed = vote === 1 ? "true" : "false";
    var downPressed = vote === -1 ? "true" : "false";
    var upClass = "btn take-vote take-vote-up" + (vote === 1 ? " is-active" : "");
    var downClass = "btn take-vote take-vote-down" + (vote === -1 ? " is-active" : "");
    var voteBlock = canVote
      ? '<button class="' + upClass + '" data-id="' + t.id +
        '" data-dir="1" aria-pressed="' + upPressed + '" ' +
        'aria-label="Upvote this take (currently ' + t.upvotes + ' upvote' +
        (t.upvotes === 1 ? "" : "s") + ')">▲ <span aria-hidden="true">' +
        t.upvotes + '</span></button>' +
        '<button class="' + downClass + '" data-id="' + t.id +
        '" data-dir="-1" aria-pressed="' + downPressed + '" ' +
        'aria-label="Downvote this take (currently ' + t.downvotes + ' downvote' +
        (t.downvotes === 1 ? "" : "s") + ')">▼ <span aria-hidden="true">' +
        t.downvotes + '</span></button>'
      : '<span class="take-vote-static" aria-label="' +
        t.upvotes + " upvotes, " + t.downvotes + ' downvotes">▲ ' +
        t.upvotes + "  ▼ " + t.downvotes + "</span>";

    return (
      '<article class="take-row" id="take-' + t.id + '">' +
        '<div style="display:flex;justify-content:space-between;align-items:baseline">' +
          '<div>' +
            '<span class="take-author-handle">@' + esc(t.author_handle) + "</span> " +
            '<span class="take-author-cred" ' +
              'title="Blended credibility: global prediction accuracy with a ' +
              'small nudge from take accuracy (0–1, higher is better).">' +
              "cred " + t.author_credibility.toFixed(2) +
            "</span>" +
            ownerChip +
            resolvedBadge(t.resolved_correct) +
          "</div>" +
          '<span style="font-size:11px;color:var(--text-tertiary)">' +
            esc(timeAgo(t.created_at)) +
            (t.edited_at ? " · edited" : "") +
          "</span>" +
        "</div>" +
        '<div style="font-size:13px;margin-top:4px">' +
          positionBadge(t.position, t.confidence) +
        "</div>" +
        '<div class="take-reasoning">' + esc(t.reasoning) + "</div>" +
        shadowNote +
        '<div style="display:flex;gap:8px;margin-top:10px;align-items:center">' +
          voteBlock +
          editBtn + reportBtn +
        "</div>" +
      "</article>"
    );
  }

  function render() {
    var listEl = document.getElementById("takes-list");
    var countEl = document.getElementById("takes-count");
    var postBtn = document.getElementById("takes-post-btn");
    var gateEl = document.getElementById("takes-post-gate");

    countEl.textContent = "(" + state.total + ")";

    if (postBtn) postBtn.style.display = state.canPost ? "" : "none";
    if (gateEl) gateEl.style.display = state.canPost || !currentUserId ? "none" : "";

    // Switch off the loading state for assistive tech on every render.
    listEl.setAttribute("aria-busy", "false");

    if (!state.takes.length) {
      // Tailor the empty-state CTA to the viewer.
      var cta = "";
      if (state.canPost) {
        cta = " Be the first.";
      } else if (currentUserId) {
        // Logged in but not paid — point them at /pricing.
        cta = ' <a href="/pricing" style="color:var(--text-primary)">Upgrade</a> to post one.';
      }
      // Loading → Loaded empty. Keep the text in a live region so screen
      // readers announce the transition.
      listEl.innerHTML =
        '<div class="empty-state" style="padding:24px;text-align:center;' +
        "color:var(--text-tertiary)\">No takes yet." + cta + "</div>";
      return;
    }
    listEl.innerHTML = state.takes.map(takeRowHTML).join("");
  }

  function load() {
    var url = "/api/v1/markets/" + encodeURIComponent(marketSlug) +
      "/takes?sort=" + encodeURIComponent(state.sort);
    if (state.positionFilter) {
      url += "&position=" + encodeURIComponent(state.positionFilter);
    }
    return request("GET", url).then(function (r) {
      if (!r.ok) {
        document.getElementById("takes-list").innerHTML =
          '<div class="empty-state" style="color:var(--text-tertiary)">' +
          "Couldn’t load takes. Try refreshing.</div>";
        return;
      }
      state.takes = r.body.takes || [];
      state.total = r.body.total || 0;
      state.canPost = !!r.body.can_post;
      render();
    });
  }

  // ── Posting modal ────────────────────────────────────────────────────
  function openPostModal(existing) {
    var existingPos = existing && existing.position;
    var existingConf = existing && existing.confidence;
    var existingReasoning = (existing && existing.reasoning) || "";
    var isEdit = !!existing;

    // Modal uses the new .take-modal-* CSS classes for the outer chrome;
    // the body scrolls, the action row is sticky at the bottom so Cancel/
    // Submit stay visible even when reasoning pushes past the viewport.
    var html =
      '<div class="take-modal-overlay">' +
      '<div class="take-modal" role="dialog" aria-modal="true" ' +
           'aria-labelledby="take-modal-title">' +
        '<div class="take-modal-body">' +
          '<h3 id="take-modal-title" style="margin:0 0 8px">' +
            (isEdit ? "Edit" : "Post") + " your take</h3>" +
          '<p style="margin:0 0 16px;color:var(--text-secondary);font-size:13px">' +
            "One take per market. You can edit for 24 hours after posting." +
          "</p>" +
          '<label style="display:block;margin-bottom:6px;font-size:12px;font-weight:600">Position</label>' +
          '<div style="display:flex;gap:6px;margin-bottom:16px" role="radiogroup" aria-label="Position">' +
            ["yes", "no", "neutral"].map(function (p) {
              var active = p === existingPos;
              return (
                '<label style="flex:1;text-align:center;padding:10px;' +
                "border:1px solid var(--border-default);border-radius:6px;cursor:pointer;" +
                (active ? "background:var(--interactive-ghost);" : "") +
                "\">" +
                '<input type="radio" name="take-pos" value="' + p + '" ' +
                (active ? "checked" : "") + ' style="margin-right:6px">' +
                (p === "yes" ? "YES" : p === "no" ? "NO" : "Neutral") +
                "</label>"
              );
            }).join("") +
          "</div>" +
          '<label style="display:block;margin-bottom:6px;font-size:12px;font-weight:600">' +
            "Confidence: <span id=\"take-conf-label\">" +
            (existingConf || 5) + "/10</span></label>" +
          '<input id="take-conf" type="range" min="1" max="10" ' +
            'aria-label="Confidence" aria-valuemin="1" aria-valuemax="10" ' +
            'value="' + (existingConf || 5) + '" style="width:100%;margin-bottom:16px">' +
          '<label for="take-reasoning" ' +
            'style="display:block;margin-bottom:6px;font-size:12px;font-weight:600">' +
            "Reasoning (50–2000 chars)</label>" +
          '<textarea id="take-reasoning" rows="6" ' +
            'aria-describedby="take-count take-error" ' +
            'style="width:100%;padding:10px;border:1px solid var(--border-default);' +
            "border-radius:6px;font-family:inherit;font-size:14px\">" +
            esc(existingReasoning) +
          "</textarea>" +
          '<div style="display:flex;justify-content:space-between;align-items:center;' +
            "margin-top:6px;font-size:11px\">" +
            '<span id="take-count" style="color:var(--text-tertiary)">0 chars</span>' +
            '<span id="take-error" role="alert" aria-live="assertive" ' +
              'style="color:var(--semantic-low)"></span>' +
          "</div>" +
        "</div>" +
        '<div class="take-modal-actions">' +
          '<button class="btn" id="take-cancel">Cancel</button>' +
          '<button class="btn btn-primary" id="take-submit">' +
            (isEdit ? "Save changes" : "Post take") +
          "</button>" +
        "</div>" +
      "</div>" +
      "</div>";

    var host = document.createElement("div");
    host.innerHTML = html;
    var overlay = host.firstChild;
    document.body.appendChild(overlay);

    var confSlider = overlay.querySelector("#take-conf");
    var confLabel = overlay.querySelector("#take-conf-label");
    confSlider.addEventListener("input", function () {
      confLabel.textContent = confSlider.value + "/10";
    });

    var textarea = overlay.querySelector("#take-reasoning");
    var counter = overlay.querySelector("#take-count");
    function updateCount() {
      counter.textContent = textarea.value.length + " chars";
    }
    textarea.addEventListener("input", updateCount);
    updateCount();

    overlay.querySelector("#take-cancel").addEventListener("click", close);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) close();
    });

    overlay.querySelector("#take-submit").addEventListener("click", function () {
      var pos = (overlay.querySelector('input[name="take-pos"]:checked') || {}).value;
      var conf = parseInt(confSlider.value, 10);
      var reasoning = textarea.value.trim();
      var err = overlay.querySelector("#take-error");
      err.textContent = "";

      if (!pos) { err.textContent = "Pick a position."; return; }
      if (reasoning.length < 50) {
        err.textContent = "Reasoning must be ≥ 50 chars.";
        return;
      }
      if (reasoning.length > 2000) {
        err.textContent = "Reasoning must be ≤ 2000 chars.";
        return;
      }

      var body = { position: pos, confidence: conf, reasoning: reasoning };
      var url = isEdit
        ? "/api/v1/takes/" + existing.id
        : "/api/v1/markets/" + encodeURIComponent(marketSlug) + "/takes";
      request(isEdit ? "PATCH" : "POST", url, body).then(function (r) {
        if (!r.ok) {
          err.textContent = (r.body && r.body.detail) ||
            "Something went wrong. Try again.";
          return;
        }
        close();
        load();
      });
    });

    function close() {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }
  }

  // ── Report modal ─────────────────────────────────────────────────────
  var REPORT_REASONS = [
    { v: "spam", l: "Spam or promotional" },
    { v: "harassment", l: "Harassment or personal attack" },
    { v: "misinformation", l: "Knowingly false claim" },
    { v: "off_topic", l: "Off-topic for this market" },
    { v: "other", l: "Other" },
  ];

  function openReportModal(takeId) {
    var html =
      '<div class="take-modal-overlay">' +
      '<div class="take-modal" role="dialog" aria-modal="true" ' +
           'aria-labelledby="report-modal-title" ' +
           'style="max-width:440px">' +
        '<div class="take-modal-body">' +
          '<h3 id="report-modal-title" style="margin:0 0 8px">Report this take</h3>' +
          '<p style="font-size:13px;color:var(--text-secondary);margin:0 0 14px">' +
            "We review every report. Abuse of the report button may result in a warning." +
          "</p>" +
          '<label for="report-reason" ' +
            'style="display:block;margin-bottom:6px;font-size:12px;font-weight:600">' +
            "Reason</label>" +
          '<select id="report-reason" style="width:100%;padding:10px;' +
            "border:1px solid var(--border-default);border-radius:6px;margin-bottom:12px\">" +
            REPORT_REASONS.map(function (r) {
              return '<option value="' + r.v + '">' + esc(r.l) + "</option>";
            }).join("") +
          "</select>" +
          '<label for="report-details" ' +
            'style="display:block;margin-bottom:6px;font-size:12px;font-weight:600">' +
            "Details (optional)</label>" +
          '<textarea id="report-details" rows="3" ' +
            'style="width:100%;padding:10px;border:1px solid var(--border-default);' +
            "border-radius:6px;font-family:inherit;font-size:13px\"></textarea>" +
          '<div id="report-msg" style="margin-top:10px;font-size:12px" ' +
            'role="status" aria-live="polite"></div>' +
        "</div>" +
        '<div class="take-modal-actions">' +
          '<button class="btn" id="report-cancel">Cancel</button>' +
          '<button class="btn btn-primary" id="report-submit">Send report</button>' +
        "</div>" +
      "</div>" +
      "</div>";
    var host = document.createElement("div");
    host.innerHTML = html;
    var overlay = host.firstChild;
    document.body.appendChild(overlay);

    function close() { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }
    overlay.querySelector("#report-cancel").addEventListener("click", close);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) close();
    });

    overlay.querySelector("#report-submit").addEventListener("click", function () {
      var reason = overlay.querySelector("#report-reason").value;
      var details = overlay.querySelector("#report-details").value.trim() || null;
      var msg = overlay.querySelector("#report-msg");
      msg.textContent = "Sending…";
      request("POST", "/api/v1/takes/" + takeId + "/report",
              { reason: reason, details: details }).then(function (r) {
        if (r.ok) {
          msg.style.color = "var(--semantic-high)";
          msg.textContent = "Reported. Thanks — a moderator will review.";
          setTimeout(close, 1200);
        } else {
          msg.style.color = "var(--semantic-low)";
          msg.textContent = (r.body && r.body.detail) || "Couldn’t send. Try again.";
        }
      });
    });
  }

  // ── Event delegation ─────────────────────────────────────────────────
  root.addEventListener("click", function (e) {
    var t = e.target;
    if (!(t instanceof HTMLElement)) return;

    if (t.id === "takes-post-btn") {
      openPostModal(null);
      return;
    }
    if (t.classList.contains("takes-sort-btn")) {
      var s = t.getAttribute("data-sort");
      if (s) { state.sort = s; load(); }
      root.querySelectorAll(".takes-sort-btn").forEach(function (b) {
        b.classList.toggle("active", b === t);
      });
      return;
    }
    if (t.classList.contains("takes-filter-btn")) {
      var f = t.getAttribute("data-filter");
      state.positionFilter = f === "all" ? null : f;
      load();
      root.querySelectorAll(".takes-filter-btn").forEach(function (b) {
        b.classList.toggle("active", b === t);
      });
      return;
    }
    if (t.classList.contains("take-vote")) {
      var id = parseInt(t.getAttribute("data-id"), 10);
      var dir = parseInt(t.getAttribute("data-dir"), 10);
      var existing = state.takes.find(function (x) { return x.id === id; });
      var already = existing && existing.viewer_vote === dir;
      var body = { vote: already ? 0 : dir };
      request("POST", "/api/v1/takes/" + id + "/vote", body).then(function (r) {
        if (r.ok) load();
      });
      return;
    }
    if (t.classList.contains("take-edit")) {
      var eid = parseInt(t.getAttribute("data-id"), 10);
      var take = state.takes.find(function (x) { return x.id === eid; });
      if (take) openPostModal(take);
      return;
    }
    if (t.classList.contains("take-report")) {
      var rid = parseInt(t.getAttribute("data-id"), 10);
      openReportModal(rid);
      return;
    }
  });

  // ── Boot ─────────────────────────────────────────────────────────────
  load();
})();
