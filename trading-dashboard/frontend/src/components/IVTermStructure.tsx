import React, { useMemo, useState } from 'react';
import { TrendingUp, TrendingDown } from 'lucide-react';

interface IVTermStructureProps {
  spotPrice: number;
}

const IVTermStructureComponent: React.FC<IVTermStructureProps> = ({ spotPrice }) => {
  const [selectedStrike, setSelectedStrike] = useState('ATM');

  // Generate IV term structure data
  const ivData = useMemo(() => {
    const strikes = {
      OTM: spotPrice * 0.95, // Out of money
      ATM: spotPrice, // At the money
      ITM: spotPrice * 1.05, // In the money
    };

    const expirations = [7, 14, 30, 60, 90, 180, 365];

    const data: Record<string, Array<{ dte: number; iv: number }>> = {
      OTM: [],
      ATM: [],
      ITM: [],
    };

    (Object.keys(strikes) as Array<keyof typeof strikes>).forEach((strikeType) => {
      expirations.forEach((dte) => {
        // Simplified IV curve (term structure)
        // Typically: near-term has higher IV (term structure is downward sloping)
        const baseIV = 0.25;
        const termStructure = 0.25 - 0.1 * Math.log(dte / 7 + 1) / Math.log(365 / 7 + 1);
        const iv = Math.max(0.1, baseIV + (Math.random() - 0.5) * 0.1 + termStructure);

        data[strikeType].push({
          dte,
          iv,
        });
      });
    });

    return data;
  }, [spotPrice]);

  const currentData = ivData[selectedStrike as keyof typeof ivData];

  const chartConfig = useMemo(() => {
    const width = 600;
    const height = 300;
    const padding = { top: 30, right: 30, bottom: 50, left: 60 };
    return { width, height, padding, chartWidth: width - padding.left - padding.right, chartHeight: height - padding.top - padding.bottom };
  }, []);

  const ivStats = useMemo(() => {
    const maxDTE = Math.max(...currentData.map((d) => d.dte));
    const minIV = Math.min(...currentData.map((d) => d.iv));
    const maxIV = Math.max(...currentData.map((d) => d.iv));
    return { maxDTE, minIV, maxIV };
  }, [currentData]);

  const scales = useMemo(() => {
    const xScale = (dte: number) => chartConfig.padding.left + (dte / ivStats.maxDTE) * chartConfig.chartWidth;
    const yScale = (iv: number) => chartConfig.padding.top + chartConfig.chartHeight - ((iv - ivStats.minIV) / (ivStats.maxIV - ivStats.minIV + 0.001)) * chartConfig.chartHeight;
    return { xScale, yScale };
  }, [chartConfig, ivStats]);

  const points = useMemo(() =>
    currentData.map((d) => `${scales.xScale(d.dte)},${scales.yScale(d.iv)}`).join(' '),
    [currentData, scales]
  );

  const trend = useMemo(() =>
    currentData[currentData.length - 1].iv - currentData[0].iv > 0 ? 'increasing' : 'decreasing',
    [currentData]
  );

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold text-gray-100 mb-3">IV Term Structure</h3>
        <p className="text-sm text-gray-400 mb-4">How implied volatility changes across different expiration dates</p>
      </div>

      {/* Strike Selector */}
      <div className="flex gap-2">
        {(['OTM', 'ATM', 'ITM'] as const).map((strike) => (
          <button
            key={strike}
            onClick={() => setSelectedStrike(strike)}
            className={`px-3 py-2 rounded text-sm font-medium transition ${
              selectedStrike === strike
                ? 'bg-blue-600 text-white'
                : 'bg-gray-700 text-gray-400 hover:text-gray-200'
            }`}
          >
            {strike === 'OTM' && `OTM (${(spotPrice * 0.95).toFixed(0)})`}
            {strike === 'ATM' && `ATM (${spotPrice.toFixed(0)})`}
            {strike === 'ITM' && `ITM (${(spotPrice * 1.05).toFixed(0)})`}
          </button>
        ))}
      </div>

      {/* SVG Chart */}
      <div className="bg-gray-900 p-4 rounded-lg border border-gray-700 overflow-x-auto">
        <svg width={chartConfig.width} height={chartConfig.height} className="bg-gray-800 rounded">
          {/* Grid lines */}
          {Array.from({ length: 5 }).map((_, i) => (
            <React.Fragment key={`grid-${i}`}>
              <line
                x1={chartConfig.padding.left}
                y1={chartConfig.padding.top + (chartConfig.chartHeight / 4) * i}
                x2={chartConfig.width - chartConfig.padding.right}
                y2={chartConfig.padding.top + (chartConfig.chartHeight / 4) * i}
                stroke="#444"
                strokeWidth="1"
                strokeDasharray="4"
              />
            </React.Fragment>
          ))}

          {/* Axes */}
          <line
            x1={chartConfig.padding.left}
            y1={chartConfig.padding.top}
            x2={chartConfig.padding.left}
            y2={chartConfig.height - chartConfig.padding.bottom}
            stroke="#666"
            strokeWidth="2"
          />
          <line
            x1={chartConfig.padding.left}
            y1={chartConfig.height - chartConfig.padding.bottom}
            x2={chartConfig.width - chartConfig.padding.right}
            y2={chartConfig.height - chartConfig.padding.bottom}
            stroke="#666"
            strokeWidth="2"
          />

          {/* Line */}
          <polyline points={points} fill="none" stroke="#3b82f6" strokeWidth="2" />

          {/* Gradient fill under line */}
          <polygon
            points={`${chartConfig.padding.left},${chartConfig.height - chartConfig.padding.bottom} ${points} ${chartConfig.width - chartConfig.padding.right},${chartConfig.height - chartConfig.padding.bottom}`}
            fill="url(#ivGradient)"
            opacity="0.2"
          />

          {/* Gradient definition */}
          <defs>
            <linearGradient id="ivGradient" x1="0%" y1="0%" x2="0%" y2="100%">
              <stop offset="0%" stopColor="#3b82f6" />
              <stop offset="100%" stopColor="#1f2937" />
            </linearGradient>
          </defs>

          {/* Data points */}
          {currentData.map((d) => (
            <circle
              key={d.dte}
              cx={scales.xScale(d.dte)}
              cy={scales.yScale(d.iv)}
              r="4"
              fill="#3b82f6"
              stroke="#1f2937"
              strokeWidth="2"
              className="cursor-pointer hover:r-6 transition"
            />
          ))}

          {/* Y-axis labels */}
          {Array.from({ length: 5 }).map((_, i) => {
            const iv = ivStats.minIV + ((ivStats.maxIV - ivStats.minIV) / 4) * i;
            return (
              <text
                key={`y-${i}`}
                x={chartConfig.padding.left - 10}
                y={chartConfig.padding.top + (chartConfig.chartHeight / 4) * (4 - i)}
                textAnchor="end"
                dominantBaseline="middle"
                fill="#999"
                fontSize="12"
              >
                {(iv * 100).toFixed(0)}%
              </text>
            );
          })}

          {/* X-axis labels */}
          {[7, 30, 60, 90, 180, 365].map((dte) => (
            <text
              key={`x-${dte}`}
              x={scales.xScale(dte)}
              y={chartConfig.height - chartConfig.padding.bottom + 20}
              textAnchor="middle"
              fill="#999"
              fontSize="12"
            >
              {dte}d
            </text>
          ))}

          {/* Axis labels */}
          <text x={20} y={chartConfig.height / 2} textAnchor="middle" fill="#999" fontSize="12" transform={`rotate(-90 20 ${chartConfig.height / 2})`}>
            Implied Volatility
          </text>
          <text x={chartConfig.width / 2} y={chartConfig.height - 10} textAnchor="middle" fill="#999" fontSize="12">
            Days to Expiration
          </text>
        </svg>
      </div>

      {/* Key Metrics */}
      <div className="grid grid-cols-2 gap-4">
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-gray-400 text-sm mb-2">Term Structure</div>
          <div className="flex items-center gap-2">
            {trend === 'increasing' ? (
              <TrendingUp className="w-5 h-5 text-green-400" />
            ) : (
              <TrendingDown className="w-5 h-5 text-red-400" />
            )}
            <span className={`text-lg font-bold ${trend === 'increasing' ? 'text-green-400' : 'text-red-400'}`}>
              {trend === 'increasing' ? 'Contango' : 'Backwardation'}
            </span>
          </div>
          <p className="text-xs text-gray-500 mt-2">
            {trend === 'increasing'
              ? 'Near-term IV lower than far-term (normal market)'
              : 'Near-term IV higher than far-term (elevated risk)'}
          </p>
        </div>

        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-gray-400 text-sm mb-2">IV Range</div>
          <div className="text-lg font-bold text-blue-400">
            {(ivStats.minIV * 100).toFixed(1)}% - {(ivStats.maxIV * 100).toFixed(1)}%
          </div>
          <p className="text-xs text-gray-500 mt-2">Implied volatility spread across expiration dates</p>
        </div>
      </div>

      {/* Interpretation */}
      <div className="bg-blue-900/20 border border-blue-700/30 rounded p-3 text-xs text-blue-300 space-y-1">
        <p><strong>📈 Contango:</strong> Near-term IV lower than far-term. Normal market condition. Sell near-term premium.</p>
        <p><strong>📉 Backwardation:</strong> Near-term IV higher than far-term. High uncertainty/risk. Sell far-term, buy near-term.</p>
        <p><strong>🎯 Trading Edge:</strong> Trade the curve: sell overpriced expiration, buy underpriced expiration.</p>
      </div>
    </div>
  );
};

export const IVTermStructure = React.memo(IVTermStructureComponent);
