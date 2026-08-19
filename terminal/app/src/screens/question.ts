// screens/question.ts — per-question drill: big combined_p, market quotes,
// per-source table, RESOLVE YES/NO/VOID with confirm modal + inline moves.
import {
  el,
  errorMessage,
  fmtInt,
  fmtProb,
  fmtSigned,
  fmtTime,
  getQuestion,
  resolveQuestion,
  sampleTag,
  td,
  theadRow,
} from "../api.ts";
import type {
  BiasSpread,
  Outcome,
  QuestionDetail,
  RelevantSource,
  ResolveResult,
  Snapshot,
} from "../api.ts";

export function mount(root: HTMLElement, params: Record<string, string>): void {
  root.textContent = "";
  const panel = el("section", "nv-panel");
  panel.appendChild(el("div", "nv-marker", "[ QUESTION / 03 ]"));
  const back = el("a", undefined, "← MARKETS");
  back.href = "#/markets";
  panel.appendChild(back);
  const body = el("div");
  panel.appendChild(body);
  root.appendChild(panel);

  const raw = params["id"];
  if (raw === undefined || raw === "") {
    body.appendChild(
      el("div", "nv-empty", "No question id in the route. Pick a row on the MARKETS tape."),
    );
    return;
  }
  void render(body, decodeURIComponent(raw));
}

async function render(body: HTMLElement, id: string): Promise<void> {
  body.textContent = "LOADING QUESTION…";
  let d: QuestionDetail;
  try {
    d = await getQuestion(id);
  } catch (err) {
    body.textContent = "";
    body.appendChild(el("div", "nv-empty", errorMessage(err)));
    return;
  }
  body.textContent = "";

  const q = d.question;
  const title = el("h1", undefined, q.title || q.id);
  if (q.is_sample) title.appendChild(sampleTag());
  body.appendChild(title);
  const statusLine = el("div", "nv-strip", statusText(d));
  body.appendChild(statusLine);

  // big combined_p + edge vs latest snapshot across venues
  const bigWrap = el("div");
  bigWrap.appendChild(el("div", "nv-marker", "[ COMBINED P ]"));
  const big = el("div", "nv-num", fmtProb(d.combined_p));
  big.style.fontSize = "40px";
  big.style.lineHeight = "1.1";
  bigWrap.appendChild(big);
  const latest = latestSnapshot(d);
  if (latest !== null && d.combined_p !== null) {
    bigWrap.appendChild(
      el(
        "div",
        undefined,
        `EDGE ${fmtSigned(d.combined_p - latest.yes_price)} VS ${latest.venue.toUpperCase()} ` +
          `@ ${fmtProb(latest.yes_price)}`,
      ),
    );
  }
  body.appendChild(bigWrap);

  body.appendChild(marketBlock(d));
  body.appendChild(perSourceBlock(d));
  body.appendChild(sourceContextBlock(d));
  body.appendChild(messagesBlock(d));
  body.appendChild(resolveBlock(d, statusLine));
}

function statusText(d: QuestionDetail): string {
  const q = d.question;
  if (q.status === "resolved") {
    const oc = q.resolved_outcome === 1 ? "YES" : "NO";
    return `${q.id} · RESOLVED ${oc} · ${fmtTime(q.resolved_at)}`;
  }
  if (q.status === "void") return `${q.id} · VOID — no credibility changes`;
  return `${q.id} · LIVE · ${d.per_source.length} SOURCES`;
}

function latestSnapshot(d: QuestionDetail): Snapshot | null {
  let best: Snapshot | null = null;
  for (const s of d.market) {
    if (best === null || s.captured_at > best.captured_at) best = s;
  }
  return best;
}

