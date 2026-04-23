/*
 * Share-button module — mints a signed share URL via POST /api/share/{kind}
 * and copies it to the clipboard. Single file used by market_detail,
 * source, and prediction_detail pages; the DOM hook is:
 *
 *   <button class="narve-share-btn"
 *           data-share-kind="market|source|prediction"
 *           data-share-slug="…"   (for kind=market, the slug)
 *           data-share-handle="…" (for kind=source, the handle)
 *           data-share-pid="…"    (for kind=prediction, the user_prediction_id)
 *   >Share</button>
 *
 * Each page embeds at most one button. The module auto-wires every
 * matching button on DOMContentLoaded — no manual init needed.
 *
 * UX flow:
 *   1. Click → button shows "minting…", POST to /api/share/{kind}
 *   2. On 200: copy the full narve.ai/s/{kind}/{token} URL to clipboard,
 *      swap to "copied ✓" for 1.5s, then restore.
 *   3. On 402: redirect to /subscribe (paid-tier required).
 *   4. On 429: show "wait an hour" inline for 3s, then restore.
 *   5. On any other error: show "try again" inline for 3s.
 *
 * Forensic-copy note:
 *   The clipboard value here is a server-minted token URL, not
 *   watermarked page content. The site-wide anti-copy system
 *   (see gateway/watermark.py) intentionally does NOT intercept this
 *   because the user is explicitly exfiltrating a URL they just
 *   authorised the server to mint on their behalf.
 */
(function () {
  "use strict";

  var ENDPOINT_BY_KIND = {
    market: "/api/share/market",
    source: "/api/share/source",
    prediction: "/api/share/prediction",
  };

  function payloadFor(btn, kind) {
    // Each kind has a different identifier field. The server-side
    // endpoints 400 on a missing one, but we guard here too so the
    // UX error is instant (no round-trip).
    if (kind === "market") {
      var slug = btn.dataset.shareSlug || "";
      return slug ? { market_slug: slug } : null;
    }
    if (kind === "source") {
      var handle = btn.dataset.shareHandle || "";
      return handle ? { source_handle: handle } : null;
    }
    if (kind === "prediction") {
      var pid = parseInt(btn.dataset.sharePid || "0", 10);
      return pid > 0 ? { user_prediction_id: pid } : null;
    }
    return null;
  }

  function csrfToken() {
    // Double-submit cookie CSRF: the middleware expects the value of
    // the `_csrf` cookie to be echoed on the `x-csrf-token` header.
    // The gateway sets this cookie on every authenticated page GET,
    // so by the time a share button renders the cookie exists.
    var m = document.cookie.match(/(?:^|;\s*)_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function copyToClipboard(text) {
    // Prefer the modern API; fall back to a hidden textarea for
    // browsers in non-secure contexts or with restrictive permissions.
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "-1000px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        resolve();
      } catch (e) {
        reject(e);
      }
    });
  }

  function setLabel(btn, text, cls) {
    btn.textContent = text;
    btn.classList.remove("share-btn-copied", "share-btn-error", "share-btn-loading");
    if (cls) btn.classList.add(cls);
  }

  function restore(btn, originalLabel, delayMs) {
    setTimeout(function () {
      setLabel(btn, originalLabel, null);
      btn.disabled = false;
    }, delayMs);
  }

  function fullShareUrl(pathPart) {
    // Server returns a relative path (/s/m/{token}). Stitch the origin
    // on client-side so the clipboard gets the absolute URL a recipient
    // can paste anywhere.
    return window.location.origin + pathPart;
  }

  async function handleClick(btn) {
    var kind = btn.dataset.shareKind;
    var endpoint = ENDPOINT_BY_KIND[kind];
    if (!endpoint) return;

    var body = payloadFor(btn, kind);
    if (!body) {
      setLabel(btn, "missing info", "share-btn-error");
      restore(btn, "Share", 3000);
      return;
    }

    var originalLabel = btn.dataset.originalLabel || btn.textContent || "Share";
    btn.dataset.originalLabel = originalLabel;
    btn.disabled = true;
    setLabel(btn, "minting…", "share-btn-loading");

    var r;
    try {
      r = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "content-type": "application/json",
          "x-csrf-token": csrfToken(),
        },
        body: JSON.stringify(body),
      });
    } catch (_netErr) {
      setLabel(btn, "offline — retry", "share-btn-error");
      restore(btn, originalLabel, 3000);
      return;
    }

    if (r.status === 402) {
      // Paid-tier required. Route to billing — same pattern as the
      // rest of the paywall-gated surface.
      window.location.href = "/subscribe";
      return;
    }
    if (r.status === 429) {
      setLabel(btn, "slow down — try in 1h", "share-btn-error");
      restore(btn, originalLabel, 3000);
      return;
    }
    if (!r.ok) {
      setLabel(btn, "error — try again", "share-btn-error");
      restore(btn, originalLabel, 3000);
      return;
    }

    var data = null;
    try {
      data = await r.json();
    } catch (_parseErr) {
      setLabel(btn, "error — try again", "share-btn-error");
      restore(btn, originalLabel, 3000);
      return;
    }
    if (!data || !data.ok || !data.share_url) {
      setLabel(btn, "error — try again", "share-btn-error");
      restore(btn, originalLabel, 3000);
      return;
    }

    try {
      await copyToClipboard(fullShareUrl(data.share_url));
      setLabel(btn, "copied ✓", "share-btn-copied");
    } catch (_copyErr) {
      // If clipboard write failed, show the URL inline so the user
      // can still manually copy it.
      setLabel(btn, data.share_url, "share-btn-copied");
    }
    restore(btn, originalLabel, 1500);
  }

  function wireAll() {
    document.querySelectorAll(".narve-share-btn").forEach(function (btn) {
      if (btn.dataset.shareWired === "1") return;
      btn.dataset.shareWired = "1";
      btn.addEventListener("click", function () { handleClick(btn); });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireAll);
  } else {
    wireAll();
  }

  // Re-scan on demand for pages that render buttons after the
  // initial load (e.g. prediction_detail.html swaps the page body
  // in after fetch). Callers can dispatch this event to retry.
  document.addEventListener("narve:rescan-share-buttons", wireAll);
})();
