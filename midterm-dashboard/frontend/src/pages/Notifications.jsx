import React, { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { pushSupported, pushStatus, subscribePush, unsubscribePush } from '../lib/push'
import { Bell, BellOff, Mail, Smartphone, History as HistoryIcon, AlertTriangle } from 'lucide-react'

export default function Notifications() {
  const [alerts, setAlerts] = useState([])
  const [history, setHistory] = useState([])
  const [channels, setChannels] = useState({ email: false, push: false })
  const [pushState, setPushState] = useState({ supported: false, subscribed: false, permission: 'default' })
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    Promise.all([
      api.alerts().catch(() => ({ alerts: [] })),
      api.alertHistory().catch(() => ({ history: [] })),
      api.pushConfig().catch(() => ({ channels: {} })),
      pushSupported() ? pushStatus() : Promise.resolve({ supported: false }),
    ]).then(([a, h, cfg, ps]) => {
      setAlerts(a.alerts || [])
      setHistory(h.history || [])
      setChannels(cfg.channels || { email: false, push: false })
      setPushState(ps)
    }).finally(() => setLoading(false))
  }, [])

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
    </div>
  )
}
