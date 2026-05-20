// Default to same-origin (frontend served by FastAPI in production). Override
// with VITE_API_URL when the frontend is hosted separately from the backend.
const BASE = (import.meta.env?.VITE_API_URL ?? '').replace(/\/$/, '')

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
  comparison: (filters = {}) => {
    const q = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== undefined && v !== '' && v !== null)).toString()
    return request(`/data/comparison${q ? '?' + q : ''}`)
  },
  // CSV export — returns the URL the browser should hit (download endpoint
  // sets Content-Disposition so the browser saves the file).
  exportRacesCsvUrl: (filters = {}) => {
    const q = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== undefined && v !== '' && v !== null)).toString()
    return `${BASE}/data/export/races.csv${q ? '?' + q : ''}`
  },

  // Premium
  watchlist: () => request('/premium/watchlist'),
  addWatchlist: (key) => request(`/premium/watchlist/${key}`, { method: 'POST' }),
  removeWatchlist: (key) => request(`/premium/watchlist/${key}`, { method: 'DELETE' }),
  detailedComparison: (key) => request(`/premium/detailed-comparison/${key}`),

  // Alerts
  alerts: () => request('/premium/alerts'),
  createAlert: (race_key, threshold, alert_type = 'divergence') =>
    request('/premium/alerts', { method: 'POST', body: JSON.stringify({ race_key, threshold, alert_type }) }),
  alertHistory: (limit = 50) => request(`/premium/alerts/history?limit=${limit}`),

  // Push
  pushConfig: () => request('/data/push/public-key'),
  pushSubscribe: (endpoint, keys) =>
    request('/premium/push/subscribe', { method: 'POST', body: JSON.stringify({ endpoint, keys }) }),
  pushUnsubscribe: (endpoint, keys) =>
    request('/premium/push/unsubscribe', { method: 'POST', body: JSON.stringify({ endpoint, keys }) }),

  // Comments
  comments: (raceKey) => request(`/data/race/${encodeURIComponent(raceKey)}/comments`),
  postComment: (raceKey, body) =>
    request(`/premium/race/${encodeURIComponent(raceKey)}/comments`, { method: 'POST', body: JSON.stringify({ body }) }),
  deleteComment: (id) => request(`/premium/comments/${id}`, { method: 'DELETE' }),

  // Paper portfolio
  portfolio: (openOnly = false) => request(`/premium/portfolio${openOnly ? '?open_only=true' : ''}`),
  openPosition: (p) => request('/premium/portfolio', { method: 'POST', body: JSON.stringify(p) }),
  closePosition: (id, exit_price) => request(`/premium/portfolio/${id}?exit_price=${exit_price}`, { method: 'DELETE' }),

  // Accuracy + movements
  accuracy: () => request('/data/accuracy'),
  accuracyBadge: (source, raceType) => {
    const q = raceType ? `?race_type=${encodeURIComponent(raceType)}` : ''
    return request(`/data/accuracy/badge/${encodeURIComponent(source)}${q}`)
  },
  movements: (raceKey, hours = 24) => request(`/data/race/${encodeURIComponent(raceKey)}/movements?hours=${hours}`),

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
