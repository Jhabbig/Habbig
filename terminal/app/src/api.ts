// api.ts — typed client for the narve credibility sidecar
// (FastAPI, http://127.0.0.1:41733). Shapes mirror CONTRACT.md §API exactly.
// No libraries. This module also hosts the small shared DOM/format helpers
// used by the four screens (the contract layout allocates no util module).

export const API_BASE = "http://127.0.0.1:41733";

export type IngestKind = "predictions" | "markets" | "resolutions";
export type Outcome = "yes" | "no" | "void";

export interface Health {
  ok: boolean;
  version: string;
  db: string;
}

export interface IngestError {
  line: number; // 1-based data line
  reason: string;
}

export interface IngestResult {
  ok_rows: number;
  err_rows: number;
  dedup_skipped: number;
  already_ingested: boolean;
  errors: IngestError[];
}

export interface SampleLoadResult {
  loaded: boolean;
  counts: {
    sources: number;
    questions: number;
    predictions: number;
    snapshots: number;
  };
}

export interface SourceRow {
  id: string;
  name: string;
  alpha: number;
  beta: number;
  credibility: number;
  n_resolved: number;
  n_live: number;
  brier: number | null;
  is_sample: number;
  last_active: string | null;
}

export interface CredibilityEvent {
  id: number;
  source_id: string;
  question_id: string;
  old_alpha: number;
  old_beta: number;
  new_alpha: number;
  new_beta: number;
  at: string;
}

export interface PredictionRow {
  id: number;
  source_id: string;
  question_id: string;
  p: number;
  stated_at: string;
  note: string | null;
}

export interface SourceDetail extends SourceRow {
  events: CredibilityEvent[];
  predictions: PredictionRow[];
}

export interface QuestionRow {
  id: string;
  title: string;
  status: string; // 'live' | 'resolved' | 'void'
  n_sources: number;
  combined_p: number | null;
  market_price: number | null;
  edge: number | null;
  is_sample: number;
  updated_at: string | null;
}

export interface Question {
  id: string;
  title: string;
  status: string;
  resolved_outcome: number | null; // 1 = yes, 0 = no, null = unresolved/void
  resolved_at: string | null;
  is_sample: number;
  created_at: string;
}

export interface Snapshot {
  id: number;
  venue: string;
  market_id: string;
  question_id: string;
  yes_price: number;
  liquidity: number | null;
  captured_at: string;
}

export interface PerSource {
  source_id: string;
  credibility: number;
  p: number;
  stated_at: string;
}

export interface QuestionDetail {
  question: Question;
  per_source: PerSource[];
  combined_p: number | null;
  market: Snapshot[]; // latest snapshot per venue
  history: Snapshot[]; // all snapshots, captured_at asc
}

export interface ResolveMove {
  source_id: string;
  old_cred: number;
  new_cred: number;
  hit: boolean;
}

export interface ResolveResult {
  resolved: boolean;
  moves: ResolveMove[];
}

export interface RawResult {
  rows: Record<string, unknown>[];
  total: number;
}

export interface RawQuery {
  question_id?: string;
  source_id?: string;
  limit?: number;
  offset?: number;
}

