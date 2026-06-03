import React, { useState, useEffect, useCallback } from 'react';
import { GreeksChain } from '../types';

interface GreeksHeatmapProps {
  ticker: string;
  spotPrice: number;
}

const GreeksHeatmapComponent: React.FC<GreeksHeatmapProps> = ({ ticker, spotPrice }) => {
  const [greeks, setGreeks] = useState<GreeksChain[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const fetchGreeks = async () => {
      setLoading(true);
      try {
        const response = await fetch(
          `/api/greeks?ticker=${ticker}&spot_price=${spotPrice}&expiration_days=30`
        );
        const data = await response.json();
        setGreeks(data);
      } catch (error) {
        console.error('Failed to fetch Greeks:', error);
      } finally {
        setLoading(false);
      }
    };

    fetchGreeks();
  }, [ticker, spotPrice]);

  const getColor = useCallback((value: number, type: string): string => {
    if (type === 'delta') {
      const normalized = (value + 1) / 2;
      if (normalized > 0.65) return 'bg-green-900';
      if (normalized > 0.35) return 'bg-gray-800';
      return 'bg-red-900';
    } else if (type === 'gamma') {
      if (value > 0.02) return 'bg-yellow-900';
      if (value > 0.01) return 'bg-gray-800';
      return 'bg-blue-900';
    } else if (type === 'vega') {
      if (value > 0.15) return 'bg-purple-900';
      if (value > 0.08) return 'bg-gray-800';
      return 'bg-gray-700';
    } else if (type === 'theta') {
      if (value < -0.02) return 'bg-red-900';
      if (value < -0.01) return 'bg-gray-800';
      return 'bg-green-900';
    }
    return 'bg-gray-800';
  }, []);

  if (loading) {
    return (
      <div className="text-gray-400 text-sm p-4">Loading Greeks...</div>
    );
  }

  return (
    <div className="space-y-4 p-4 bg-gray-900 rounded-lg border border-gray-700">
      <h3 className="text-lg font-semibold text-gray-100">Options Greeks</h3>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-gray-700">
              <th className="text-left py-2 px-2 text-gray-400">Strike</th>
              <th className="text-center py-2 px-2 text-gray-400">Call Δ</th>
              <th className="text-center py-2 px-2 text-gray-400">Put Δ</th>
              <th className="text-center py-2 px-2 text-gray-400">Γ</th>
              <th className="text-center py-2 px-2 text-gray-400">Θ</th>
              <th className="text-center py-2 px-2 text-gray-400">Ν</th>
            </tr>
          </thead>
          <tbody>
            {greeks.map((strike) => (
              <tr key={strike.strike} className="border-b border-gray-800">
                <td className="py-2 px-2 text-gray-300 font-semibold">
                  ${strike.strike.toFixed(0)}
                  {Math.abs(strike.strike - spotPrice) < 1 && (
                    <span className="ml-1 text-yellow-400">ATM</span>
                  )}
                </td>
                <td
                  className={`text-center py-2 px-2 ${getColor(
                    strike.call.delta,
                    'delta'
                  )} text-white rounded`}
                >
                  {strike.call.delta.toFixed(3)}
                </td>
                <td
                  className={`text-center py-2 px-2 ${getColor(
                    strike.put.delta,
                    'delta'
                  )} text-white rounded`}
                >
                  {strike.put.delta.toFixed(3)}
                </td>
                <td
                  className={`text-center py-2 px-2 ${getColor(
                    strike.call.gamma,
                    'gamma'
                  )} text-white rounded`}
                >
                  {strike.call.gamma.toFixed(4)}
                </td>
                <td
                  className={`text-center py-2 px-2 ${getColor(
                    strike.call.theta,
                    'theta'
                  )} text-white rounded`}
                >
                  {strike.call.theta.toFixed(4)}
                </td>
                <td
                  className={`text-center py-2 px-2 ${getColor(
                    strike.call.vega,
                    'vega'
                  )} text-white rounded`}
                >
                  {strike.call.vega.toFixed(4)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="text-xs text-gray-500 space-y-1 pt-3 border-t border-gray-700">
        <p>Δ (Delta): Directional exposure</p>
        <p>Γ (Gamma): Delta acceleration</p>
        <p>Θ (Theta): Time decay per day</p>
        <p>Ν (Vega): 1% vol sensitivity</p>
      </div>
    </div>
  );
};

export const GreeksHeatmap = React.memo(GreeksHeatmapComponent);
