# midterm-dashboard/frontend/src/pages/ — Route components

One component per route. Mounted from `App.jsx`. Each page is self-contained
— it imports `api` from `../lib/api`, fetches what it needs in a `useEffect`,
and renders.

## Files in this directory

| File | Route | Purpose |
|---|---|---|
| `Dashboard.jsx` | `/` | Landing view — top movers, race summary cards, divergence highlights. |
| `Races.jsx` | `/races` | Filterable/sortable list of every Senate, House, and gubernatorial race. |
| `RaceDetail.jsx` | `/race/:id` | Single-race deep dive — current odds across markets, polling, candidates, history, divergence chart. |
| `Divergence.jsx` | `/divergence` | Cross-market divergence view — pairs of markets disagreeing about the same race. |
| `Historical.jsx` | `/historical` | Historical results comparison — current odds vs `historical_results.py` baselines. |
| `WorldElections.jsx` | `/world` | Global elections sidebar — non-US races tracked for context. |
| `AdminDashboard.jsx` | `/admin` | Admin-only panel — user management, manual data refresh, error logs. |
| `Account.jsx` | `/account` | Subscription / billing settings (the gateway handles real billing — this is the dashboard-side view). |
| `Settings.jsx` | `/settings` | User preferences (unit system, theme). Reads/writes via `useSettings()`. |
| `Login.jsx` | `/login` | Login form. POSTs to the gateway via `api.login()`. |
| `Register.jsx` | `/register` | Signup form. Disabled in production unless self-serve registration is enabled gateway-side. |