// ── error normalization ──────────────────────────────────────────────────
// Every failure surfaces as ApiError. status 0 = sidecar unreachable;
// otherwise the HTTP status with FastAPI's `detail` as the message.

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const OFFLINE_COPY =
  "SIDECAR OFFLINE — no response from 127.0.0.1:41733. " +
  "The credibility engine may still be starting; retry in a moment.";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(API_BASE + path, init);
  } catch {
    throw new ApiError(0, OFFLINE_COPY);
  }
  if (!res.ok) {
    let detail = res.statusText || `request failed (${res.status})`;
    try {
      const body: unknown = await res.json();
      if (body !== null && typeof body === "object" && "detail" in body) {
        const d = (body as { detail: unknown }).detail;
        if (typeof d === "string") detail = d;
        else if (d !== null && d !== undefined) detail = JSON.stringify(d);
      }
    } catch {
      // non-JSON error body — keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

// ── endpoints (CONTRACT.md §API) ─────────────────────────────────────────

export function getHealth(): Promise<Health> {
  return request<Health>("/health");
}

export function ingestFile(kind: IngestKind, file: File): Promise<IngestResult> {
  const fd = new FormData();
  fd.append("file", file, file.name);
  return request<IngestResult>(`/ingest/${kind}`, { method: "POST", body: fd });
}

export function ingestRows(kind: IngestKind, rows: unknown[]): Promise<IngestResult> {
  return request<IngestResult>(`/ingest/${kind}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rows }),
  });
}

export function templateUrl(kind: IngestKind): string {
  return `${API_BASE}/templates/${kind}.csv`;
}

export function loadSample(): Promise<SampleLoadResult> {
  return request<SampleLoadResult>("/sample/load", { method: "POST" });
}

export function getSources(): Promise<SourceRow[]> {
  return request<SourceRow[]>("/sources");
}

export function getSource(id: string): Promise<SourceDetail> {
  return request<SourceDetail>(`/sources/${encodeURIComponent(id)}`);
}

export function getQuestions(): Promise<QuestionRow[]> {
  return request<QuestionRow[]>("/questions");
}

export function getQuestion(id: string): Promise<QuestionDetail> {
  return request<QuestionDetail>(`/questions/${encodeURIComponent(id)}`);
}

export function resolveQuestion(
  question_id: string,
  outcome: Outcome,
  resolved_at?: string,
): Promise<ResolveResult> {
  const body: Record<string, string> = { question_id, outcome };
  if (resolved_at !== undefined) body["resolved_at"] = resolved_at;
  return request<ResolveResult>("/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function getRaw(kind: IngestKind, opts: RawQuery = {}): Promise<RawResult> {
  const q = new URLSearchParams();
  if (opts.question_id !== undefined) q.set("question_id", opts.question_id);
  if (opts.source_id !== undefined) q.set("source_id", opts.source_id);
  if (opts.limit !== undefined) q.set("limit", String(opts.limit));
  if (opts.offset !== undefined) q.set("offset", String(opts.offset));
  const qs = q.toString();
  return request<RawResult>(`/raw/${kind}${qs ? "?" + qs : ""}`);
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

// ── shared format helpers (terminal look: explicit +/−, never color) ─────

export const MINUS = "−"; // typographic minus, wider than hyphen

export function fmtProb(v: number | null | undefined, digits = 3): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

export function fmtSigned(v: number | null | undefined, digits = 3): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const sign = v < 0 ? MINUS : "+";
  return sign + Math.abs(v).toFixed(digits);
}

export function fmtAB(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return Math.round(v).toLocaleString("en-US");
}

// "MM-DD HH:MM" in UTC. Timestamps without an explicit zone are treated
// as UTC (the sidecar stores UTC ISO strings).
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const zoned = /Z$|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z";
  const d = new Date(zoned);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

// Mono block bar for credibility: ▮▮▮▮▮▮▯▯▯▯
export function credBar(cred: number, width = 10): string {
  const filled = Math.max(0, Math.min(width, Math.round(cred * width)));
  return "▮".repeat(filled) + "▯".repeat(width - filled);
}

// ── shared DOM helpers ───────────────────────────────────────────────────
// textContent only — never innerHTML with data, so ingested strings render
// verbatim without injection risk.

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  cls?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function sampleTag(): HTMLElement {
  return el("span", "nv-tag", "SAMPLE");
}

export function theadRow(labels: string[]): HTMLTableSectionElement {
  const thead = el("thead");
  const tr = el("tr");
  for (const label of labels) tr.appendChild(el("th", undefined, label));
  thead.appendChild(tr);
  return thead;
}

export function td(text: string, cls?: string): HTMLTableCellElement {
  return el("td", cls, text);
}
