import React, { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { getCurrencySymbol, getLocale } from '../lib/settings'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { Users, DollarSign, TrendingUp, Activity, Shield, Search } from 'lucide-react'

function StatCard({ icon: Icon, label, value, sub, color = 'text-stone-900' }) {
  return (
    <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6">
      <div className="flex items-center gap-3">
        <div className="p-2 rounded-lg bg-stone-50"><Icon className={`h-5 w-5 ${color}`} /></div>
        <div>
          <div className="text-xs text-stone-400">{label}</div>
          <div className="text-xl font-bold text-stone-800">{value}</div>
          {sub && <div className="text-xs text-stone-400">{sub}</div>}
        </div>
      </div>
    </div>
  )
}

export default function AdminDashboard() {
  const [stats, setStats] = useState(null)
  const [users, setUsers] = useState([])
  const [growth, setGrowth] = useState([])
  const [churn, setChurn] = useState(null)
  const [auditLog, setAuditLog] = useState([])
  const [loading, setLoading] = useState(true)
  const [userSearch, setUserSearch] = useState('')

  useEffect(() => {
    Promise.all([
      api.adminStats().catch(() => null),
      api.adminUsers().catch(() => []),
      api.adminGrowth().catch(() => []),
      api.adminChurn().catch(() => null),
      api.adminAuditLog().catch(() => []),
    ]).then(([s, u, g, c, a]) => {
      setStats(s)
      setUsers(Array.isArray(u) ? u : u?.users || [])
      setGrowth(Array.isArray(g) ? g : g?.growth || [])
      setChurn(c)
      setAuditLog(Array.isArray(a) ? a : a?.logs || [])
    }).finally(() => setLoading(false))
  }, [])

  const filteredUsers = users.filter(u =>
    !userSearch || (u.email || '').toLowerCase().includes(userSearch.toLowerCase()) ||
    (u.display_name || '').toLowerCase().includes(userSearch.toLowerCase())
  )

  const handleTierChange = async (userId, newTier) => {
    try {
      await api.adminSetTier(userId, newTier)
      // Functional setter so concurrent edits don't clobber each other via a
      // stale `users` snapshot captured by the closure at render time.
      setUsers(prev => prev.map(u => u.id === userId ? { ...u, tier: newTier } : u))
    } catch (e) { alert('Failed: ' + e.message) }
  }

  if (loading) return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">{[1,2,3,4].map(i => <div key={i} className="bg-white shadow-sm border border-stone-100 rounded-xl animate-pulse h-20"></div>)}</div>
    </div>
  )

  return (
    <div>
      <h1 className="text-3xl font-semibold text-stone-800 mb-6 flex items-center gap-2"><Shield className="h-6 w-6 text-amber-600" /> Admin Dashboard</h1>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <StatCard icon={Users} label="Total Users" value={stats?.total_users || 0} color="text-stone-900" />
        <StatCard icon={DollarSign} label="Est. MRR" value={`${getCurrencySymbol()}${(stats?.estimated_mrr || 0).toLocaleString(getLocale(), { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`} sub={`${stats?.users_by_tier?.premium || 0} premium`} color="text-emerald-600" />
        <StatCard icon={TrendingUp} label="New (7d)" value={stats?.new_users_7d || 0} color="text-amber-600" />
        <StatCard icon={Activity} label="Active Sessions" value={stats?.active_sessions || 0} color="text-violet-600" />
      </div>

      <div className="grid lg:grid-cols-2 gap-6 mb-8">
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6">
          <h3 className="text-sm font-semibold text-stone-500 mb-4 uppercase tracking-wide">Users by Tier</h3>
          <div className="space-y-3">
            {Object.entries(stats?.users_by_tier || {}).map(([tier, count]) => (
              <div key={tier} className="flex items-center justify-between">
                <span className="capitalize text-sm text-stone-700">{tier}</span>
                <div className="flex items-center gap-3">
                  <div className="w-32 bg-stone-100 rounded-full h-2">
                    <div className="h-2 rounded-full transition-all"
                      style={{ width: `${(count / Math.max(stats?.total_users || 1, 1)) * 100}%`, backgroundColor: tier === 'premium' ? '#f59e0b' : tier === 'admin' ? '#8b5cf6' : '#a8a29e' }}></div>
                  </div>
                  <span className="font-bold text-sm text-stone-800 w-8 text-right">{count}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6">
          <h3 className="text-sm font-semibold text-stone-500 mb-4 uppercase tracking-wide">Churn</h3>
          {churn ? (
            <div className="space-y-3">
              <div className="flex justify-between"><span className="text-sm text-stone-500">Active Subscriptions</span><span className="font-bold text-stone-800">{churn.active_subs}</span></div>
              <div className="flex justify-between"><span className="text-sm text-stone-500">Recent Cancellations (30d)</span><span className="font-bold text-rose-500">{churn.recent_churn}</span></div>
              <div className="flex justify-between"><span className="text-sm text-stone-500">Churn Rate</span><span className={`font-bold ${churn.churn_rate_pct > 5 ? 'text-rose-500' : 'text-emerald-600'}`}>{churn.churn_rate_pct}%</span></div>
            </div>
          ) : <p className="text-stone-400 text-sm">No churn data.</p>}
        </div>
      </div>

      {growth.length > 0 && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-8">
          <h3 className="text-sm font-semibold text-stone-500 mb-4 uppercase tracking-wide">User Growth</h3>
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={growth}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e7e5e4" />
              <XAxis dataKey="day" tick={{ fill: '#78716c', fontSize: 12 }} />
              <YAxis tick={{ fill: '#78716c', fontSize: 12 }} />
              <Tooltip contentStyle={{ backgroundColor: '#ffffff', border: '1px solid #e7e5e4', borderRadius: '12px', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.05)' }} />
              <Bar dataKey="new_users" fill="#3b82f6" radius={[4, 4, 0, 0]} name="New Users" />
              <Bar dataKey="new_premium" fill="#f59e0b" radius={[4, 4, 0, 0]} name="New Premium" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-8">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-stone-500 uppercase tracking-wide">User Management</h3>
          <span className="text-xs text-stone-400">{filteredUsers.length} users</span>
        </div>
        <div className="relative mb-4">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-stone-400" />
          <input type="text" placeholder="Search users..." value={userSearch} onChange={e => setUserSearch(e.target.value)}
            className="w-full bg-white border border-stone-200 rounded-lg pl-10 pr-4 py-2 text-sm text-stone-800 focus:outline-none focus:ring-2 focus:ring-stone-900/10" />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-stone-400 text-xs uppercase border-b border-stone-200">
                <th className="text-left py-2 px-3">User</th>
                <th className="text-left py-2 px-3">Tier</th>
                <th className="text-left py-2 px-3">Status</th>
                <th className="text-left py-2 px-3">Joined</th>
                <th className="text-left py-2 px-3">Last Login</th>
                <th className="text-left py-2 px-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredUsers.slice(0, 50).map(u => (
                <tr key={u.id} className="border-b border-stone-100 hover:bg-stone-50">
                  <td className="py-2 px-3"><div className="font-medium text-stone-800">{u.display_name || '—'}</div><div className="text-xs text-stone-400">{u.email}</div></td>
                  <td className="py-2 px-3"><span className={`text-xs px-2 py-0.5 rounded-full ${u.tier === 'premium' ? 'badge-premium' : u.tier === 'admin' ? 'bg-violet-50 text-violet-600' : 'badge-free'}`}>{u.tier}</span></td>
                  <td className="py-2 px-3 text-xs text-stone-500">{u.subscription_status || 'none'}</td>
                  <td className="py-2 px-3 text-xs text-stone-400">{u.created_at?.split('T')[0]}</td>
                  <td className="py-2 px-3 text-xs text-stone-400">{u.last_login?.split('T')[0] || '—'}</td>
                  <td className="py-2 px-3">
                    <select value={u.tier} onChange={e => handleTierChange(u.id, e.target.value)}
                      className="bg-white border border-stone-200 rounded px-2 py-1 text-xs text-stone-700 focus:outline-none focus:ring-2 focus:ring-stone-900/10">
                      <option value="free">Free</option>
                      <option value="premium">Premium</option>
                      <option value="admin">Admin</option>
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-8">
        <h3 className="text-sm font-semibold text-stone-500 mb-4 uppercase tracking-wide">Data Pipeline</h3>
        <div className="grid grid-cols-3 gap-4">
          <div className="bg-stone-50 rounded-lg p-3 text-center"><div className="text-2xl font-bold text-stone-800">{stats?.active_markets || 0}</div><div className="text-xs text-stone-400">Active Markets</div></div>
          <div className="bg-stone-50 rounded-lg p-3 text-center"><div className="text-2xl font-bold text-stone-800">{stats?.price_snapshots || 0}</div><div className="text-xs text-stone-400">Price Snapshots</div></div>
          <div className="bg-stone-50 rounded-lg p-3 text-center"><div className="text-2xl font-bold text-stone-800">{stats?.divergence_snapshots || 0}</div><div className="text-xs text-stone-400">Divergence Snapshots</div></div>
        </div>
      </div>

      {auditLog.length > 0 && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6">
          <h3 className="text-sm font-semibold text-stone-500 mb-4 uppercase tracking-wide">Recent Audit Log</h3>
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {auditLog.slice(0, 20).map((log, i) => (
              <div key={i} className="flex items-center justify-between text-xs py-1.5 border-b border-stone-100">
                <div><span className="text-stone-500">{log.action}</span>{log.details && <span className="text-stone-400 ml-2">{log.details}</span>}</div>
                <span className="text-stone-400">{log.created_at?.replace('T', ' ').slice(0, 16)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
