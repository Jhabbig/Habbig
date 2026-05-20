// Standalone tests for Kelly + EV math. Run with: node src/lib/portfolio_test.mjs
//
// We re-implement the helpers here to avoid hauling in React/JSX into a
// Node test. The originals live in pages/Portfolio.jsx — keep these in sync.

function kellyFraction(estimatedProb, entryPrice) {
  if (entryPrice <= 0 || entryPrice >= 1) return 0
  const p = Math.max(0, Math.min(1, estimatedProb))
  const q = 1 - p
  const b = (1 - entryPrice) / entryPrice
  if (b <= 0) return 0
  const f = (p * b - q) / b
  return Math.max(0, Math.min(0.25, f))
}

function expectedValue(estimatedProb, entryPrice, side = 'yes') {
  if (side === 'yes') return estimatedProb * (1 - entryPrice) - (1 - estimatedProb) * entryPrice
  return (1 - estimatedProb) * entryPrice - estimatedProb * (1 - entryPrice)
}

function approx(a, b, eps = 0.001) { return Math.abs(a - b) < eps }
function assert(cond, label) {
  if (!cond) { console.log(`FAIL ${label}`); process.exit(1) }
  console.log(`PASS ${label}`)
}

// At fair odds (p == price), Kelly = 0
assert(approx(kellyFraction(0.5, 0.5), 0), 'kelly: zero at fair odds')

// Negative edge (p < price) returns 0 (clamped)
assert(kellyFraction(0.3, 0.5) === 0, 'kelly: negative edge clamps to 0')

// Positive edge: p=0.6, price=0.5 -> b=1, q=0.4, f=(0.6 - 0.4)/1 = 0.2
assert(approx(kellyFraction(0.6, 0.5), 0.2), 'kelly: known formula at p=0.6, price=0.5')

// Huge edge: caps at 25%
assert(approx(kellyFraction(0.99, 0.5), 0.25), 'kelly: cap at 0.25')

// Invalid price boundaries
assert(kellyFraction(0.5, 0) === 0, 'kelly: price=0 -> 0')
assert(kellyFraction(0.5, 1) === 0, 'kelly: price=1 -> 0')
assert(kellyFraction(0.5, -0.1) === 0, 'kelly: price<0 -> 0')

// EV for yes side at fair odds is 0
assert(approx(expectedValue(0.5, 0.5, 'yes'), 0), 'ev: zero at fair odds (yes)')

// EV for no side at fair odds is 0
assert(approx(expectedValue(0.5, 0.5, 'no'), 0), 'ev: zero at fair odds (no)')

// EV for yes at p=0.6, price=0.5 -> 0.6 * 0.5 - 0.4 * 0.5 = 0.1
assert(approx(expectedValue(0.6, 0.5, 'yes'), 0.1), 'ev: yes at p=0.6, price=0.5')

// EV for no at p=0.6, price=0.5 -> -0.1 (mirror of yes)
assert(approx(expectedValue(0.6, 0.5, 'no'), -0.1), 'ev: no is mirror of yes')

// Negative EV when betting yes against the price
assert(expectedValue(0.4, 0.5, 'yes') < 0, 'ev: negative when betting against the price')

console.log('\nAll portfolio math tests passed.')
