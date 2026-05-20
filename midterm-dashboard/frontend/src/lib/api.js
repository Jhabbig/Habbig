const BASE = ''

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', ...options.headers },
    ...options,
  })
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent('auth:unauthorized'))
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Request failed')
  }
  return res.json()
}

export const api = {
  // Auth
  login: (email, password) => request('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  register: (email, password, display_name) => request('/auth/register', { method: 'POST', body: JSON.stringify({ email, password, display_name }) }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  me: () => request('/auth/me'),
  getSettings: () => request('/auth/settings'),
  updateSettings: (settings) => request('/auth/settings', { method: 'PUT', body: JSON.stringify(settings) }),

  // Public data
  overview: () => request('/data/overview'),
  races: (filters = {}) => {
    const q = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== undefined && v !== '' && v !== null)).toString()
    return request(`/data/races${q ? '?' + q : ''}`)
  },
  race: (key) => request(`/data/race/${key}`),
  raceCandidates: (key, refresh = false) => request(`/data/race/${key}/candidates${refresh ? '?refresh=true' : ''}`),
  history: (key) => request(`/data/history/${key}`),
  worldElections: (filters = {}) => {
    const q = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== undefined && v !== '' && v !== null)).toString()
    return request(`/data/world-elections${q ? '?' + q : ''}`)
  },
  raceContext: (raceKey) => request(`/data/race-context/${raceKey}`),
  raceContexts: () => request('/data/race-contexts'),
  districtProfile: (state) => request(`/data/district-profile/${state}`),
  districtProfiles: () => request('/data/district-profiles'),
  jurisdictionProfile: (type, code, refresh = false) => {
    const q = new URLSearchParams({ jurisdiction_type: type, jurisdiction_code: code, ...(refresh ? { refresh: 'true' } : {}) }).toString()
    return request(`/data/jurisdiction-profile?${q}`)
  },
  jurisdictionProfiles: (type) => {
    const q = type ? `?jurisdiction_type=${type}` : ''
    return request(`/data/jurisdiction-profiles${q}`)
  },
  historical: (filters = {}) => {
    const q = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== undefined && v !== '' && v !== null)).toString()
    return request(`/data/historical${q ? '?' + q : ''}`)
  },
  divergence: () => request('/data/divergence'),
  divergenceHistory: (key) => request(`/data/divergence/history/${key}`),
  sources: () => request('/data/sources'),
  polling: (key) => request(`/data/polling/${key}`),
  recentPolls: () => request('/data/polling/recent'),
  backtest: (sinceDays = 30) => request(`/data/backtest?since_days=${sinceDays}`),
  forecast: (key) => request(`/data/forecast/${encodeURIComponent(key)}`),
  forecasts: (filters = {}) => {
    const q = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== undefined && v !== '' && v !== null)).toString()
    return request(`/data/forecasts${q ? '?' + q : ''}`)
  },
  smartMoney: (key) => request(`/data/smart-money/${encodeURIComponent(key)}`),
  newsForRace: (key, limit = 20) => request(`/data/news/race/${encodeURIComponent(key)}?limit=${limit}`),
  newsRecent: (limit = 30) => request(`/data/news/recent?limit=${limit}`),
  newsLagCurve: (minDeltaPp = 1.0) => request(`/data/news/lag-curve?min_delta_pp=${minDeltaPp}`),
  electionNight: () => request('/data/election-night'),

  // Premium
  watchlist: () => request('/premium/watchlist'),
  addWatchlist: (key) => request(`/premium/watchlist/${key}`, { method: 'POST' }),
  removeWatchlist: (key) => request(`/premium/watchlist/${key}`, { method: 'DELETE' }),
  detailedComparison: (key) => request(`/premium/detailed-comparison/${key}`),

  // Admin
  adminStats: () => request('/admin/stats'),
  adminUsers: (limit = 100, offset = 0) => request(`/admin/users?limit=${limit}&offset=${offset}`),
  adminUser: (id) => request(`/admin/user/${id}`),
  adminSetTier: (id, tier) => request(`/admin/user/${id}/tier`, { method: 'PUT', body: JSON.stringify({ tier }) }),
  adminGrowth: () => request('/admin/growth'),
  adminChurn: () => request('/admin/churn'),
  adminAuditLog: () => request('/admin/audit-log'),
  adminDataStatus: () => request('/admin/data-status'),

  // Admin: human review of market matches
  flagMarket: (raceKey, source, sourceId, note = null) =>
    request(`/admin/race/${encodeURIComponent(raceKey)}/flag`, {
      method: 'POST',
      body: JSON.stringify({ source, source_id: sourceId, note }),
    }),
  unflagMarket: (raceKey, source, sourceId) =>
    request(`/admin/race/${encodeURIComponent(raceKey)}/flag/${encodeURIComponent(source)}/${encodeURIComponent(sourceId)}`, {
      method: 'DELETE',
    }),
  verifyRace: (raceKey, note = null) =>
    request(`/admin/race/${encodeURIComponent(raceKey)}/verify`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    }),
  unverifyRace: (raceKey) =>
    request(`/admin/race/${encodeURIComponent(raceKey)}/verify`, { method: 'DELETE' }),
}
