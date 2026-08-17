// screens/ingest.ts — four upload cards (drag-drop + picker), template
// downloads, LOAD SAMPLE DATA, full LINE·REASON error table, raw-data peek.
// CSV + JSON array only in v0.5 (XLSX lands in v1).
import {
  el,
  errorMessage,
  getRaw,
  ingestFile,
  loadSample,
  templateUrl,
  td,
  theadRow,
} from "../api.ts";
import type { IngestKind, IngestResult } from "../api.ts";

const KINDS: { kind: IngestKind; cols: string }[] = [
  {
    kind: "predictions",
    cols: "source_id · question_id · predicted_probability · stated_at · note?",
  },
  {
    kind: "markets",
    cols: "venue · market_id · question_id · yes_price · liquidity? · captured_at",
  },
  { kind: "resolutions", cols: "question_id · outcome (yes|no|void) · resolved_at" },
  { kind: "messages", cols: "source_id · text · sent_at · question_id? · stance?" },
];

export function mount(root: HTMLElement, _params: Record<string, string>): void {
  root.textContent = "";
  const panel = el("section", "nv-panel");
  panel.appendChild(el("div", "nv-marker", "[ INGEST / 04 ]"));
  panel.appendChild(
    el(
      "div",
      "nv-strip",
      "CSV OR JSON ARRAY · STRICT — EVERY ROW ERROR REPORTED AT ONCE · " +
        "RE-UPLOADS DEDUP CLEANLY · XLSX LANDS IN V1",
    ),
  );

  // sample loader
  const sampleRow = el("div");
  const sampleBtn = el("button", "nv-btn", "LOAD SAMPLE DATA");
  const sampleStatus = el(
    "span",
    undefined,
    " 12 markets + 2 pre-resolved questions + 1 texter with 3 messages, tagged SAMPLE",
  );
  sampleRow.append(sampleBtn, sampleStatus);
  panel.appendChild(sampleRow);

  const resultPanel = el("div");
  const rawBody = el("div");

  // four upload cards
  const cards = el("div");
  cards.style.cssText = "display:flex;gap:12px;align-items:stretch;flex-wrap:wrap;";
  for (const { kind, cols } of KINDS) {
    cards.appendChild(card(kind, cols, resultPanel, rawBody));
  }
  panel.appendChild(cards);
  panel.appendChild(resultPanel);

  // raw peek
  panel.appendChild(el("div", "nv-marker", "[ RAW / PEEK ]"));
  const rawTabs = el("div");
  for (const { kind } of KINDS) {
    const b = el("button", "nv-btn", kind.toUpperCase());
    b.dataset["kind"] = kind;
    b.addEventListener("click", () => void renderRaw(rawBody, kind));
    rawTabs.appendChild(b);
  }
  panel.appendChild(rawTabs);
  panel.appendChild(rawBody);
  root.appendChild(panel);

  sampleBtn.addEventListener("click", () => {
    void doSample(sampleBtn, sampleStatus, rawBody);
  });
  void renderRaw(rawBody, "predictions");
}

async function doSample(
  btn: HTMLButtonElement,
  status: HTMLElement,
  rawBody: HTMLElement,
): Promise<void> {
  btn.disabled = true;
  status.textContent = " LOADING SAMPLE…";
  try {
    const res = await loadSample();
    const c = res.counts;
    const msgs = c.messages !== undefined ? ` · ${c.messages} MESSAGES` : "";
    status.textContent =
      ` LOADED · ${c.sources} SOURCES · ${c.questions} QUESTIONS · ` +
      `${c.predictions} PREDICTIONS · ${c.snapshots} SNAPSHOTS${msgs} — see MARKETS`;
    window.dispatchEvent(
      new CustomEvent("nv:ingest", { detail: { at: new Date().toISOString() } }),
    );
    void renderRaw(rawBody, "predictions");
  } catch (err) {
    status.textContent = " " + errorMessage(err);
  } finally {
    btn.disabled = false;
  }
}

