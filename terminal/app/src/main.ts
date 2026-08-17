// main.ts — narve Terminal shell: theme boot, chrome (command bar +
// status bar), UTC clock, sidecar health polling, router start.
//
// THEME BOOT is the first executed statement (imports below are
// side-effect-free) so tokens.css's [data-theme="dark"] block applies
// before first paint. Terminal is dark-first; a stored "light"
// preference (localStorage key "nv-theme") is honoured. index.html
// deliberately has no inline script — CSP stays script-src 'self'.
document.documentElement.dataset.theme =
  window.localStorage.getItem("nv-theme") === "light" ? "light" : "dark";

import { startRouter } from "./router.ts";

const API_BASE = "http://127.0.0.1:41733";
const APP_VERSION = "0.5.0";

interface Health {
  ok: boolean;
  version: string;
  db: string;
}

interface RawPage {
  rows: unknown[];
  total: number;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    throw new Error(`GET ${path} -> ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

/* ── Chrome ──────────────────────────────────────────────────────────
   Static shell markup. No user data flows through this template —
   screens render themselves into #nv-screen via the router. */
const app = document.getElementById("app");
if (!(app instanceof HTMLElement)) {
  throw new Error("index.html is missing the #app mount point");
}
app.innerHTML = `
  <header class="nv-cmdbar">
    <a class="nv-logo" href="#/markets" title="narve Terminal">
      <span class="nv-logo-mark" aria-hidden="true"></span>
      <span class="nv-logo-word">NARVE</span>
      <span class="nv-logo-sub">TERMINAL</span>
    </a>
    <nav class="nv-tabs" aria-label="Screens">
      <a class="nv-tab" data-screen="markets" href="#/markets">Markets</a>
      <a class="nv-tab" data-screen="sources" href="#/sources">Sources</a>
      <a class="nv-tab" data-screen="ingest" href="#/ingest">Ingest</a>
    </nav>
    <div class="nv-cmdbar-right">
      <span class="nv-clock" id="nv-clock" aria-label="UTC time"></span>
      <span class="nv-tag nv-tag--local" title="All data stays on this machine">LOCAL</span>
    </div>
  </header>
  <main class="nv-screen" id="nv-screen"></main>
  <footer class="nv-statusbar" id="nv-statusbar" aria-live="polite"></footer>
`;

/* ── UTC clock ─────────────────────────────────────────────────────── */
const clockEl = document.getElementById("nv-clock");

function tickClock(): void {
  if (!clockEl) return;
  const iso = new Date().toISOString(); // always UTC
  clockEl.textContent = `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`;
}
tickClock();
window.setInterval(tickClock, 1000);

/* ── Active tab ──────────────────────────────────────────────────────
   #/question/:id is a drill-down of MARKETS, so it lights that tab. */
function setActiveTab(screen: string): void {
  const tab = screen === "question" ? "markets" : screen;
  for (const a of document.querySelectorAll<HTMLAnchorElement>(".nv-tab")) {
    if (a.dataset.screen === tab) {
      a.setAttribute("aria-current", "page");
    } else {
      a.removeAttribute("aria-current");
    }
  }
}

/* ── Status bar ──────────────────────────────────────────────────────
   Online:  DB path · PRED/SNAP row counts · LAST INGEST · versions.
   Offline: a single honest line — the screens degrade on their own,
   the shell never blanks. Counts come from /raw/{kind}?limit=1
   (cheap: server returns {rows, total}). "LAST INGEST" updates when
   the ingest screen dispatches `nv:ingest` on window (optional
   CustomEvent with detail.at — falls back to "now"); it shows "—"
   until an upload happens this session. */
const statusEl = document.getElementById("nv-statusbar");
let lastIngest = "—";

window.addEventListener("nv:ingest", (ev: Event) => {
  const at = (ev as CustomEvent<{ at?: string }>).detail?.at;
  lastIngest = (at ?? new Date().toISOString()).slice(0, 19).replace("T", " ");
  void pollStatus();
});

function statusItem(label: string, value: string): string {
  const span = document.createElement("span");
  span.className = "nv-status-item";
  const lbl = document.createElement("span");
  lbl.className = "nv-status-lbl";
  lbl.textContent = label;
  const val = document.createElement("span");
  val.className = "nv-status-val";
  val.textContent = value;
  span.append(lbl, val);
  return span.outerHTML;
}

function tildify(path: string): string {
  return path.replace(/^\/(?:Users|home)\/[^/]+/, "~");
}

function renderStatusOnline(h: Health, preds: number, snaps: number): void {
  if (!statusEl) return;
  statusEl.classList.remove("is-offline");
  statusEl.innerHTML = [
    statusItem("DB", tildify(h.db)),
    statusItem("PRED", String(preds)),
    statusItem("SNAP", String(snaps)),
    statusItem("LAST INGEST", lastIngest),
    statusItem("SIDECAR", `v${h.version}`),
    statusItem("SHELL", `v${APP_VERSION}`),
  ].join('<span class="nv-status-sep" aria-hidden="true">·</span>');
}

function renderStatusOffline(): void {
  if (!statusEl) return;
  statusEl.classList.add("is-offline");
  statusEl.innerHTML = "";
  const msg = document.createElement("span");
  msg.className = "nv-status-item";
  msg.textContent =
    "SIDECAR OFFLINE — start it or launch via the app " +
    `(python3.11 -m narve_sidecar.server · expected on ${API_BASE.replace("http://", "")})`;
  const shell = document.createElement("span");
  shell.className = "nv-status-item nv-status-dim";
  shell.textContent = `SHELL v${APP_VERSION}`;
  statusEl.append(msg, shell);
}

let statusPolling = false;

async function pollStatus(): Promise<void> {
  if (statusPolling) return; // never stack slow polls
  statusPolling = true;
  try {
    const h = await getJson<Health>("/health");
    let preds = 0;
    let snaps = 0;
    try {
      preds = (await getJson<RawPage>("/raw/predictions?limit=1")).total;
      snaps = (await getJson<RawPage>("/raw/markets?limit=1")).total;
    } catch {
      /* health is up but /raw hiccuped — still online, counts stay 0 */
    }
    renderStatusOnline(h, preds, snaps);
  } catch {
    renderStatusOffline();
  } finally {
    statusPolling = false;
  }
}

void pollStatus();
window.setInterval(() => void pollStatus(), 10_000);

/* ── Router ────────────────────────────────────────────────────────── */
const screenRoot = document.getElementById("nv-screen");
if (!(screenRoot instanceof HTMLElement)) {
  throw new Error("shell chrome failed to render (#nv-screen missing)");
}
startRouter(screenRoot, (match) => setActiveTab(match.screen));