function marketBlock(d: QuestionDetail): HTMLElement {
  const wrap = el("div");
  wrap.appendChild(el("div", "nv-marker", "[ MARKET ]"));
  if (d.market.length === 0) {
    wrap.appendChild(
      el(
        "div",
        "nv-empty",
        "No market snapshots for this question — ingest markets rows to price the edge.",
      ),
    );
    return wrap;
  }
  const table = el("table", "nv-table");
  table.appendChild(theadRow(["VENUE", "YES", "LIQ", "CAPTURED"]));
  const tbody = el("tbody");
  for (const m of d.market) {
    const tr = el("tr");
    tr.appendChild(td(m.venue.toUpperCase()));
    tr.appendChild(td(fmtProb(m.yes_price), "nv-num"));
    tr.appendChild(td(fmtInt(m.liquidity), "nv-num"));
    tr.appendChild(td(fmtTime(m.captured_at), "nv-num"));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  if (d.history.length >= 2) {
    wrap.appendChild(
      el("div", undefined, `HISTORY ${spark(d.history)} · ${d.history.length} SNAPSHOTS`),
    );
  }
  return wrap;
}

// Mono sparkline of yes_price history (asc), fixed 0..1 scale.
function spark(history: Snapshot[]): string {
  const GLYPHS = "▁▂▃▄▅▆▇█";
  let out = "";
  for (const s of history.slice(-40)) {
    const clamped = Math.max(0, Math.min(1, s.yes_price));
    const idx = Math.min(GLYPHS.length - 1, Math.floor(clamped * GLYPHS.length));
    out += GLYPHS[idx] ?? "▁";
  }
  return out;
}

function perSourceBlock(d: QuestionDetail): HTMLElement {
  const wrap = el("div");
  wrap.appendChild(el("div", "nv-marker", "[ SOURCES ON THIS QUESTION ]"));
  if (d.per_source.length === 0) {
    wrap.appendChild(
      el(
        "div",
        "nv-empty",
        "No source has called this question yet — ingest predictions rows to build a consensus.",
      ),
    );
    return wrap;
  }
  const table = el("table", "nv-table");
  table.appendChild(theadRow(["SOURCE", "CRED", "P", "STATED"]));
  const tbody = el("tbody");
  for (const s of d.per_source) {
    const tr = el("tr");
    tr.appendChild(td(s.source_id));
    tr.appendChild(td(fmtProb(s.credibility), "nv-num"));
    tr.appendChild(td(fmtProb(s.p), "nv-num"));
    tr.appendChild(td(fmtTime(s.stated_at), "nv-num"));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

// ── SOURCE CONTEXT — who is calling this question ────────────────────────
// Bias/region composition of the predicting sources, the sidecar's DJT skew
// note when present (emphasis via weight+border — never hue), and WORTH
// HEARING FROM: topic-matched sources with no prediction on it yet.

const BIAS_ORDER: (keyof BiasSpread)[] = [
  "left",
  "lean-left",
  "center",
  "lean-right",
  "right",
  "unknown",
];

function sourceContextBlock(d: QuestionDetail): HTMLElement {
  const wrap = el("div");
  const ctx = d.source_context ?? null; // tolerate a pre-v3 sidecar mid-rollout
  const relevant: RelevantSource[] = d.relevant_sources ?? [];
  if (ctx === null && relevant.length === 0) return wrap;

  wrap.appendChild(el("div", "nv-marker", "[ SOURCE CONTEXT ]"));
  if (ctx !== null) {
    const biasLine = BIAS_ORDER.map((k) => `${k} ${ctx.bias_spread[k]}`).join(" · ");
    wrap.appendChild(el("div", undefined, `BIAS ${biasLine}`));
    const regionLine = Object.entries(ctx.region_spread)
      .sort((a, b) => b[1] - a[1])
      .map(([region, n]) => `${region} ${n}`)
      .join(" · ");
    wrap.appendChild(el("div", undefined, `REGION ${regionLine || "—"}`));
    if (ctx.skew_note !== null && ctx.skew_note !== "") {
      const note = el("div", undefined, ctx.skew_note);
      note.style.cssText =
        "border:1px solid currentColor;padding:8px 10px;margin:8px 0;" +
        "font-weight:600;max-width:520px;";
      wrap.appendChild(note);
    }
  }

  if (relevant.length > 0) {
    wrap.appendChild(el("div", "nv-marker", "[ WORTH HEARING FROM ]"));
    wrap.appendChild(
      el(
        "div",
        undefined,
        "Topic-matched sources with no call on this question yet.",
      ),
    );
    const table = el("table", "nv-table");
    table.appendChild(theadRow(["SOURCE", "CRED", "MATCH"]));
    const tbody = el("tbody");
    for (const r of relevant) {
      const tr = el("tr");
      tr.appendChild(td(r.source_id));
      tr.appendChild(td(fmtProb(r.credibility), "nv-num"));
      tr.appendChild(td(fmtProb(r.match, 2), "nv-num"));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
  }
  return wrap;
}

// Texts from person-sources on this question, newest first. Stance is the
// sender's stated probability; context-only texts show an em-dash.
function messagesBlock(d: QuestionDetail): HTMLElement {
  const wrap = el("div");
  wrap.appendChild(el("div", "nv-marker", "[ MESSAGES ]"));
  const messages = d.messages ?? []; // tolerate a pre-v2 sidecar mid-rollout
  if (messages.length === 0) {
    wrap.appendChild(
      el(
        "div",
        "nv-empty",
        "No messages on this question — ingest texts from the MESSAGES card.",
      ),
    );
    return wrap;
  }
  const table = el("table", "nv-table");
  table.appendChild(theadRow(["SOURCE", "STANCE", "SENT", "TEXT"]));
  const tbody = el("tbody");
  for (const m of messages) {
    const tr = el("tr");
    tr.appendChild(td(m.source_id));
    tr.appendChild(td(fmtProb(m.stance_p), "nv-num"));
    tr.appendChild(td(fmtTime(m.sent_at), "nv-num"));
    tr.appendChild(td(m.text));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function resolveBlock(d: QuestionDetail, statusLine: HTMLElement): HTMLElement {
  const wrap = el("div");
  if (d.question.status !== "live") return wrap; // already resolved/void
  wrap.appendChild(el("div", "nv-marker", "[ RESOLVE ]"));
  wrap.appendChild(
    el(
      "div",
      undefined,
      "Resolution feeds credibility: each source's latest call is a hit iff " +
        "(p ≥ 0.5) matches the outcome. VOID changes nothing.",
    ),
  );
  const buttons = el("div");
  const result = el("div");
  const n = d.per_source.length;
  for (const outcome of ["yes", "no", "void"] as Outcome[]) {
    const label = outcome === "void" ? "VOID" : `RESOLVE ${outcome.toUpperCase()}`;
    const btn = el("button", "nv-btn", label);
    btn.addEventListener("click", () => {
      const copy =
        outcome === "void"
          ? `Void ${d.question.id}? No credibility will move.`
          : `Resolve ${d.question.id} as ${outcome.toUpperCase()}? ` +
            `This moves credibility for ${n} source${n === 1 ? "" : "s"} and cannot be re-run.`;
      confirmModal(copy, () => void doResolve(d, outcome, buttons, result, statusLine));
    });
    buttons.appendChild(btn);
  }
  wrap.appendChild(buttons);
  wrap.appendChild(result);
  return wrap;
}

async function doResolve(
  d: QuestionDetail,
  outcome: Outcome,
  buttons: HTMLElement,
  result: HTMLElement,
  statusLine: HTMLElement,
): Promise<void> {
  for (const b of buttons.querySelectorAll("button")) b.disabled = true;
  result.textContent = "RESOLVING…";
  let res: ResolveResult;
  try {
    res = await resolveQuestion(d.question.id, outcome);
  } catch (err) {
    result.textContent = "";
    result.appendChild(el("div", "nv-empty", errorMessage(err)));
    for (const b of buttons.querySelectorAll("button")) b.disabled = false;
    return;
  }
  buttons.remove();
  result.textContent = "";
  if (outcome === "void") {
    statusLine.textContent = `${d.question.id} · VOID — no credibility changes`;
    result.appendChild(el("div", "nv-empty", "VOIDED — no credibility changed."));
    return;
  }
  statusLine.textContent = `${d.question.id} · RESOLVED ${outcome.toUpperCase()} · just now`;
  result.appendChild(el("div", "nv-marker", "[ CREDIBILITY MOVES ]"));
  if (res.moves.length === 0) {
    result.appendChild(
      el("div", "nv-empty", "Resolved — no sources had predictions, so nothing moved."),
    );
    return;
  }
  const table = el("table", "nv-table");
  table.appendChild(theadRow(["SOURCE", "CRED", "Δ", "CALL"]));
  const tbody = el("tbody");
  for (const m of res.moves) {
    const tr = el("tr");
    tr.appendChild(td(m.source_id));
    tr.appendChild(td(`${fmtProb(m.old_cred)} → ${fmtProb(m.new_cred)}`, "nv-num"));
    tr.appendChild(td(fmtSigned(m.new_cred - m.old_cred), "nv-num"));
    tr.appendChild(td(m.hit ? "HIT" : "MISS"));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  result.appendChild(table);
}

function confirmModal(text: string, onConfirm: () => void): void {
  const overlay = el("div");
  overlay.style.cssText =
    "position:fixed;inset:0;display:flex;align-items:center;justify-content:center;" +
    "background:rgba(0,0,0,0.65);z-index:100;";
  const box = el("div", "nv-panel");
  box.style.cssText = "min-width:360px;max-width:520px;padding:16px;";
  box.appendChild(el("div", "nv-marker", "[ CONFIRM ]"));
  box.appendChild(el("p", undefined, text));
  const row = el("div");
  row.style.cssText = "display:flex;gap:8px;margin-top:12px;";
  const ok = el("button", "nv-btn", "CONFIRM");
  ok.addEventListener("click", () => {
    overlay.remove();
    onConfirm();
  });
  const cancel = el("button", "nv-btn", "CANCEL");
  cancel.addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });
  row.append(ok, cancel);
  box.appendChild(row);
  overlay.appendChild(box);
  document.body.appendChild(overlay);
}
