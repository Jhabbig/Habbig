import React from 'react'

// Standard tile-grid layout for the US. Each entry is [col, row] in a 12×8
// grid where (0, 0) is top-left. This is the same general arrangement
// 538 / NPR use — recognisable at a glance without needing GeoJSON polygons,
// and it scales to any container size as an SVG.
const TILES = {
  AK: [0, 0], ME: [11, 0],
  VT: [10, 1], NH: [11, 1],
  WA: [1, 1], ID: [2, 1], MT: [3, 1], ND: [4, 1], MN: [5, 1], WI: [6, 1], MI: [8, 1],  // MI upper
  OR: [1, 2], UT: [2, 2], WY: [3, 2], SD: [4, 2], IA: [5, 2], IL: [6, 2], IN: [7, 2], OH: [8, 2], PA: [9, 2], NY: [10, 2], MA: [11, 2],
  CA: [1, 3], NV: [2, 3], CO: [3, 3], NE: [4, 3], MO: [5, 3], KY: [6, 3], WV: [7, 3], VA: [8, 3], NJ: [9, 3], CT: [10, 3], RI: [11, 3],
  AZ: [2, 4], NM: [3, 4], KS: [4, 4], AR: [5, 4], TN: [6, 4], NC: [7, 4], SC: [8, 4], DC: [9, 4], DE: [10, 4],
  HI: [0, 5], TX: [3, 5], OK: [4, 5], LA: [5, 5], MS: [6, 5], AL: [7, 5], GA: [8, 5], MD: [9, 5],
  FL: [9, 6],
}

const CALL_FILL = {
  called_d: { fill: '#2563eb', text: '#ffffff', stroke: '#1d4ed8' },
  called_r: { fill: '#dc2626', text: '#ffffff', stroke: '#b91c1c' },
  lean_d:   { fill: '#93c5fd', text: '#1e3a8a', stroke: '#60a5fa' },
  lean_r:   { fill: '#fca5a5', text: '#7f1d1d', stroke: '#f87171' },
  tossup:   { fill: '#fbbf24', text: '#78350f', stroke: '#f59e0b' },
  unknown:  { fill: '#e7e5e4', text: '#78716c', stroke: '#d6d3d1' },
}

// Render the US tile grid colored by call state. Optional ``onHover`` and
// ``onClick`` callbacks fire with the race row (or null for clear). When a
// race is "conditioned" externally, the parent should pass the modified
// race rows and we re-render with the new colours.
export default function StateGridMap({
  racesByState = {},       // { TX: race, CA: race, ... }
  onHover,
  onClick,
  selectedState = null,
  conditionedState = null,
  height = 320,
}) {
  const tileSize = 56
  const tileGap = 4
  const cols = 12
  const rows = 8
  const totalWidth = cols * (tileSize + tileGap)
  const totalHeight = rows * (tileSize + tileGap)

  return (
    <div className="w-full" style={{ minHeight: height }}>
      <svg
        viewBox={`0 0 ${totalWidth} ${totalHeight}`}
        preserveAspectRatio="xMidYMid meet"
        className="w-full"
        style={{ maxHeight: '70vh' }}
      >
        {Object.entries(TILES).map(([state, [col, row]]) => {
          const race = racesByState[state]
          const callState = race?.call_state || 'unknown'
          const style = CALL_FILL[callState] || CALL_FILL.unknown
          const x = col * (tileSize + tileGap)
          const y = row * (tileSize + tileGap)
          const isSelected = state === selectedState
          const isConditioned = state === conditionedState

          // For conditional view, show delta as a small badge — positive
          // = market moved D, negative = moved R.
          const delta = race?.delta_pp
          const showDelta = delta != null && Math.abs(delta) >= 0.5 && !isConditioned

          return (
            <g
              key={state}
              transform={`translate(${x}, ${y})`}
              onMouseEnter={race ? () => onHover?.(race) : undefined}
              onMouseLeave={() => onHover?.(null)}
              onClick={race ? () => onClick?.(race) : undefined}
              style={{ cursor: race ? 'pointer' : 'default' }}
            >
              <rect
                width={tileSize}
                height={tileSize}
                rx={6}
                ry={6}
                fill={style.fill}
                stroke={isSelected || isConditioned ? '#0f172a' : style.stroke}
                strokeWidth={isSelected || isConditioned ? 3 : 1}
                opacity={race ? 1 : 0.4}
              />
              <text
                x={tileSize / 2}
                y={tileSize / 2 + 4}
                textAnchor="middle"
                fontSize={tileSize * 0.38}
                fontWeight={700}
                fill={style.text}
                style={{ pointerEvents: 'none', fontFamily: 'system-ui, sans-serif' }}
              >
                {state}
              </text>
              {race?.forecast_d != null && (
                <text
                  x={tileSize / 2}
                  y={tileSize - 8}
                  textAnchor="middle"
                  fontSize={tileSize * 0.20}
                  fill={style.text}
                  opacity={0.85}
                  style={{ pointerEvents: 'none', fontFamily: 'system-ui, sans-serif' }}
                >
                  {Math.round((race.forecast_d >= 0.5 ? race.forecast_d : 1 - race.forecast_d) * 100)}%
                </text>
              )}
              {showDelta && (
                <g transform={`translate(${tileSize - 4}, 12)`}>
                  <rect x={-22} y={-9} width={22} height={14} rx={3}
                        fill={delta > 0 ? '#1d4ed8' : '#b91c1c'} />
                  <text x={-11} y={1} textAnchor="middle" fontSize={9}
                        fill="#fff" fontWeight={700}
                        style={{ pointerEvents: 'none', fontFamily: 'system-ui, sans-serif' }}>
                    {delta > 0 ? '+' : ''}{delta.toFixed(1)}
                  </text>
                </g>
              )}
              {isConditioned && (
                <g transform={`translate(${tileSize - 6}, ${tileSize - 6})`}>
                  <circle r={5} fill="#fde047" stroke="#0f172a" strokeWidth={1} />
                </g>
              )}
            </g>
          )
        })}
      </svg>
      <div className="flex flex-wrap items-center gap-3 mt-3 text-xs">
        {Object.entries(CALL_FILL).filter(([k]) => k !== 'unknown').map(([k, s]) => (
          <div key={k} className="flex items-center gap-1.5">
            <span className="w-3 h-3 rounded" style={{ background: s.fill }} />
            <span className="text-stone-600">
              {k === 'called_d' ? 'Called D' : k === 'called_r' ? 'Called R' :
               k === 'lean_d' ? 'Lean D' : k === 'lean_r' ? 'Lean R' : 'Tossup'}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
