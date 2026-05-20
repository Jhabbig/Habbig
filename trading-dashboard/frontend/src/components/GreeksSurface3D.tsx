import React, { useMemo } from 'react';
import { TrendingUp, TrendingDown } from 'lucide-react';

interface Greeks3DData {
  strike: number;
  daysToExpiration: number;
  delta: number;
  gamma: number;
  vega: number;
  theta: number;
}

interface GreeksSurface3DProps {
  spotPrice: number;
  selectedGreek?: 'delta' | 'gamma' | 'vega' | 'theta';
}

export const GreeksSurface3D: React.FC<GreeksSurface3DProps> = ({ spotPrice, selectedGreek = 'delta' }) => {
  const [greekType, setGreekType] = React.useState<'delta' | 'gamma' | 'vega' | 'theta'>(selectedGreek);

  // Generate Greeks surface data
  const greeksData = useMemo(() => {
    const strikes = Array.from({ length: 13 }, (_, i) => spotPrice - 60 + i * 10);
    const expirations = [7, 14, 30, 60, 90, 180];
    const data: Greeks3DData[] = [];

    strikes.forEach((strike) => {
      expirations.forEach((dte) => {
        const moneyness = strike / spotPrice;
        const timeValue = Math.sqrt(dte / 365);

        // Simplified Black-Scholes Greeks
        const d1 = (Math.log(moneyness) + 0.05 * dte / 365) / (0.2 * timeValue + 0.01);
        const normCDF = (x: number) => 0.5 * (1 + Math.tanh(0.7978845608 * (x + 0.044715 * Math.pow(x, 3))));
        const normPDF = (x: number) => Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);

        const delta = normCDF(d1);
        const gamma = normPDF(d1) / (spotPrice * 0.2 * timeValue + 0.01);
        const vega = spotPrice * normPDF(d1) * timeValue / 100;
        const theta = -(spotPrice * normPDF(d1) * 0.2) / (2 * Math.sqrt(dte / 365)) / 365;

        data.push({
          strike,
          daysToExpiration: dte,
          delta,
          gamma,
          vega,
          theta,
        });
      });
    });

    return data;
  }, [spotPrice]);

  // Get value range for color mapping
  const getValueRange = (type: string) => {
    const values = greeksData.map((d) => d[type as keyof Greeks3DData] as number);
    return { min: Math.min(...values), max: Math.max(...values) };
  };

  const valueRange = getValueRange(greekType);

  // Color mapping function
  const getColor = (value: number): string => {
    const normalized = (value - valueRange.min) / (valueRange.max - valueRange.min + 0.001);
    if (normalized < 0.33) {
      return `rgb(239, 68, 68)`; // Red
    } else if (normalized < 0.66) {
      return `rgb(251, 191, 36)`; // Yellow
    } else {
      return `rgb(34, 197, 94)`; // Green
    }
  };

  const strikes = Array.from(new Set(greeksData.map((d) => d.strike))).sort((a, b) => a - b);
  const expirations = Array.from(new Set(greeksData.map((d) => d.daysToExpiration))).sort((a, b) => a - b);

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold text-gray-100 mb-3">Greeks Surface (Heatmap View)</h3>
        <p className="text-sm text-gray-400 mb-4">Darker colors = lower values, Brighter colors = higher values</p>
      </div>

      {/* Greek Type Selector */}
      <div className="flex gap-2">
        {(['delta', 'gamma', 'vega', 'theta'] as const).map((type) => (
          <button
            key={type}
            onClick={() => setGreekType(type)}
            className={`px-3 py-2 rounded text-sm font-medium transition ${
              greekType === type
                ? 'bg-blue-600 text-white'
                : 'bg-gray-700 text-gray-400 hover:text-gray-200'
            }`}
          >
            {type.toUpperCase()}
          </button>
        ))}
      </div>

      {/* Heatmap */}
      <div className="bg-gray-900 p-6 rounded-lg border border-gray-700 overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr>
              <th className="p-2 text-gray-400 text-left">Strike</th>
              {expirations.map((exp) => (
                <th key={exp} className="p-2 text-gray-400">
                  {exp}d
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {strikes.map((strike) => (
              <tr key={strike} className="border-t border-gray-700">
                <td className="p-2 text-gray-300 font-semibold">${strike.toFixed(0)}</td>
                {expirations.map((exp) => {
                  const cell = greeksData.find((d) => d.strike === strike && d.daysToExpiration === exp);
                  const value = cell ? cell[greekType as keyof Greeks3DData] : 0;
                  const bgColor = getColor(value as number);

                  return (
                    <td
                      key={`${strike}-${exp}`}
                      className="p-2 text-center rounded text-xs font-semibold text-white cursor-pointer hover:ring-2 hover:ring-blue-400 transition"
                      style={{ backgroundColor: bgColor }}
                      title={`Strike: $${strike}, DTE: ${exp}, ${greekType}: ${(value as number).toFixed(4)}`}
                    >
                      {(value as number).toFixed(2)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Legend */}
      <div className="grid grid-cols-3 gap-4 text-xs">
        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <div className="w-full h-2 bg-gradient-to-r from-red-500 via-yellow-500 to-green-500 rounded mb-2" />
          <div className="flex justify-between text-gray-400">
            <span>Low</span>
            <span>High</span>
          </div>
        </div>

        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <div className="text-gray-100 font-semibold mb-1">Strike Range</div>
          <div className="text-gray-400">${Math.min(...strikes).toFixed(0)} - ${Math.max(...strikes).toFixed(0)}</div>
        </div>

        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <div className="text-gray-100 font-semibold mb-1">Spot Price</div>
          <div className="text-gray-400">${spotPrice.toFixed(2)}</div>
        </div>
      </div>

      {/* Interpretation */}
      <div className="bg-blue-900/20 border border-blue-700/30 rounded p-3 text-xs text-blue-300">
        <p className="mb-2"><strong>📊 How to read:</strong> Each cell shows how a Greek changes across different strikes (columns) and expiration dates (rows).</p>
        <p><strong>🎯 Focus areas:</strong> Bright green (high Greek exposure), Dark red (low exposure). ATM options (middle column) have highest gamma and vega.</p>
      </div>
    </div>
  );
};
