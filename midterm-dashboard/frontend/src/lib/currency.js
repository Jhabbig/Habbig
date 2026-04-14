// Currency conversion + formatting utilities.
//
// Source markets are USD-denominated. The user picks a target currency
// (default USD = no conversion). FX rates are fetched from the same-origin
// /api/fx-rates endpoint, which proxies frankfurter.dev with a 1h cache.
// Rates + timestamp are mirrored to localStorage so a stale value is
// available immediately on page load while we refetch in the background.

export const CURRENCIES = [
  { code: 'USD', name: 'US Dollar' },
  { code: 'EUR', name: 'Euro' },
  { code: 'GBP', name: 'British Pound' },
  { code: 'JPY', name: 'Japanese Yen' },
  { code: 'AUD', name: 'Australian Dollar' },
  { code: 'CAD', name: 'Canadian Dollar' },
  { code: 'CHF', name: 'Swiss Franc' },
  { code: 'CNY', name: 'Chinese Yuan' },
  { code: 'HKD', name: 'Hong Kong Dollar' },
  { code: 'NZD', name: 'New Zealand Dollar' },
  { code: 'SEK', name: 'Swedish Krona' },
  { code: 'KRW', name: 'South Korean Won' },
  { code: 'SGD', name: 'Singapore Dollar' },
  { code: 'NOK', name: 'Norwegian Krone' },
  { code: 'MXN', name: 'Mexican Peso' },
  { code: 'INR', name: 'Indian Rupee' },
  { code: 'ZAR', name: 'South African Rand' },
  { code: 'TRY', name: 'Turkish Lira' },
  { code: 'BRL', name: 'Brazilian Real' },
  { code: 'DKK', name: 'Danish Krone' },
  { code: 'PLN', name: 'Polish Zloty' },
  { code: 'THB', name: 'Thai Baht' },
  { code: 'IDR', name: 'Indonesian Rupiah' },
  { code: 'HUF', name: 'Hungarian Forint' },
  { code: 'CZK', name: 'Czech Koruna' },
  { code: 'ILS', name: 'Israeli Shekel' },
  { code: 'PHP', name: 'Philippine Peso' },
  { code: 'MYR', name: 'Malaysian Ringgit' },
  { code: 'RON', name: 'Romanian Leu' },
  { code: 'ISK', name: 'Icelandic Krona' },
]

const CACHE_KEY = 'narve_fx_rates'
const TTL_MS = 60 * 60 * 1000 // 1 hour

let _ratesPromise = null

function _readCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (parsed && parsed.rates && parsed.fetched_at) return parsed
  } catch {}
  return null
}

function _writeCache(rates) {
  try {
    localStorage.setItem(
      CACHE_KEY,
      JSON.stringify({ rates, fetched_at: Date.now() }),
    )
  } catch {}
}

async function _fetchRates() {
  try {
    const r = await fetch('/api/fx-rates', {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    })
    if (!r.ok) throw new Error('fx fetch failed')
    const data = await r.json()
    const rates = data.rates || {}
    rates.USD = 1.0
    _writeCache(rates)
    return rates
  } catch {
    const cached = _readCache()
    return cached?.rates || { USD: 1.0 }
  }
}

// Trigger a background refresh (idempotent). Returns a promise that resolves
// to the latest rates dictionary.
export function ensureRates() {
  const cached = _readCache()
  const isFresh = cached && Date.now() - cached.fetched_at < TTL_MS
  if (isFresh) return Promise.resolve(cached.rates)
  if (!_ratesPromise) {
    _ratesPromise = _fetchRates().finally(() => {
      _ratesPromise = null
    })
  }
  return _ratesPromise
}

// Synchronous lookup of the cached rate. If we have nothing yet, returns 1
// for USD and undefined for everything else.
export function getRate(code) {
  if (!code || code === 'USD') return 1
  const cached = _readCache()
  return cached?.rates?.[code]
}

// Convert a USD amount to the target currency. If we have no rate yet,
// returns the original USD amount as a fallback (better than NaN).
export function convertUSD(usdAmount, targetCode) {
  if (usdAmount == null || isNaN(usdAmount)) return null
  if (!targetCode || targetCode === 'USD') return Number(usdAmount)
  const rate = getRate(targetCode)
  if (rate == null) return Number(usdAmount)
  return Number(usdAmount) * rate
}

// Format a USD amount in the user's chosen currency, using their chosen
// locale (driven by the unit-system toggle, e.g. en-US vs de-DE).
export function formatUSDAs(usdAmount, targetCode, locale, options = {}) {
  if (usdAmount == null || isNaN(usdAmount)) return '\u2014'
  const code = targetCode || 'USD'
  const value = convertUSD(usdAmount, code)
  try {
    return new Intl.NumberFormat(locale || 'en-US', {
      style: 'currency',
      currency: code,
      maximumFractionDigits: options.maxFractionDigits ?? 0,
      minimumFractionDigits: options.minFractionDigits ?? 0,
    }).format(value)
  } catch {
    return `${code} ${Number(value).toLocaleString(locale || 'en-US')}`
  }
}

// Compact format: $1.2M / €1,2M / ¥1.5M / £1.1K, etc.
// We use K/M suffixes (rather than Intl's locale-specific "Mio." / "億")
// to keep widths predictable for tight layouts.
export function formatUSDAsCompact(usdAmount, targetCode, locale) {
  if (usdAmount == null || isNaN(usdAmount)) return null
  const code = targetCode || 'USD'
  const value = convertUSD(usdAmount, code)
  const loc = locale || 'en-US'
  let suffix = ''
  let scaled = value
  if (Math.abs(value) >= 1_000_000) {
    scaled = value / 1_000_000
    suffix = 'M'
  } else if (Math.abs(value) >= 1_000) {
    scaled = value / 1_000
    suffix = 'K'
  }
  // Format the scaled number as plain decimal in the locale, then position
  // the currency symbol where the locale puts it (prefix vs suffix). Drop
  // the literal value from the parts and substitute our scaled+suffix.
  try {
    const fmt = new Intl.NumberFormat(loc, {
      style: 'currency',
      currency: code,
      maximumFractionDigits: suffix ? 1 : 0,
      minimumFractionDigits: 0,
    })
    const numberStr = scaled.toLocaleString(loc, {
      maximumFractionDigits: suffix ? 1 : 0,
      minimumFractionDigits: 0,
    })
    const parts = fmt.formatToParts(0)
    const symPart = parts.find(p => p.type === 'currency')
    const sym = symPart ? symPart.value : code
    const symFirst = parts.findIndex(p => p.type === 'currency') < parts.findIndex(p => p.type === 'integer')
    return symFirst
      ? `${sym}${numberStr}${suffix}`
      : `${numberStr}${suffix} ${sym}`
  } catch {
    return `${code} ${scaled.toLocaleString(loc)}${suffix}`
  }
}
