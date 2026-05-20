import React, { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { pushSupported, pushStatus, subscribePush, unsubscribePush } from '../lib/push'
import { Bell, BellOff, Mail, Smartphone, History as HistoryIcon, AlertTriangle, Webhook, Key, Trash2, Copy, Rss, Send } from 'lucide-react'

export default function Notifications() {
  const [alerts, setAlerts] = useState([])
  const [history, setHistory] = useState([])
  const [channels, setChannels] = useState({ email: false, push: false })
  const [pushState, setPushState] = useState({ supported: false, subscribed: false, permission: 'default' })
  const [webhooks, setWebhooks] = useState([])
  const [apiKeys, setApiKeys] = useState([])
  const [digest, setDigest] = useState({ enabled: false, last_sent_at: null })
  const [newKey, setNewKey] = useState(null) // plaintext from POST, shown once
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  function reloadAll() {
    return Promise.all([
      api.alerts().catch(() => ({ alerts: [] })),
      api.alertHistory().catch(() => ({ history: [] })),
      api.pushConfig().catch(() => ({ channels: {} })),
      pushSupported() ? pushStatus() : Promise.resolve({ supported: false }),
      api.webhooks().catch(() => ({ webhooks: [] })),
      api.apiKeys().catch(() => ({ keys: [] })),
      api.digestStatus().catch(() => ({ enabled: false })),
    ]).then(([a, h, cfg, ps, wh, k, d]) => {
      setAlerts(a.alerts || [])
      setHistory(h.history || [])
      setChannels(cfg.channels || { email: false, push: false })
      setPushState(ps)
      setWebhooks(wh.webhooks || [])
      setApiKeys(k.keys || [])
      setDigest(d || { enabled: false })
    })
  }

  useEffect(() => { reloadAll().finally(() => setLoading(false)) }, [])

  async function addWebhook() {
    const url = window.prompt('Webhook URL (Slack/Discord/generic — must be https://):', 'https://hooks.slack.com/services/...')
    if (!url) return
    const fmt = window.prompt('Format: generic | slack | discord', 'slack') || 'generic'
    const thresh = parseFloat(window.prompt('Threshold (pp) — fire when a race moves at least this much', '5') || '5')
    setBusy(true); setError(null)
    try {
      await api.addWebhook({ url, format: fmt, threshold_pp: thresh })
      await reloadAll()
    } catch (e) { setError(e.message || 'Failed to add webhook') } finally { setBusy(false) }
  }

  async function removeWebhook(id) {
    if (!confirm('Remove this webhook?')) return
    try { await api.removeWebhook(id); await reloadAll() } catch (e) { setError(e.message) }
  }

  async function createKey() {
    const name = window.prompt('Key name (e.g. "production backend"):', '')
    if (name === null) return
    const tier = window.prompt('Tier: free | premium', 'premium') || 'free'
    try {
      const k = await api.createApiKey(name || null, tier)
      setNewKey(k.key)
      await reloadAll()
    } catch (e) { setError(e.message) }
  }

  async function revokeKey(id) {
    if (!confirm('Revoke this key? Existing integrations will stop working.')) return
    try { await api.revokeApiKey(id); await reloadAll() } catch (e) { setError(e.message) }
  }

  async function toggleDigest() {
    setBusy(true); setError(null)
    try {
      await api.digestSubscribe(!digest.enabled)
      const d = await api.digestStatus()
      setDigest(d)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function togglePush() {
    setBusy(true)
    setError(null)
    try {
      if (pushState.subscribed) await unsubscribePush()
      else await subscribePush()
      setPushState(await pushStatus())
    } catch (e) {
      setError(e.message || 'Push setup failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <div className="p-2 bg-amber-50 rounded-lg"><Bell className="h-6 w-6 text-amber-600" aria-hidden="true" /></div>
        <div>
          <h1 className="text-2xl font-bold text-stone-900">Notifications</h1>
          <p className="text-stone-500 text-sm">Manage your alerts and delivery channels.</p>
        </div>
      </div>

      {error && (
        <div role="alert" className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 mb-4 text-sm">{error}</div>
      )}

      <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 mb-6">
        <h2 className="text-sm font-semibold text-stone-800 mb-3">Delivery channels</h2>
        <div className="grid sm:grid-cols-2 gap-3">
          <div className="flex items-center justify-between p-3 border border-stone-100 rounded-lg">
            <div className="flex items-center gap-2">
              <Mail className="h-4 w-4 text-stone-500" aria-hidden="true" />
              <div>
                <div className="text-sm font-medium text-stone-800">Email</div>
                <div className="text-xs text-stone-400">{channels.email ? 'Configured' : 'Not configured on this server'}</div>
              </div>
            </div>
            <span className={`text-[10px] font-bold px-2 py-0.5 rounded ${channels.email ? 'bg-emerald-100 text-emerald-700' : 'bg-stone-100 text-stone-500'}`}>
              {channels.email ? 'ON' : 'OFF'}
            </span>
          </div>
          <div className="flex items-center justify-between p-3 border border-stone-100 rounded-lg">
            <div className="flex items-center gap-2">
              <Smartphone className="h-4 w-4 text-stone-500" aria-hidden="true" />
              <div>
                <div className="text-sm font-medium text-stone-800">Web push</div>
                <div className="text-xs text-stone-400">
                  {!pushState.supported ? 'Not supported in this browser'
                   : !channels.push ? 'Server has no VAPID key'
                   : pushState.subscribed ? 'Subscribed on this device' : 'Tap to subscribe'}
                </div>
              </div>
            </div>
            <button onClick={togglePush}
              disabled={busy || !pushState.supported || !channels.push}
              aria-pressed={pushState.subscribed}
              aria-label={pushState.subscribed ? 'Disable push notifications' : 'Enable push notifications'}
              className={`text-xs font-semibold px-3 py-1.5 rounded-md transition-colors ${
                pushState.subscribed ? 'bg-stone-100 text-stone-700 hover:bg-stone-200'
                : 'bg-stone-800 text-white hover:bg-stone-700 disabled:opacity-50'}`}>
              {busy ? '…' : pushState.subscribed ? 'Disable' : 'Enable'}
            </button>
          </div>
        </div>
        {!channels.email && !channels.push && (
          <div className="text-xs text-stone-400 mt-3 flex items-center gap-1">
            <AlertTriangle className="h-3 w-3" aria-hidden="true" />
            No delivery channels configured. Set SMTP_HOST or VAPID_PRIVATE_KEY on the server.
          </div>
        )}
      </section>

      <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 mb-6">
        <h2 className="text-sm font-semibold text-stone-800 mb-3">Active alerts</h2>
        {loading ? (
          <div role="status" aria-label="Loading alerts" className="text-stone-400 text-sm">Loading…</div>
        ) : alerts.length === 0 ? (
          <div className="text-sm text-stone-400">
            You don't have any alerts. Add one from a race detail page (click the bell icon).
          </div>
        ) : (
          <ul className="divide-y divide-stone-100">
            {alerts.map((a) => (
              <li key={a.id} className="py-2 flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium text-stone-800">{a.race_key}</div>
                  <div className="text-xs text-stone-500 capitalize">
                    {a.alert_type || 'divergence'} &middot; threshold {a.threshold}pp
                  </div>
                </div>
                <span className="text-[10px] bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded font-medium">enabled</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
        <h2 className="text-sm font-semibold text-stone-800 mb-3 flex items-center gap-1.5">
          <HistoryIcon className="h-4 w-4 text-stone-500" aria-hidden="true" />Recent alert deliveries
        </h2>
        {history.length === 0 ? (
          <div className="text-sm text-stone-400">No alerts have fired yet.</div>
        ) : (
          <ul className="divide-y divide-stone-100">
            {history.slice(0, 20).map((h) => (
              <li key={h.id} className="py-2">
                <div className="text-sm text-stone-800">{h.message}</div>
                <div className="text-[11px] text-stone-400">
                  {h.race_key} · {h.alert_type} · {(h.created_at || '').slice(0, 16).replace('T', ' ')}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Daily digest */}
      <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 mb-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-stone-800 mb-1 flex items-center gap-1.5">
              <Send className="h-4 w-4 text-stone-500" aria-hidden="true" />Daily digest email
            </h2>
            <p className="text-xs text-stone-500">
              Once-a-day summary of the biggest movers across all races plus anything from your watchlist that moved.
              {digest.last_sent_at && <span> Last sent: {digest.last_sent_at.slice(0, 16).replace('T', ' ')}.</span>}
            </p>
          </div>
          <button onClick={toggleDigest} disabled={busy}
            aria-pressed={digest.enabled}
            className={`text-xs font-semibold px-3 py-1.5 rounded-md transition-colors shrink-0 ${
              digest.enabled ? 'bg-stone-100 text-stone-700 hover:bg-stone-200'
              : 'bg-stone-800 text-white hover:bg-stone-700 disabled:opacity-50'}`}>
            {digest.enabled ? 'Unsubscribe' : 'Subscribe'}
          </button>
        </div>
      </section>

      {/* RSS feed */}
      <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 mb-6">
        <h2 className="text-sm font-semibold text-stone-800 mb-2 flex items-center gap-1.5">
          <Rss className="h-4 w-4 text-amber-600" aria-hidden="true" />Public RSS feed
        </h2>
        <p className="text-xs text-stone-500 mb-2">
          Subscribe in any RSS reader (Feedly, NetNewsWire, Substack) to get the biggest movers in your reader.
        </p>
        <div className="flex items-center gap-2">
          <code className="flex-1 bg-stone-50 border border-stone-200 rounded px-2 py-1 text-xs text-stone-700 font-mono overflow-x-auto">
            {typeof window !== 'undefined' ? `${window.location.origin}/feed/movements.xml` : '/feed/movements.xml'}
          </code>
          <a href="/feed/movements.xml" target="_blank" rel="noopener noreferrer"
            className="text-xs font-semibold px-3 py-1.5 rounded-md bg-stone-100 text-stone-700 hover:bg-stone-200">
            Open
          </a>
        </div>
      </section>

      {/* Outbound webhooks */}
      <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-stone-800 flex items-center gap-1.5">
            <Webhook className="h-4 w-4 text-stone-500" aria-hidden="true" />Outbound webhooks
          </h2>
          <button onClick={addWebhook} disabled={busy}
            className="text-xs font-semibold px-3 py-1.5 rounded-md bg-stone-800 text-white hover:bg-stone-700 disabled:opacity-50">
            + Add
          </button>
        </div>
        <p className="text-xs text-stone-500 mb-3">
          POST to Slack/Discord/generic webhooks when a race moves past a threshold. Same dedup as inline alerts.
        </p>
        {webhooks.length === 0 ? (
          <div className="text-sm text-stone-400">No webhooks configured.</div>
        ) : (
          <ul className="divide-y divide-stone-100">
            {webhooks.map(w => (
              <li key={w.id} className="py-2 flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-mono text-stone-700 truncate" title={w.url}>{w.url}</div>
                  <div className="text-[11px] text-stone-500 mt-0.5">
                    <span className="capitalize">{w.format}</span> · threshold {w.threshold_pp}pp
                    {w.last_status === 'error' && <span className="text-red-600 ml-1">· error: {w.last_error || 'unknown'}</span>}
                    {w.last_fired_at && <span className="text-stone-400 ml-1">· last fire {w.last_fired_at.slice(0, 16).replace('T', ' ')}</span>}
                  </div>
                </div>
                <button onClick={() => removeWebhook(w.id)} aria-label="Remove webhook"
                  className="text-stone-400 hover:text-red-600 shrink-0">
                  <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* API keys */}
      <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-stone-800 flex items-center gap-1.5">
            <Key className="h-4 w-4 text-stone-500" aria-hidden="true" />API keys
          </h2>
          <button onClick={createKey} disabled={busy}
            className="text-xs font-semibold px-3 py-1.5 rounded-md bg-stone-800 text-white hover:bg-stone-700 disabled:opacity-50">
            + Generate
          </button>
        </div>
        <p className="text-xs text-stone-500 mb-3">
          Bearer tokens for the <code className="bg-stone-100 px-1 rounded text-[11px]">/v1/api/*</code> endpoints. Premium tier unlocks history + movements feed.
        </p>
        {newKey && (
          <div role="alert" className="mb-3 p-3 bg-amber-50 border border-amber-200 rounded-lg">
            <div className="text-xs font-semibold text-amber-800 mb-1">Save this key now — it won't be shown again.</div>
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-white border border-amber-300 rounded px-2 py-1 text-xs font-mono text-amber-900 overflow-x-auto">{newKey}</code>
              <button onClick={() => { navigator.clipboard?.writeText(newKey); setNewKey(null) }}
                aria-label="Copy key" className="text-xs font-semibold px-2 py-1 rounded bg-amber-700 text-white hover:bg-amber-800">
                <Copy className="h-3 w-3" aria-hidden="true" />
              </button>
            </div>
          </div>
        )}
        {apiKeys.length === 0 ? (
          <div className="text-sm text-stone-400">No API keys yet.</div>
        ) : (
          <ul className="divide-y divide-stone-100">
            {apiKeys.map(k => (
              <li key={k.id} className="py-2 flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-mono text-stone-700">{k.key_prefix}…</div>
                  <div className="text-[11px] text-stone-500 mt-0.5">
                    {k.name || '(unnamed)'} · {k.tier} · {k.rate_limit_rpm} req/min
                    {k.revoked_at && <span className="text-red-600 ml-1">· revoked</span>}
                    {k.last_used_at && <span className="text-stone-400 ml-1">· last used {k.last_used_at.slice(0, 16).replace('T', ' ')}</span>}
                  </div>
                </div>
                {!k.revoked_at && (
                  <button onClick={() => revokeKey(k.id)} aria-label="Revoke key"
                    className="text-stone-400 hover:text-red-600 shrink-0">
                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
