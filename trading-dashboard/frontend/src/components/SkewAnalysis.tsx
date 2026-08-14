import React, { useMemo } from 'react';
import { AlertTriangle, TrendingDown, TrendingUp } from 'lucide-react';

interface SkewAnalysisProps {
  spotPrice: number;
  expirationDays?: number;
}

export const SkewAnalysis: React.FC<SkewAnalysisProps> = ({ spotPrice, expirationDays = 30 }) => {
  // Generate skew data
  const skewData = useMemo(() => {
    const strikes = Array.from({ length: 15 }, (_, i) => spotPrice - 70 + i * 10);

    return strikes.map((strike) => {
      // Simplified skew calculation
      const moneyness = strike / spotPrice;
      const baseIV = 0.25;

      // Put skew: OTM puts have higher IV (tail risk premium)
      const putSkew = Math.exp(-Math.pow((moneyness - 1) * 2, 2) / 2) * 0.15;
      const putIV = baseIV + putSkew * (1 - Math.abs(moneyness - 1));

      // Call skew: OTM calls have lower IV
      const callSkew = Math.exp(-Math.pow((moneyness - 1) * 2, 2) / 2) * 0.08;
      const callIV = baseIV - callSkew * Math.max(0, 1 - moneyness);

      const skew = putIV - callIV;

      return {
        strike,
        moneyness,
        callIV: Math.max(0.05, callIV),
        putIV: Math.max(0.05, putIV),
        skew,
      };
    });
  }, [spotPrice]);

  // Calculate metrics
  const metrics = useMemo(() => {
    const skews = skewData.map((d) => d.skew);
    const avgSkew = skews.reduce((a, b) => a + b, 0) / skews.length;
    const putSkewOTM = skewData.filter((d) => d.strike < spotPrice).slice(-2).map((d) => d.skew);
    const avgPutSkew = putSkewOTM.reduce((a, b) => a + b, 0) / putSkewOTM.length;

    return {
      avgSkew,
      avgPutSkew,
      trend: avgSkew > 0.05 ? 'put_skew' : 'neutral',
      riskSentiment: avgPutSkew > 0.08 ? 'bearish' : 'neutral',
    };
  }, [skewData, spotPrice]);

  // SVG chart
  const width = 700;
  const height = 350;
  const padding = { top: 30, right: 30, bottom: 60, left: 60 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;

  const minStrike = Math.min(...skewData.map((d) => d.strike));
  const maxStrike = Math.max(...skewData.map((d) => d.strike));
  const minIV = 0.05;
  const maxIV = 0.4;

  const xScale = (strike: number) => padding.left + ((strike - minStrike) / (maxStrike - minStrike)) * chartWidth;
  const yScale = (iv: number) => padding.top + chartHeight - ((iv - minIV) / (maxIV - minIV)) * chartHeight;

  const callPoints = skewData.map((d) => `${xScale(d.strike)},${yScale(d.callIV)}`).join(' ');
  const putPoints = skewData.map((d) => `${xScale(d.strike)},${yScale(d.putIV)}`).join(' ');

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold text-gray-100 mb-3">Volatility Skew Analysis</h3>
        <p className="text-sm text-gray-400 mb-4">Put/Call IV imbalance reveals market risk sentiment</p>
      </div>

      {/* SVG Chart */}
      <div className="bg-gray-900 p-4 rounded-lg border border-gray-700 overflow-x-auto">
        <svg width={width} height={height} className="bg-gray-800 rounded">
          {/* Grid lines */}
          {Array.from({ length: 5 }).map((_, i) => (
            <React.Fragment key={`grid-${i}`}>
              <line
                x1={padding.left}
                y1={padding.top + (chartHeight / 4) * i}
                x2={width - padding.right}
                y2={padding.top + (chartHeight / 4) * i}
                stroke="#444"
                strokeWidth="1"
                strokeDasharray="4"
              />
            </React.Fragment>
          ))}

          {/* ATM line */}
          <line
            x1={xScale(spotPrice)}
            y1={padding.top}
            x2={xScale(spotPrice)}
            y2={height - padding.bottom}
            stroke="#666"
            strokeWidth="2"
            strokeDasharray="8"
          />

          {/* Axes */}
          <line
            x1={padding.left}
            y1={padding.top}
            x2={padding.left}
            y2={height - padding.bottom}
            stroke="#666"
            strokeWidth="2"
          />
          <line
            x1={padding.left}
            y1={height - padding.bottom}
            x2={width - padding.right}
            y2={height - padding.bottom}
            stroke="#666"
            strokeWidth="2"
          />

          {/* Call IV line */}
          <polyline points={callPoints} fill="none" stroke="#3b82f6" strokeWidth="2.5" opacity="0.8" />

          {/* Put IV line */}
          <polyline points={putPoints} fill="none" stroke="#ef4444" strokeWidth="2.5" opacity="0.8" />

          {/* Skew area (put higher than call) */}
          <defs>
            <linearGradient id="skewGrad" x1="0%" y1="0%" x2="0%" y2="100%">
              <stop offset="0%" stopColor="rgba(239, 68, 68, 0.2)" />
              <stop offset="100%" stopColor="rgba(239, 68, 68, 0)" />
            </linearGradient>
          </defs>

          {/* Data points */}
          {skewData.map((d) => (
            <React.Fragment key={d.strike}>
              <circle cx={xScale(d.strike)} cy={yScale(d.callIV)} r="3" fill="#3b82f6" opacity="0.7" />
              <circle cx={xScale(d.strike)} cy={yScale(d.putIV)} r="3" fill="#ef4444" opacity="0.7" />
            </React.Fragment>
          ))}

          {/* ATM label */}
          <text x={xScale(spotPrice)} y={padding.top - 10} textAnchor="middle" fill="#999" fontSize="11" fontWeight="bold">
            ATM
          </text>

          {/* Y-axis labels */}
          {[0.05, 0.15, 0.25, 0.35].map((iv) => (
            <text
              key={iv}
              x={padding.left - 10}
              y={yScale(iv)}
              textAnchor="end"
              dominantBaseline="middle"
              fill="#999"
              fontSize="11"
            >
              {(iv * 100).toFixed(0)}%
            </text>
          ))}

          {/* X-axis labels */}
          {[0, 1, 2, 3, 4].map((i) => {
            const strike = minStrike + ((maxStrike - minStrike) / 4) * i;
            return (
              <text
                key={i}
                x={xScale(strike)}
                y={height - padding.bottom + 20}
                textAnchor="middle"
                fill="#999"
                fontSize="11"
              >
                ${strike.toFixed(0)}
              </text>
            );
          })}

          {/* Axis labels */}
          <text x={25} y={height / 2} textAnchor="middle" fill="#999" fontSize="12" transform={`rotate(-90 25 ${height / 2})`}>
            Implied Volatility
          </text>
          <text x={width / 2} y={height - 10} textAnchor="middle" fill="#999" fontSize="12">
            Strike Price
          </text>

          {/* Legend */}
          <g transform={`translate(${width - 150}, ${padding.top})`}>
            <rect width="140" height="80" fill="#1f2937" stroke="#444" rx="4" />
            <line x1="10" y1="15" x2="30" y2="15" stroke="#3b82f6" strokeWidth="2" />
            <text x="40" y="20" fill="#3b82f6" fontSize="12" fontWeight="bold">
              Call IV
            </text>
            <line x1="10" y1="40" x2="30" y2="40" stroke="#ef4444" strokeWidth="2" />
            <text x="40" y="45" fill="#ef4444" fontSize="12" fontWeight="bold">
              Put IV
            </text>
            <line x1="10" y1="65" x2="30" y2="65" stroke="#666" strokeWidth="2" strokeDasharray="4" />
            <text x="40" y="70" fill="#999" fontSize="11">
              ATM
            </text>
          </g>
        </svg>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-gray-400 text-sm mb-2">Avg Skew</div>
          <div className={`text-2xl font-bold ${metrics.avgSkew > 0.05 ? 'text-red-400' : 'text-green-400'}`}>
            {(metrics.avgSkew * 100).toFixed(2)}%
          </div>
          <p className="text-xs text-gray-500 mt-2">{metrics.avgSkew > 0.05 ? 'Put skew (bearish)' : 'Neutral'}</p>
        </div>

        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-gray-400 text-sm mb-2">OTM Put Skew</div>
          <div className={`text-2xl font-bold ${metrics.avgPutSkew > 0.08 ? 'text-red-400' : 'text-yellow-400'}`}>
            {(metrics.avgPutSkew * 100).toFixed(2)}%
          </div>
          <p className="text-xs text-gray-500 mt-2">{metrics.avgPutSkew > 0.08 ? 'High risk' : 'Normal'}</p>
        </div>

        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-gray-400 text-sm mb-2">Market Sentiment</div>
          <div className="flex items-center gap-2">
            {metrics.riskSentiment === 'bearish' ? (
              <TrendingDown className="w-5 h-5 text-red-400" />
            ) : (
              <TrendingUp className="w-5 h-5 text-green-400" />
            )}
            <span className={`text-lg font-bold ${metrics.riskSentiment === 'bearish' ? 'text-red-400' : 'text-green-400'}`}>
              {metrics.riskSentiment === 'bearish' ? 'Bearish' : 'Neutral'}
            </span>
          </div>
        </div>
      </div>

      {/* Risk Alert */}
      {metrics.avgPutSkew > 0.08 && (
        <div className="bg-red-900/30 border border-red-700 rounded-lg p-4 flex gap-3">
          <AlertTriangle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
          <div>
            <div className="text-red-100 font-semibold">High Put Skew Detected</div>
            <div className="text-red-200 text-sm">
              Traders are paying premium for downside protection. Market expects higher risk of decline. Consider defensive strategies.
            </div>
          </div>
        </div>
      )}

      {/* Interpretation */}
      <div className="bg-blue-900/20 border border-blue-700/30 rounded p-3 text-xs text-blue-300 space-y-1">
        <p>
          <strong>📊 Put Skew:</strong> OTM puts have higher IV than calls. Traders fear downside (tail risk). Bullish indicator contrarian.
        </p>
        <p>
          <strong>📊 Call Skew:</strong> OTM calls have higher IV than puts. Traders fear upside moves. Less common in equities.
        </p>
        <p>
          <strong>🎯 Trading Strategy:</strong> High put skew? Sell puts (collect premium), buy calls. Low put skew? Buy puts, sell calls.
        </p>
      </div>
    </div>
  );
};
