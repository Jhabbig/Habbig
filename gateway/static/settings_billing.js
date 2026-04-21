/* settings_billing.js — client UI for /settings/billing.
 *
 * Responsibilities:
 *   1. Monthly / Annual toggle — swap displayed prices and rebuild cards' hidden amounts.
 *   2. Upgrade / Downgrade modal — compute and display proration using the same
 *      formula as Stripe, so users get an exact preview before confirming.
 *   3. Cancel modal open/close.
 *   4. Billing history fetch from /api/billing/invoices (paginated).
 *
 * No Stripe SDK calls happen here — the POSTs land on stubbed server routes.
 */
(function () {
  "use strict";

  // ── Grab server-provided data payload ─────────────────────────────────────
  var dataEl = document.getElementById("sb-data");
  var DATA = {};
  try {
    DATA = JSON.parse(dataEl ? dataEl.textContent : "{}");
  } catch (e) {
    DATA = {};
  }

  var nowSec = Math.floor(Date.now() / 1000);
  var currentPlan = DATA.current_plan || null; // {key, interval, amount_usd, started_at, expires_at}
  var catalog = DATA.catalog || {};
  var addonCurrent = DATA.addon || { active: false, amount_usd: 29 };
  var CURRENCY = "$";

  // ── Proration calculator (client-side, matches Stripe's formula) ──────────
  //
  //   1. daily_rate_old  = old_plan.amount / days_in_period
  //   2. unused_days     = floor((period_end - now) / 86400)
  //   3. credit          = daily_rate_old * unused_days
  //   4. daily_rate_new  = new_plan.amount / new_period_days
  //   5. new_cost        = daily_rate_new * unused_days
  //   6. net             = new_cost - credit
  //
  // If the user has no current plan, the "credit" is 0 and the charge is the
  // full new-plan amount. If they're downgrading and credit > new_cost, the
  // result is a credit (negative net).
  function calculateProration(current, next, periodEnd, now) {
    var NO_CURRENT = !current || !current.amount_usd;
    var nextAmount = Number(next.amount_usd) || 0;
    var nextDays = next.interval === "annual" ? 365 : 30;

    if (NO_CURRENT) {
      return {
        credit: 0,
        charge: nextAmount,
        net: nextAmount,
        unusedDays: nextDays,
        totalDays: nextDays,
        dailyRateNew: nextAmount / nextDays,
        kind: "new",
      };
    }

    var curDays = current.interval === "annual" ? 365 : 30;
    var curAmount = Number(current.amount_usd) || 0;
    var dailyOld = curDays > 0 ? curAmount / curDays : 0;
    var unusedDays = Math.max(0, Math.floor((periodEnd - now) / 86400));
    var credit = dailyOld * unusedDays;

    var dailyNew = nextDays > 0 ? nextAmount / nextDays : 0;
    var newCost = dailyNew * unusedDays;

    var net = newCost - credit;
    var kind = "same";
    if (next.key !== current.key) {
      kind = nextAmount > curAmount ? "upgrade" : "downgrade";
    } else if (next.interval !== current.interval) {
      kind = nextDays > curDays ? "interval_up" : "interval_down";
    }

    return {
      credit: round2(credit),
      charge: round2(newCost),
      net: round2(net),
      unusedDays: unusedDays,
      totalDays: curDays,
      dailyRateNew: dailyNew,
      kind: kind,
    };
  }

  function round2(n) {
    return Math.round((Number(n) || 0) * 100) / 100;
  }

  function fmtMoney(n) {
    var sign = n < 0 ? "-" : "";
    var v = Math.abs(n);
    return sign + CURRENCY + v.toFixed(2);
  }

  // Exposed for unit testing from the browser console and by the test suite.
  window.narveProration = {
    calculate: calculateProration,
    fmtMoney: fmtMoney,
  };

  // ── Interval toggle ───────────────────────────────────────────────────────
  var toggleBtns = document.querySelectorAll(".sb-toggle-btn");
  var planCards = document.querySelectorAll("[data-plan-card]");

  function setInterval_(interval) {
    toggleBtns.forEach(function (b) {
      b.classList.toggle("active", b.dataset.interval === interval);
    });
    planCards.forEach(function (card) {
      var key = card.dataset.planCard;
      var priceEl = card.querySelector("[data-price]");
      var periodEl = card.querySelector("[data-period]");
      var intervalInput = card.querySelector("[data-interval-input]");
      if (!catalog[key]) return;
      var amt = interval === "annual" ? catalog[key].annual_usd : catalog[key].monthly_usd;
      var period = interval === "annual" ? "/yr" : "/mo";
      if (priceEl) priceEl.textContent = CURRENCY + amt.toLocaleString();
      if (periodEl) periodEl.textContent = period;
      if (intervalInput) intervalInput.value = interval;
      card.dataset.currentInterval = interval;
    });
  }

  toggleBtns.forEach(function (b) {
    b.addEventListener("click", function () {
      setInterval_(b.dataset.interval);
    });
  });

  // ── Modal helpers ─────────────────────────────────────────────────────────
  function openModal(id) {
    var m = document.getElementById(id);
    if (m) m.classList.add("open");
  }
  function closeModal(id) {
    var m = document.getElementById(id);
    if (m) m.classList.remove("open");
  }
  // Close buttons — any element with data-close-modal closes its enclosing modal.
  document.addEventListener("click", function (ev) {
    if (ev.target && ev.target.matches("[data-close-modal]")) {
      var m = ev.target.closest(".sb-modal-backdrop");
      if (m) m.classList.remove("open");
    }
  });
  // Click on backdrop closes.
  document.querySelectorAll(".sb-modal-backdrop").forEach(function (bd) {
    bd.addEventListener("click", function (ev) {
      if (ev.target === bd) bd.classList.remove("open");
    });
  });
  // Esc closes all modals.
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      document.querySelectorAll(".sb-modal-backdrop.open").forEach(function (bd) {
        bd.classList.remove("open");
      });
    }
  });

  // ── Feature maps (gains/losses) per plan ──────────────────────────────────
  var PLAN_FEATURES = {
    trader: [
      "3 dashboard credits",
      "30-day data window",
      "Basic credibility scores",
      "Standard support",
    ],
    pro: [
      "Unlimited dashboards",
      "6-month data window",
      "Per-category credibility",
      "Signal Search",
      "Push notifications",
    ],
    enterprise: [
      "Everything in Pro",
      "Intelligence Add-on",
      "Dedicated Slack channel",
      "Custom SLA",
      "API access",
    ],
  };

  function featuresGainedLost(fromKey, toKey) {
    var from = new Set(PLAN_FEATURES[fromKey] || []);
    var to = new Set(PLAN_FEATURES[toKey] || []);
    var gained = [];
    var lost = [];
    to.forEach(function (f) { if (!from.has(f)) gained.push(f); });
    from.forEach(function (f) { if (!to.has(f)) lost.push(f); });
    return { gained: gained, lost: lost };
  }

  // ── Change-plan flow ──────────────────────────────────────────────────────
  // Any element with data-change-plan="<key>" opens the modal for that plan.
  document.querySelectorAll("[data-change-plan]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var targetKey = btn.dataset.changePlan;
      if (targetKey === "enterprise") {
        window.location.href = "/enquire";
        return;
      }
      var targetInterval = btn.dataset.interval || (
        document.querySelector(".sb-toggle-btn.active")?.dataset.interval || "monthly"
      );
      openChangeModal(targetKey, targetInterval);
    });
  });

  function openChangeModal(targetKey, targetInterval) {
    var tPlan = catalog[targetKey];
    if (!tPlan) return;
    var amount = targetInterval === "annual" ? tPlan.annual_usd : tPlan.monthly_usd;
    var next = {
      key: targetKey,
      interval: targetInterval,
      amount_usd: amount,
    };

    var pr = calculateProration(
      currentPlan,
      next,
      (currentPlan && currentPlan.expires_at) || 0,
      nowSec
    );

    var titleEl = document.getElementById("sb-change-title");
    var descEl = document.getElementById("sb-change-desc");
    var boxTitle = document.getElementById("sb-change-box-title");
    var boxAmount = document.getElementById("sb-change-box-amount");
    var boxDetail = document.getElementById("sb-change-box-detail");
    var gainsEl = document.getElementById("sb-change-gains");
    var lossesEl = document.getElementById("sb-change-losses");
    var confirmBtn = document.getElementById("sb-change-confirm");

    // Fill hidden form inputs so submit posts to /billing/subscribe
    document.getElementById("sb-change-plan").value = targetKey;
    document.getElementById("sb-change-interval").value = targetInterval;

    var kind = pr.kind;
    var curLabel = currentPlan ? (currentPlan.label || currentPlan.key) : "no plan";
    var tLabel = tPlan.label || targetKey;
    var intLabel = targetInterval === "annual" ? "Annual" : "Monthly";

    if (kind === "upgrade") {
      titleEl.textContent = "Upgrade to " + tLabel + "?";
      descEl.textContent = "Switching from " + curLabel + " to " + tLabel + " (" + intLabel + ").";
      boxTitle.textContent = "Prorated charge today";
      boxAmount.textContent = fmtMoney(Math.max(0, pr.net));
      boxDetail.textContent = pr.unusedDays + " days remaining at new tier, then " + fmtMoney(amount) + "/" + (targetInterval === "annual" ? "yr" : "mo");
      confirmBtn.textContent = "Confirm upgrade";
      confirmBtn.classList.remove("sb-btn-danger");
      confirmBtn.classList.add("sb-btn-primary");
    } else if (kind === "downgrade") {
      titleEl.textContent = "Downgrade to " + tLabel + "?";
      descEl.textContent = "Switching from " + curLabel + " to " + tLabel + " (" + intLabel + ").";
      boxTitle.textContent = "Credit applied to future invoices";
      boxAmount.textContent = fmtMoney(Math.max(0, -pr.net));
      boxDetail.textContent = pr.unusedDays + " days remaining — you keep " + curLabel + " access until then.";
      confirmBtn.textContent = "Confirm downgrade";
      confirmBtn.classList.remove("sb-btn-primary");
      confirmBtn.classList.add("sb-btn-danger");
    } else if (kind === "interval_up" || kind === "interval_down") {
      titleEl.textContent = "Switch to " + intLabel + " billing?";
      descEl.textContent = "Keeping " + tLabel + " but changing the billing interval.";
      boxTitle.textContent = pr.net >= 0 ? "Prorated charge today" : "Credit applied";
      boxAmount.textContent = fmtMoney(Math.abs(pr.net));
      boxDetail.textContent = pr.unusedDays + " days remaining at the new rate.";
      confirmBtn.textContent = "Confirm switch";
      confirmBtn.classList.remove("sb-btn-danger");
      confirmBtn.classList.add("sb-btn-primary");
    } else if (kind === "new") {
      titleEl.textContent = "Subscribe to " + tLabel + "?";
      descEl.textContent = "Starting a new " + intLabel.toLowerCase() + " subscription.";
      boxTitle.textContent = "Charged today";
      boxAmount.textContent = fmtMoney(pr.charge);
      boxDetail.textContent = "Renews " + (targetInterval === "annual" ? "annually" : "monthly") + " after.";
      confirmBtn.textContent = "Subscribe";
      confirmBtn.classList.remove("sb-btn-danger");
      confirmBtn.classList.add("sb-btn-primary");
    } else {
      // same plan + same interval — nothing to do
      titleEl.textContent = "No change";
      descEl.textContent = "You're already on this plan and interval.";
      boxTitle.textContent = "Amount";
      boxAmount.textContent = fmtMoney(0);
      boxDetail.textContent = "";
      confirmBtn.textContent = "Close";
    }

    // Features gained / lost — only show for real tier changes.
    var fromKey = currentPlan && currentPlan.key ? currentPlan.key : null;
    gainsEl.innerHTML = "";
    lossesEl.innerHTML = "";
    if (fromKey && fromKey !== targetKey) {
      var diff = featuresGainedLost(fromKey, targetKey);
      if (diff.gained.length) {
        gainsEl.innerHTML =
          '<div class="sb-modal-box-title" style="margin-top:10px">You gain</div>' +
          '<ul class="sb-modal-list">' +
          diff.gained.map(function (f) { return "<li>" + escapeHtml(f) + "</li>"; }).join("") +
          "</ul>";
      }
      if (diff.lost.length) {
        lossesEl.innerHTML =
          '<div class="sb-modal-box-title" style="margin-top:10px;color:var(--red)">You lose</div>' +
          '<ul class="sb-modal-list">' +
          diff.lost.map(function (f) { return "<li>" + escapeHtml(f) + "</li>"; }).join("") +
          "</ul>";
      }
    }

    openModal("sb-change-modal");
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ── Cancel flow ───────────────────────────────────────────────────────────
  var cancelBtn = document.querySelector("[data-open-cancel]");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", function () {
      var endEl = document.getElementById("sb-cancel-end");
      if (endEl && DATA.renewal_str) endEl.textContent = DATA.renewal_str;
      openModal("sb-cancel-modal");
    });
  }

  // ── Billing history ───────────────────────────────────────────────────────
  var historyState = { cursor: 0, done: false };
  var historyTable = document.getElementById("sb-history-table");
  var historyTbody = document.getElementById("sb-history-tbody");
  var historyEmpty = document.getElementById("sb-history-empty");
  var historyMoreWrap = document.getElementById("sb-history-more-wrap");
  var historyMoreBtn = document.getElementById("sb-history-more");

  function formatDate(ts) {
    try {
      var d = new Date(ts * 1000);
      return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
    } catch (e) {
      return "—";
    }
  }

  function renderHistoryRows(invoices) {
    if (!invoices || !invoices.length) return;
    if (historyEmpty) historyEmpty.style.display = "none";
    if (historyTable) historyTable.style.display = "table";

    invoices.forEach(function (inv) {
      var tr = document.createElement("tr");
      var statusClass = inv.status === "paid" ? "sb-history-status-paid" : "";
      var pdfCell = inv.pdf_url
        ? '<a href="' + escapeHtml(inv.pdf_url) + '" class="sb-btn sb-btn-ghost sb-btn-sm" title="Download PDF">↓ PDF</a>'
        : '<span style="color:var(--text-muted);font-size:11px">Unavailable</span>';
      tr.innerHTML =
        "<td>" + escapeHtml(formatDate(inv.date)) + "</td>" +
        "<td>" + escapeHtml(inv.description || "") + "</td>" +
        '<td class="col-amount">' + escapeHtml("$" + Number(inv.amount).toFixed(2)) + "</td>" +
        '<td><span class="sb-history-status ' + statusClass + '">' + escapeHtml(inv.status || "") + "</span></td>" +
        "<td>" + pdfCell + "</td>";
      historyTbody.appendChild(tr);
    });
  }

  function loadHistory(cursor) {
    var url = "/api/v1/billing/invoices";
    if (cursor) url += "?cursor=" + encodeURIComponent(cursor);
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { invoices: [] }; })
      .then(function (body) {
        var invoices = (body && body.invoices) || [];
        if (!invoices.length && historyState.cursor === 0) {
          if (historyEmpty) historyEmpty.textContent = "No invoices yet.";
          return;
        }
        renderHistoryRows(invoices);
        if (body.next_cursor) {
          historyState.cursor = body.next_cursor;
          if (historyMoreWrap) historyMoreWrap.style.display = "block";
        } else {
          historyState.done = true;
          if (historyMoreWrap) historyMoreWrap.style.display = "none";
        }
      })
      .catch(function () {
        if (historyEmpty) historyEmpty.textContent = "Failed to load invoices.";
      });
  }

  if (historyMoreBtn) {
    historyMoreBtn.addEventListener("click", function () {
      if (!historyState.done) loadHistory(historyState.cursor);
    });
  }
  // Kick off the initial fetch.
  loadHistory(0);

})();
