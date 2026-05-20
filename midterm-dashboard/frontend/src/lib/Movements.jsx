import React, { useState, useEffect } from 'react'
import { api } from './api'
import { TrendingUp, TrendingDown, Newspaper, AlertCircle, ExternalLink, Sparkles, Info } from 'lucide-react'

const SOURCE_COLORS = { polymarket: '#8b5cf6', kalshi: '#3b82f6', predictit: '#f59e0b', polling: '#10b981' }

const CONFIDENCE_STYLES = {
  high: 'bg-emerald-100 text-emerald-700 border-emerald-200',
  medium: 'bg-amber-100 text-amber-700 border-amber-200',
  low: 'bg-stone-100 text-stone-600 border-stone-200',
}

const EMPTY_REASONS = {
  no_relevant_news_found: 'No news in the window plausibly explains this movement.',
  insufficient_movement: 'Movement is within normal noise — no explanation needed.',
  source_disagreement: 'Sources disagree on direction; news did not resolve which is correct.',
  timing_mismatch: 'Candidate articles were published outside the causal window.',
}

function hostname(url) {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url }
}

export default function Movements({ raceKey, hours = 24 }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!raceKey) return
    setLoading(true)
    api.movements(raceKey, hours)
      .then(setData)
      .catch(e => setError(e.message || 'Failed to load'))
      .finally(() => setLoading(false))
  }, [raceKey, hours])

  if (loading || error || !data) return null
  const { movements = [], explanation = {}, cached, articles = [] } = data
  if (!movements.length && !explanation.explanations?.length && !explanation.summary) return null

  return (
    <section aria-labelledby="why-moved-heading"
      className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 sm:p-6 mb-6">
      <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
        <h3 id="why-moved-heading" className="text-lg font-semibold text-stone-800 flex items-center gap-2">
          <TrendingUp className="h-5 w-5 text-stone-500" aria-hidden="true" />
          Why did this move? <span className="text-xs font-normal text-stone-400">last {hours}h</span>
        </h3>
        {cached && (
          <span className="text-[10px] bg-stone-100 text-stone-500 px-1.5 py-0.5 rounded font-medium">cached</span>
        )}
      </div>

      {/* Per-source deltas */}
      {movements.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-4">
          {movements.map(m => {
            const isUp = m.delta_pp >= 0
            const color = SOURCE_COLORS[m.source] || '#78716c'
            return (
              <div key={m.source} className="flex items-center justify-between p-2.5 border border-stone-100 rounded-lg">
                <span className="text-xs font-bold uppercase tracking-wide" style={{ color }}>{m.source}</span>
                <div className="flex items-center gap-1 text-sm">
                  <span className="tabular-nums text-stone-500">{(m.from * 100).toFixed(0)}%</span>
                  <span className="text-stone-300">→</span>
                  <span className="tabular-nums font-semibold text-stone-800">{(m.to * 100).toFixed(0)}%</span>
                  <span className={`tabular-nums font-bold ml-2 inline-flex items-center gap-0.5 ${isUp ? 'text-emerald-600' : 'text-red-600'}`}>
                    {isUp ? <TrendingUp className="h-3 w-3" aria-hidden="true" /> : <TrendingDown className="h-3 w-3" aria-hidden="true" />}
                    {isUp ? '+' : ''}{m.delta_pp.toFixed(1)}pp
                  </span>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* LLM summary */}
      {explanation.summary && (
        <div className="mb-3 p-3 bg-stone-50 border border-stone-100 rounded-lg flex items-start gap-2">
          <Sparkles className="h-3.5 w-3.5 text-stone-500 shrink-0 mt-0.5" aria-hidden="true" />
          <p className="text-sm text-stone-700 leading-relaxed">{explanation.summary}</p>
        </div>
      )}

      {/* Empty-result message — explicit so users see why no citations */}
      {explanation.reason_if_empty && (
        <div className="mb-3 p-3 bg-stone-50 border border-stone-100 rounded-lg flex items-start gap-2 text-xs text-stone-600">
          <Info className="h-3.5 w-3.5 text-stone-400 shrink-0 mt-0.5" aria-hidden="true" />
          <span>{EMPTY_REASONS[explanation.reason_if_empty] || 'No grounded explanation available.'}</span>
        </div>
      )}

      {/* Cited articles */}
      {explanation.explanations?.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1">
            <Newspaper className="h-3 w-3" aria-hidden="true" />
            Cited articles ({explanation.explanations.length})
          </div>
          {explanation.explanations.map((exp, i) => (
            <article key={i} className="border border-stone-100 rounded-lg p-3 hover:bg-stone-50 transition-colors">
              <div className="flex items-start justify-between gap-2 mb-1.5">
                <a href={exp.url} target="_blank" rel="noopener noreferrer"
                  className="text-sm font-medium text-stone-800 hover:text-stone-600 hover:underline inline-flex items-start gap-1.5 leading-tight">
                  <span>{exp.headline || 'Untitled article'}</span>
                  <ExternalLink className="h-3 w-3 mt-0.5 text-stone-400 shrink-0" aria-hidden="true" />
                </a>
                <span className={`text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded border shrink-0 ${CONFIDENCE_STYLES[exp.confidence] || CONFIDENCE_STYLES.low}`}>
                  {exp.confidence}
                </span>
              </div>
              <div className="text-[11px] text-stone-400 mb-1.5">{hostname(exp.url || '')}</div>
              {exp.quote && (
                <blockquote className="text-xs text-stone-600 italic border-l-2 border-stone-200 pl-2 mb-1.5">
                  &ldquo;{exp.quote}&rdquo;
                </blockquote>
              )}
              {exp.rationale && (
                <p className="text-xs text-stone-500 leading-relaxed">{exp.rationale}</p>
              )}
            </article>
          ))}
        </div>
      )}

      {/* Show raw article count when LLM had inputs but didn't cite */}
      {articles.length > 0 && !explanation.explanations?.length && (
        <div className="text-[11px] text-stone-400 mt-2">
          {articles.length} candidate article{articles.length !== 1 ? 's' : ''} examined, none cited as causal.
        </div>
      )}

      {/* Disabled / not configured */}
      {explanation.configured === false && (
        <div className="text-xs text-stone-500 bg-stone-50 border border-stone-100 rounded-lg p-3 flex items-start gap-2">
          <AlertCircle className="h-3.5 w-3.5 text-stone-400 shrink-0 mt-0.5" aria-hidden="true" />
          <span>
            Set <code className="bg-stone-100 px-1 rounded font-mono">ANTHROPIC_API_KEY</code> on the server
            to enable grounded LLM explanations. News fetching also benefits from
            {' '}<code className="bg-stone-100 px-1 rounded font-mono">NEWS_API_KEY</code> (GDELT is the free fallback).
          </span>
        </div>
      )}
    </section>
  )
}
