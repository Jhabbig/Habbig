// screens/markets.ts — flagship dense grid. Every question on one tape,
// sorted |edge| desc, click-through to the question drill.
import {
  el,
  errorMessage,
  fmtProb,
  fmtSigned,
  fmtTime,
  getQuestions,
  sampleTag,
  td,
  theadRow,
} from "../api.ts";
import type { QuestionRow } from "../api.ts";

const COLS = ["QUESTION", "SRCS", "NARVE P", "MKT", "EDGE", "STATUS", "UPD"];

export function mount(root: HTMLElement, _params: Record<string, string>): void {
  root.textContent = "";
  const panel = el("section", "nv-panel");
  panel.appendChild(el("div", "nv-marker", "[ MARKETS / 01 ]"));
  const strip = el("div", "nv-strip", "LOADING TAPE…");
  panel.appendChild(strip);
  const body = el("div");
  panel.appendChild(body);
  root.appendChild(panel);
  void render(strip, body);
}

async function render(strip: HTMLElement, body: HTMLElement): Promise<void> {
  let rows: QuestionRow[];
  try {
    rows = await getQuestions();
  } catch (err) {
    strip.textContent = "";
    body.appendChild(el("div", "nv-empty", errorMessage(err)));
    return;
  }

  const live = rows.filter((r) => r.status === "live").length;
  const resolved = rows.filter((r) => r.status === "resolved").length;
  const voided = rows.filter((r) => r.status === "void").length;
  strip.textContent =
    `${rows.length} QUESTIONS · ${live} LIVE · ${resolved} RESOLVED` +
    (voided > 0 ? ` · ${voided} VOID` : "") +
    "  —  RESOLVE FEEDS CREDIBILITY — THE LOOP";

  if (rows.length === 0) {
    const empty = el(
      "div",
      "nv-empty",
      "No questions on the tape. Ingest predictions — or load the sample set — from the INGEST screen. ",
    );
    const a = el("a", "nv-btn", "GO TO INGEST");
    a.href = "#/ingest";
    empty.appendChild(a);
    body.appendChild(empty);
    return;
  }

  // API returns |edge| desc already; re-sort defensively (null edge last).
  rows.sort((a, b) => edgeKey(b) - edgeKey(a));

  const table = el("table", "nv-table");
  table.appendChild(theadRow(COLS));
  const tbody = el("tbody");
  for (const q of rows) tbody.appendChild(row(q));
  table.appendChild(tbody);
  body.appendChild(table);
}

function edgeKey(q: QuestionRow): number {
  return q.edge === null || !Number.isFinite(q.edge) ? -1 : Math.abs(q.edge);
}

function row(q: QuestionRow): HTMLTableRowElement {
  const tr = el("tr");
  const title = td(q.title || q.id);
  if (q.is_sample) title.appendChild(sampleTag());
  tr.appendChild(title);
  tr.appendChild(td(String(q.n_sources), "nv-num"));
  tr.appendChild(td(fmtProb(q.combined_p), "nv-num"));
  tr.appendChild(td(fmtProb(q.market_price), "nv-num"));
  tr.appendChild(td(fmtSigned(q.edge), "nv-num"));
  tr.appendChild(td(q.status.toUpperCase()));
  tr.appendChild(td(fmtTime(q.updated_at), "nv-num"));
  tr.tabIndex = 0;
  const go = () => {
    location.hash = "#/question/" + encodeURIComponent(q.id);
  };
  tr.addEventListener("click", go);
  tr.addEventListener("keydown", (e) => {
    if (e.key === "Enter") go();
  });
  return tr;
}
