// screens/sources.ts — credibility leaderboard. Cred drawn as mono block
// bars; a row expands into that source's credibility_events history.
import {
  credBar,
  el,
  errorMessage,
  fmtAB,
  fmtCtx,
  fmtProb,
  fmtSigned,
  fmtTime,
  getSource,
  getSources,
  sampleTag,
  td,
  theadRow,
} from "../api.ts";
import type { CredibilityEvent, SourceRow } from "../api.ts";

const COLS = [
  "SOURCE",
  "KIND",
  "BIAS",
  "FROM",
  "CRED",
  "α/β",
  "RESOLVED",
  "LIVE",
  "BRIER",
  "LAST",
];
const NCOLS = COLS.length;

export function mount(root: HTMLElement, _params: Record<string, string>): void {
  root.textContent = "";
  const panel = el("section", "nv-panel");
  panel.appendChild(el("div", "nv-marker", "[ SOURCES / 02 ]"));
  const strip = el("div", "nv-strip", "LOADING LEADERBOARD…");
  panel.appendChild(strip);
  const body = el("div");
  panel.appendChild(body);
  root.appendChild(panel);
  void render(strip, body);
}

async function render(strip: HTMLElement, body: HTMLElement): Promise<void> {
  let rows: SourceRow[];
  try {
    rows = await getSources();
  } catch (err) {
    strip.textContent = "";
    body.appendChild(el("div", "nv-empty", errorMessage(err)));
    return;
  }

  const moved = rows.filter((r) => r.n_resolved > 0).length;
  strip.textContent =
    `${rows.length} SOURCES · ${moved} WITH RESOLVED CALLS · PRIOR α=2 β=2 (CRED 0.500)` +
    " — CLICK A ROW FOR ITS HISTORY";

  if (rows.length === 0) {
    body.appendChild(
      el(
        "div",
        "nv-empty",
        "No sources tracked yet. Sources appear automatically when their predictions " +
          "or messages are ingested — every new source starts neutral at credibility 0.500.",
      ),
    );
    return;
  }

  const table = el("table", "nv-table");
  table.appendChild(theadRow(COLS));
  const tbody = el("tbody");
  for (const s of rows) tbody.appendChild(row(s, tbody));
  table.appendChild(tbody);
  body.appendChild(table);
}

function row(s: SourceRow, tbody: HTMLTableSectionElement): HTMLTableRowElement {
  const tr = el("tr");
  const name = td(s.name || s.id);
  if (s.is_sample) name.appendChild(sampleTag());
  tr.appendChild(name);
  const kind = td("");
  kind.appendChild(el("span", "nv-tag", (s.kind || "model").toUpperCase()));
  tr.appendChild(kind);
  // account context — compact, text-only; unknown renders as em-dash
  tr.appendChild(td(fmtCtx(s.bias)));
  tr.appendChild(td(fmtCtx(s.region)));
  const cred = td(credBar(s.credibility) + " ");
  cred.appendChild(el("span", "nv-num", fmtProb(s.credibility)));
  tr.appendChild(cred);
  tr.appendChild(td(`${fmtAB(s.alpha)}/${fmtAB(s.beta)}`, "nv-num"));
  tr.appendChild(td(String(s.n_resolved), "nv-num"));
  tr.appendChild(td(String(s.n_live), "nv-num"));
  tr.appendChild(td(fmtProb(s.brier), "nv-num"));
  tr.appendChild(td(fmtTime(s.last_active), "nv-num"));
  tr.tabIndex = 0;
  const toggle = () => void toggleDetail(tr, tbody, s.id);
  tr.addEventListener("click", toggle);
  tr.addEventListener("keydown", (e) => {
    if (e.key === "Enter") toggle();
  });
  return tr;
}

async function toggleDetail(
  tr: HTMLTableRowElement,
  tbody: HTMLTableSectionElement,
  sourceId: string,
): Promise<void> {
  const next = tr.nextElementSibling;
  if (next !== null && next.classList.contains("nv-detail")) {
    next.remove(); // collapse
    return;
  }
  const detail = el("tr", "nv-detail");
  const cell = el("td");
  cell.colSpan = NCOLS;
  cell.textContent = "LOADING HISTORY…";
  detail.appendChild(cell);
  tbody.insertBefore(detail, tr.nextSibling);

  try {
    const d = await getSource(sourceId);
    cell.textContent = "";
    cell.appendChild(eventsBlock(d.events));
  } catch (err) {
    cell.textContent = "";
    cell.appendChild(el("div", "nv-empty", errorMessage(err)));
  }
}

function eventsBlock(events: CredibilityEvent[]): HTMLElement {
  const wrap = el("div");
  wrap.appendChild(el("div", "nv-marker", "[ CREDIBILITY EVENTS ]"));
  if (events.length === 0) {
    wrap.appendChild(
      el(
        "div",
        "nv-empty",
        "No credibility events yet — this source moves the first time one of " +
          "its questions resolves.",
      ),
    );
    return wrap;
  }
  const table = el("table", "nv-table");
  table.appendChild(theadRow(["AT", "QUESTION", "α/β", "CRED", "Δ"]));
  const tbody = el("tbody");
  // newest first for reading history
  for (const ev of [...events].reverse()) {
    const oldCred = ev.old_alpha / (ev.old_alpha + ev.old_beta);
    const newCred = ev.new_alpha / (ev.new_alpha + ev.new_beta);
    const tr = el("tr");
    tr.appendChild(td(fmtTime(ev.at), "nv-num"));
    tr.appendChild(td(ev.question_id));
    tr.appendChild(
      td(
        `${fmtAB(ev.old_alpha)}/${fmtAB(ev.old_beta)} → ${fmtAB(ev.new_alpha)}/${fmtAB(ev.new_beta)}`,
        "nv-num",
      ),
    );
    tr.appendChild(td(`${fmtProb(oldCred)} → ${fmtProb(newCred)}`, "nv-num"));
    tr.appendChild(td(fmtSigned(newCred - oldCred), "nv-num"));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}
