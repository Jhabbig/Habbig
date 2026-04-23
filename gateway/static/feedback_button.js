/* feedback_button.js — floating "💬 Feedback" FAB + submission modal.
 *
 * Auto-mounts on DOMContentLoaded. Exposes `window.__narveFeedback.open()`
 * for pages that want to open the modal from a non-FAB trigger (like the
 * "Submit feedback +" button on /feedback).
 *
 * Suppressed on auth screens, the admin area, and on the feedback pages
 * themselves — the modal would be redundant in those contexts.
 *
 * Submission goes to POST /api/feedback with an XHR + JSON Accept header
 * so the server returns JSON (so we can show inline success without a
 * navigation away). Form fields:
 *   type      — bug | feature | question  (default: feature)
 *   title     — required
 *   body      — required
 *   is_public — "1" | "0"  (default: 1)
 *
 * Everything is vanilla JS + plain DOM — no bundler, no framework. The
 * styles are in-line so we don't fight whichever theme/stylesheet the
 * host page happens to ship.
 */
(function () {
  "use strict";

  if (window.__narveFeedbackLoaded) return;
  window.__narveFeedbackLoaded = true;

  // Pages where the FAB would be noise or redundant.
  var SUPPRESS_PATHS = [
    "/token",
    "/login",
    "/logout",
    "/admin",
    "/feedback",             // has its own submit button in the page
  ];

  function shouldSuppress() {
    var p = (window.location.pathname || "").toLowerCase();
    for (var i = 0; i < SUPPRESS_PATHS.length; i++) {
      if (p === SUPPRESS_PATHS[i] || p.indexOf(SUPPRESS_PATHS[i] + "/") === 0) {
        return true;
      }
    }
    return false;
  }

  function getCsrf() {
    var m = document.cookie.match(/(?:^|; )_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      if (k === "style") node.setAttribute("style", attrs[k]);
      else if (k.indexOf("on") === 0 && typeof attrs[k] === "function") {
        node.addEventListener(k.slice(2), attrs[k]);
      } else if (k === "html") {
        node.innerHTML = attrs[k];
      } else {
        node.setAttribute(k, attrs[k]);
      }
    });
    (children || []).forEach(function (c) {
      if (typeof c === "string") node.appendChild(document.createTextNode(c));
      else if (c) node.appendChild(c);
    });
    return node;
  }

  var modalEl = null;
  var state = { open: false };

  function closeModal() {
    if (modalEl && modalEl.parentNode) modalEl.parentNode.removeChild(modalEl);
    modalEl = null;
    state.open = false;
    document.removeEventListener("keydown", onEsc);
  }

  function onEsc(e) { if (e.key === "Escape") closeModal(); }

  function showResult(container, msg, ok) {
    var color = ok ? "#10b981" : "#ef4444";
    container.innerHTML = "";
    container.appendChild(
      el("div", {
        style:
          "padding:16px;border-radius:8px;background:" +
          (ok ? "rgba(16,185,129,0.08)" : "rgba(239,68,68,0.08)") +
          ";border:1px solid " + color + ";color:" + color +
          ";font-size:13px;line-height:1.5;text-align:center",
      }, [msg])
    );
  }

  function submit(form, resultBox) {
    var fd = new FormData(form);
    var body = new URLSearchParams();
    fd.forEach(function (v, k) { body.append(k, v); });
    // Defaults — make sure we always have a type, and is_public
    // follows the checkbox's actual state (unchecked = "0").
    if (!body.get("type")) body.set("type", "feature");
    body.set("is_public", form.elements["is_public"] && form.elements["is_public"].checked ? "1" : "0");
    var csrf = getCsrf();
    if (csrf) body.set("_csrf", csrf);

    fetch("/api/feedback", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "X-CSRF-Token": csrf,
      },
      body: body.toString(),
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t || "submit_failed"); });
      return r.json();
    }).then(function (j) {
      var msg = j.is_public
        ? "Thanks — your post is on the public roadmap. Redirecting…"
        : "Sent privately. We'll follow up through your notifications.";
      showResult(resultBox, msg, true);
      setTimeout(function () {
        closeModal();
        if (j.is_public && j.id) {
          window.location.href = "/feedback/" + j.id;
        }
      }, 1400);
    }).catch(function () {
      showResult(resultBox, "Couldn't submit. Try again in a moment.", false);
    });
  }

  function openModal(preselectType) {
    if (state.open) return;
    state.open = true;

    var typeButtons = ["bug", "feature", "question"].map(function (t) {
      var label = t === "bug" ? "Bug" : t === "feature" ? "Feature idea" : "Question";
      var active = (preselectType || "feature") === t;
      var btn = el("label", {
        class: "nf-type-btn",
        style:
          "flex:1;padding:10px 12px;border-radius:6px;cursor:pointer;text-align:center;" +
          "font-size:13px;font-weight:600;border:1px solid " + (active ? "var(--text-primary)" : "var(--border)") +
          ";background:" + (active ? "var(--interactive-ghost)" : "transparent") +
          ";color:var(--text-primary);transition:all 0.12s",
      }, [
        el("input", {
          type: "radio", name: "type", value: t,
          checked: active ? "checked" : null,
          style: "position:absolute;opacity:0;pointer-events:none",
        }),
        label,
      ]);
      return btn;
    });
    // Highlight handler: keep the selected label highlighted on change.
    function wireTypeHighlight(wrap) {
      wrap.addEventListener("change", function () {
        Array.prototype.forEach.call(wrap.querySelectorAll(".nf-type-btn"), function (lbl) {
          var inp = lbl.querySelector("input[type=radio]");
          var active = inp && inp.checked;
          lbl.style.borderColor = active ? "var(--text-primary)" : "var(--border)";
          lbl.style.background = active ? "var(--interactive-ghost)" : "transparent";
        });
      });
    }

    var resultBox = el("div", { style: "margin-top:12px" });

    // ENHANCEMENT #5 — similar-items hint container. Populated on
    // title-input blur by a GET /api/feedback/search call; cleared
    // when the search returns no matches or the input is too short.
    var similarBox = el("div", {
      id: "nf-similar",
      style: "margin-bottom:14px;display:none",
    });

    var typesWrap = el("div", {
      style: "display:flex;gap:8px;margin-bottom:14px",
    }, typeButtons);
    wireTypeHighlight(typesWrap);

    var titleInput = el("input", {
      type: "text", name: "title", maxlength: "200", required: "required",
      placeholder: "What's the gist?",
      style:
        "width:100%;padding:9px 12px;font:inherit;font-size:13px;background:var(--bg-base);" +
        "border:1px solid var(--border);border-radius:6px;color:var(--text-primary);" +
        "margin-bottom:14px;box-sizing:border-box",
    });

    // Similar-items lookup: debounce on input (500ms), run on blur too.
    var similarTimer = null;
    function refreshSimilar() {
      var q = (titleInput.value || "").trim();
      if (q.length < 3) {
        similarBox.style.display = "none";
        similarBox.innerHTML = "";
        return;
      }
      fetch("/api/feedback/search?q=" + encodeURIComponent(q), {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
      }).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          var items = (j && j.items) || [];
          if (!items.length) {
            similarBox.style.display = "none";
            similarBox.innerHTML = "";
            return;
          }
          similarBox.innerHTML = "";
          similarBox.style.display = "block";
          var heading = el("div", {
            style: "font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px",
          }, ["Possibly similar — upvote instead of re-posting?"]);
          similarBox.appendChild(heading);
          items.forEach(function (it) {
            var row = el("a", {
              href: "/feedback/" + it.id,
              target: "_blank",
              rel: "noopener",
              style:
                "display:flex;gap:10px;align-items:center;padding:6px 10px;margin-bottom:4px;" +
                "background:var(--bg-base);border:1px solid var(--border);border-radius:6px;" +
                "font-size:12px;color:var(--text-primary);text-decoration:none",
            }, [
              el("span", { style: "flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" },
                 [it.title || ("#" + it.id)]),
              el("span", { style: "font-size:10px;color:var(--text-muted);text-transform:uppercase" },
                 [(it.status || "open").replace("_", " ")]),
              el("span", { style: "font-variant-numeric:tabular-nums;font-weight:600" },
                 [String(it.upvotes || 0), " ↑"]),
            ]);
            similarBox.appendChild(row);
          });
        }).catch(function () {});
    }
    titleInput.addEventListener("input", function () {
      if (similarTimer) clearTimeout(similarTimer);
      similarTimer = setTimeout(refreshSimilar, 500);
    });
    titleInput.addEventListener("blur", refreshSimilar);

    var form = el("form", { style: "display:block" }, [
      typesWrap,
      el("label", {
        style: "display:block;font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px",
      }, ["Title"]),
      titleInput,
      similarBox,
      el("label", {
        style: "display:block;font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px",
      }, ["Details"]),
      el("textarea", {
        name: "body", maxlength: "4000", required: "required",
        placeholder: "What happened / what did you want / steps to reproduce…",
        style:
          "width:100%;padding:9px 12px;font:inherit;font-size:13px;background:var(--bg-base);" +
          "border:1px solid var(--border);border-radius:6px;color:var(--text-primary);" +
          "min-height:120px;resize:vertical;box-sizing:border-box;margin-bottom:14px",
      }),
      el("label", {
        style: "display:flex;gap:8px;align-items:center;font-size:12px;color:var(--text-secondary);cursor:pointer;margin-bottom:14px",
      }, [
        el("input", { type: "checkbox", name: "is_public", value: "1", checked: "checked" }),
        el("span", {}, ["Post publicly on /feedback (uncheck to send privately to admins)"]),
      ]),
      el("div", {
        style: "display:flex;gap:8px;justify-content:flex-end",
      }, [
        el("button", {
          type: "button",
          style:
            "padding:8px 14px;background:transparent;border:1px solid var(--border);" +
            "border-radius:6px;font-size:12px;font-weight:600;color:var(--text-muted);cursor:pointer",
          onclick: closeModal,
        }, ["Cancel"]),
        el("button", {
          type: "submit",
          style:
            "padding:8px 16px;background:var(--cta-bg,#111);color:var(--interactive-text,#fff);" +
            "border:0;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer",
        }, ["Submit"]),
      ]),
      resultBox,
    ]);

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      submit(form, resultBox);
    });

    var card = el("div", {
      style:
        "background:var(--bg-raised,#141414);border:1px solid var(--border,rgba(255,255,255,0.06));" +
        "border-radius:12px;padding:24px;width:min(520px,92vw);max-height:92vh;overflow-y:auto;" +
        "box-shadow:0 20px 50px rgba(0,0,0,0.4)",
      role: "dialog", "aria-modal": "true", "aria-labelledby": "nf-title",
    }, [
      el("h2", {
        id: "nf-title",
        style: "margin:0 0 4px;font-family:var(--font-display,inherit);font-size:18px;font-weight:700;letter-spacing:-0.01em",
      }, ["Tell us something"]),
      el("p", {
        style: "margin:0 0 18px;font-size:12px;color:var(--text-muted)",
      }, ["Bug, feature idea, or question — we read everything."]),
      form,
    ]);

    var backdrop = el("div", {
      id: "narve-feedback-modal",
      style:
        "position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:10000;" +
        "display:flex;align-items:center;justify-content:center;padding:24px",
      onclick: function (e) { if (e.target === backdrop) closeModal(); },
    }, [card]);

    document.body.appendChild(backdrop);
    modalEl = backdrop;
    document.addEventListener("keydown", onEsc);
    // Focus the title input after the modal mounts.
    var titleInput = card.querySelector('input[name="title"]');
    if (titleInput) setTimeout(function () { titleInput.focus(); }, 30);
  }

  function mountFab() {
    if (document.getElementById("narve-feedback-fab")) return;
    var fab = el("button", {
      id: "narve-feedback-fab",
      type: "button",
      "aria-label": "Submit feedback",
      title: "Submit feedback",
      style:
        "position:fixed;right:20px;bottom:20px;z-index:9000;" +
        "padding:10px 16px;border-radius:24px;border:1px solid var(--border,rgba(0,0,0,0.1));" +
        "background:var(--bg-raised,#fff);color:var(--text-primary,#111);" +
        "font-size:13px;font-weight:600;cursor:pointer;" +
        "box-shadow:0 4px 14px rgba(0,0,0,0.18);display:inline-flex;align-items:center;gap:6px",
      onclick: function () { openModal(); },
    }, ["💬 Feedback"]);
    document.body.appendChild(fab);
  }

  window.__narveFeedback = { open: openModal, close: closeModal };

  function boot() {
    if (shouldSuppress()) return;
    mountFab();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