function card(
  kind: IngestKind,
  cols: string,
  resultPanel: HTMLElement,
  rawBody: HTMLElement,
): HTMLElement {
  const c = el("div", "nv-panel");
  c.style.cssText = "flex:1 1 260px;min-width:240px;";
  c.appendChild(el("div", "nv-marker", `[ ${kind.toUpperCase()} ]`));
  c.appendChild(el("div", undefined, cols));

  const status = el("div", undefined, "");
  const drop = el("div", undefined, "DROP CSV / JSON HERE — OR CLICK");
  drop.style.cssText =
    "border:1px dashed currentColor;padding:16px 8px;margin:8px 0;" +
    "text-align:center;cursor:pointer;";
  const input = el("input", "nv-input");
  input.type = "file";
  input.accept = ".csv,.json,text/csv,application/json";
  input.style.display = "none";

  const take = (file: File | undefined) => {
    if (file === undefined) return;
    if (!/\.(csv|json)$/i.test(file.name)) {
      status.textContent = `${file.name}: only .csv or .json in v0.5 — XLSX lands in v1.`;
      return;
    }
    void upload(kind, file, status, resultPanel, rawBody);
  };

  drop.addEventListener("click", () => input.click());
  drop.addEventListener("dragover", (e) => e.preventDefault());
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    take(e.dataTransfer?.files[0]);
  });
  input.addEventListener("change", () => {
    take(input.files?.[0] ?? undefined);
    input.value = ""; // allow re-picking the same file
  });

  const tpl = el("a", undefined, `TEMPLATE ${kind}.csv`);
  tpl.href = templateUrl(kind);
  tpl.addEventListener("click", (e) => {
    e.preventDefault(); // cross-origin: fetch → blob so the webview never navigates away
    void downloadTemplate(kind, status);
  });

  c.append(drop, input, tpl, status);
  return c;
}

async function downloadTemplate(kind: IngestKind, status: HTMLElement): Promise<void> {
  try {
    const res = await fetch(templateUrl(kind));
    if (!res.ok) throw new Error(`template fetch failed (${res.status})`);
    const url = URL.createObjectURL(await res.blob());
    const a = el("a");
    a.href = url;
    a.download = `${kind}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    status.textContent = errorMessage(err);
  }
}

async function upload(
  kind: IngestKind,
  file: File,
  status: HTMLElement,
  resultPanel: HTMLElement,
  rawBody: HTMLElement,
): Promise<void> {
  status.textContent = `UPLOADING ${file.name}…`;
  try {
    const res = await ingestFile(kind, file);
    status.textContent = `${file.name}: OK ${res.ok_rows} · ERR ${res.err_rows}`;
    renderResult(resultPanel, kind, file.name, res);
    if (res.ok_rows > 0) {
      window.dispatchEvent(
        new CustomEvent("nv:ingest", { detail: { at: new Date().toISOString() } }),
      );
      void renderRaw(rawBody, kind);
    }
  } catch (err) {
    status.textContent = "";
    resultPanel.textContent = "";
    resultPanel.appendChild(el("div", "nv-marker", "[ RESULT ]"));
    resultPanel.appendChild(el("div", "nv-empty", `${file.name}: ${errorMessage(err)}`));
  }
}

function renderResult(
  panel: HTMLElement,
  kind: IngestKind,
  fileName: string,
  res: IngestResult,
): void {
  panel.textContent = "";
  panel.appendChild(el("div", "nv-marker", "[ RESULT ]"));
  panel.appendChild(
    el(
      "div",
      "nv-strip",
      `${kind.toUpperCase()} · ${fileName} · OK ${res.ok_rows} · ERR ${res.err_rows} · ` +
        `DEDUP ${res.dedup_skipped}`,
    ),
  );
  if (res.already_ingested) {
    panel.appendChild(
      el(
        "div",
        "nv-empty",
        "This exact file was already ingested (identical sha256) — nothing re-applied.",
      ),
    );
    return;
  }
  if (res.errors.length > 0) {
    const table = el("table", "nv-table");
    table.appendChild(theadRow(["LINE", "REASON"]));
    const tbody = el("tbody");
    for (const e of res.errors) {
      const tr = el("tr");
      tr.appendChild(td(String(e.line), "nv-num"));
      tr.appendChild(td(e.reason));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    panel.appendChild(table);
  } else if (res.ok_rows > 0) {
    panel.appendChild(el("div", undefined, "APPLIED CLEANLY — no row errors."));
  }
}

async function renderRaw(rawBody: HTMLElement, kind: IngestKind): Promise<void> {
  rawBody.textContent = `LOADING RAW ${kind.toUpperCase()}…`;
  try {
    const res = await getRaw(kind, { limit: 50 });
    rawBody.textContent = "";
    if (res.rows.length === 0) {
      rawBody.appendChild(
        el(
          "div",
          "nv-empty",
          `No raw ${kind} rows yet — upload a file above or load the sample data.`,
        ),
      );
      return;
    }
    rawBody.appendChild(
      el(
        "div",
        "nv-strip",
        `RAW ${kind.toUpperCase()} · SHOWING ${res.rows.length} OF ${res.total}`,
      ),
    );
    const first = res.rows[0];
    const keys = first === undefined ? [] : Object.keys(first);
    const table = el("table", "nv-table");
    table.appendChild(theadRow(keys.map((k) => k.toUpperCase())));
    const tbody = el("tbody");
    for (const row of res.rows) {
      const tr = el("tr");
      for (const k of keys) tr.appendChild(td(cellText(row[k])));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    rawBody.appendChild(table);
  } catch (err) {
    rawBody.textContent = "";
    rawBody.appendChild(el("div", "nv-empty", errorMessage(err)));
  }
}

function cellText(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
