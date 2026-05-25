import React, { useState, useMemo } from 'react';
import { AlertTriangle, TrendingDown, Zap, PieChart } from 'lucide-react';

export interface PortfolioPosition {
  ticker: string;
  quantity: number;
  entryPrice: number;
  currentPrice: number;
  sector: string;
  Greeks?: {
    delta: number;
    gamma: number;
    vega: number;
    theta: number;
  };
}

interface RiskDashboardProps {
  positions: PortfolioPosition[];
  currentEquity: number;
  startCapital: number;
}

export const RiskDashboard: React.FC<RiskDashboardProps> = ({ positions, currentEquity, startCapital }) => {
  const [view, setView] = useState<'sectors' | 'greeks' | 'var'>('sectors');

  // Calculate sector exposure
  const sectorExposure = useMemo(() => {
    const sectors: Record<string, number> = {};
    const totalValue = positions.reduce((sum, p) => sum + p.quantity * p.currentPrice, 0);

    positions.forEach((p) => {
      if (!sectors[p.sector]) sectors[p.sector] = 0;
      sectors[p.sector] += (p.quantity * p.currentPrice) / totalValue;
    });

    return Object.entries(sectors).map(([sector, exposure]) => ({
      sector,
      exposure: exposure * 100,
    }));
  }, [positions]);

  // Calculate Greeks exposure
  const greeksExposure = useMemo(() => {
    const totalValue = positions.reduce((sum, p) => sum + p.quantity * p.currentPrice, 0);

    const delta = positions.reduce((sum, p) => sum + (p.Greeks?.delta || 0) * p.quantity, 0);
    const gamma = positions.reduce((sum, p) => sum + (p.Greeks?.gamma || 0) * p.quantity, 0);
    const vega = positions.reduce((sum, p) => sum + (p.Greeks?.vega || 0) * p.quantity, 0);
    const theta = positions.reduce((sum, p) => sum + (p.Greeks?.theta || 0) * p.quantity, 0);

    return { delta, gamma, vega, theta };
  }, [positions]);

  // Calculate VaR (simplified 95% confidence)
  const calculateVaR = () => {
    const returns = positions.map((p) => (p.currentPrice - p.entryPrice) / p.entryPrice);
    if (returns.length === 0) return 0;

    const avgReturn = returns.reduce((a, b) => a + b, 0) / returns.length;
    const variance = returns.reduce((sum, r) => sum + Math.pow(r - avgReturn, 2), 0) / returns.length;
    const stdDev = Math.sqrt(variance);

    // 95% VaR ≈ mean - 1.645 * stdDev
    return avgReturn - 1.645 * stdDev;
  };

  const var95 = calculateVaR();
  const maxDrawdown = ((currentEquity - startCapital) / startCapital) * 100;
  const isRisk = maxDrawdown < -5;

  // Mock sector colors
  const sectorColor = (sector: string): string => {
    const colors: Record<string, string> = {
      Tech: 'bg-blue-500',
      Finance: 'bg-purple-500',
      Healthcare: 'bg-green-500',
      Energy: 'bg-orange-500',
      Industrials: 'bg-gray-500',
      Consumer: 'bg-pink-500',
      Other: 'bg-indigo-500',
    };
    return colors[sector] || colors.Other;
  };

  return (
    <div className="space-y-4">
      {/* Risk Alert Banner */}
      {isRisk && (
        <div className="bg-red-900/30 border border-red-700 rounded-lg p-4 flex gap-3">
          <AlertTriangle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
          <div>
            <div className="text-red-100 font-semibold">Portfolio Risk Alert</div>
            <div className="text-red-200 text-sm">Drawdown exceeds -5%. Consider reducing exposure or tightening stops.</div>
          </div>
        </div>
      )}

      {/* Key Metrics */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-xs text-gray-400 mb-1">Portfolio Delta</div>
          <div className={`text-2xl font-bold ${greeksExposure.delta > 0 ? 'text-green-400' : 'text-red-400'}`}>
            {greeksExposure.delta.toFixed(2)}
          </div>
          <div className="text-xs text-gray-500 mt-1">Directional exposure</div>
        </div>

        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-xs text-gray-400 mb-1">Portfolio Vega</div>
          <div className={`text-2xl font-bold ${greeksExposure.vega > 0 ? 'text-orange-400' : 'text-blue-400'}`}>
            {greeksExposure.vega.toFixed(2)}
          </div>
          <div className="text-xs text-gray-500 mt-1">Volatility exposure</div>
        </div>

        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-xs text-gray-400 mb-1">Max Drawdown</div>
          <div className={`text-2xl font-bold ${maxDrawdown > 0 ? 'text-green-400' : 'text-red-400'}`}>
            {maxDrawdown.toFixed(2)}%
          </div>
          <div className="text-xs text-gray-500 mt-1">Peak to trough</div>
        </div>

        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-xs text-gray-400 mb-1">95% VaR</div>
          <div className={`text-2xl font-bold ${var95 > 0 ? 'text-green-400' : 'text-red-400'}`}>
            {(var95 * 100).toFixed(2)}%
          </div>
          <div className="text-xs text-gray-500 mt-1">Value at Risk</div>
        </div>
      </div>

      {/* View Selector */}
      <div className="flex gap-2">
        {(['sectors', 'greeks', 'var'] as const).map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={`px-3 py-2 rounded text-sm font-medium transition ${
              view === v
                ? 'bg-blue-600 text-white'
                : 'bg-gray-700 text-gray-400 hover:text-gray-200'
            }`}
          >
            {v === 'sectors' && 'Sector Exposure'}
            {v === 'greeks' && 'Greeks Exposure'}
            {v === 'var' && 'Risk Metrics'}
          </button>
        ))}
      </div>

      {/* Sector Exposure */}
      {view === 'sectors' && (
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <h3 className="text-lg font-semibold text-gray-100 mb-4 flex items-center gap-2">
            <PieChart className="w-5 h-5" />
            Sector Allocation
          </h3>

          <div className="space-y-2">
            {sectorExposure.map((s) => (
              <div key={s.sector}>
                <div className="flex justify-between mb-1">
                  <span className="text-gray-300 text-sm">{s.sector}</span>
                  <span className="text-gray-100 font-semibold">{s.exposure.toFixed(1)}%</span>
                </div>
                <div className="w-full bg-gray-900 rounded-full h-2 overflow-hidden">
                  <div
                    className={`h-full ${sectorColor(s.sector)}`}
                    style={{ width: `${Math.min(100, s.exposure)}%` }}
                  />
                </div>
              </div>
            ))}
          </div>

          {/* Warnings */}
          <div className="mt-4 p-3 bg-yellow-900/20 border border-yellow-700/30 rounded text-xs text-yellow-300">
            ⚠️ Limit sector exposure to max 30% each to maintain diversification
          </div>
        </div>
      )}

      {/* Greeks Exposure */}
      {view === 'greeks' && (
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <h3 className="text-lg font-semibold text-gray-100 mb-4 flex items-center gap-2">
            <Zap className="w-5 h-5" />
            Options Greeks Exposure
          </h3>

          <div className="grid grid-cols-2 gap-4">
            <div className="bg-gray-900 p-4 rounded border border-gray-700">
              <div className="text-gray-400 text-sm mb-2">Delta (Directional)</div>
              <div className={`text-3xl font-bold ${greeksExposure.delta > 0 ? 'text-green-400' : 'text-red-400'}`}>
                {greeksExposure.delta.toFixed(3)}
              </div>
              <div className="text-xs text-gray-500 mt-1">+1 = 100% up, -1 = 100% down</div>
            </div>

            <div className="bg-gray-900 p-4 rounded border border-gray-700">
              <div className="text-gray-400 text-sm mb-2">Gamma (Delta Change)</div>
              <div className={`text-3xl font-bold ${greeksExposure.gamma > 0 ? 'text-blue-400' : 'text-purple-400'}`}>
                {greeksExposure.gamma.toFixed(5)}
              </div>
              <div className="text-xs text-gray-500 mt-1">How delta changes with price</div>
            </div>

            <div className="bg-gray-900 p-4 rounded border border-gray-700">
              <div className="text-gray-400 text-sm mb-2">Vega (Volatility)</div>
              <div className={`text-3xl font-bold ${greeksExposure.vega > 0 ? 'text-orange-400' : 'text-cyan-400'}`}>
                {greeksExposure.vega.toFixed(3)}
              </div>
              <div className="text-xs text-gray-500 mt-1">Exposure to IV changes</div>
            </div>

            <div className="bg-gray-900 p-4 rounded border border-gray-700">
              <div className="text-gray-400 text-sm mb-2">Theta (Time Decay)</div>
              <div className={`text-3xl font-bold ${greeksExposure.theta > 0 ? 'text-green-400' : 'text-red-400'}`}>
                {greeksExposure.theta.toFixed(3)}
              </div>
              <div className="text-xs text-gray-500 mt-1">Daily P&L from time decay</div>
            </div>
          </div>

          {/* Interpretation */}
          <div className="mt-4 p-3 bg-blue-900/20 border border-blue-700/30 rounded text-xs text-blue-300 space-y-1">
            <p>📊 <strong>Delta:</strong> {greeksExposure.delta > 0.5 ? 'Bullish (70%+)' : greeksExposure.delta < -0.5 ? 'Bearish (70%+)' : 'Neutral'}</p>
            <p>📈 <strong>Vega:</strong> {greeksExposure.vega > 0 ? 'Long volatility' : 'Short volatility'}</p>
          </div>
        </div>
      )}

      {/* Risk Metrics */}
      {view === 'var' && (
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <h3 className="text-lg font-semibold text-gray-100 mb-4 flex items-center gap-2">
            <TrendingDown className="w-5 h-5" />
            Risk Metrics
          </h3>

          <div className="space-y-4">
            <div className="bg-gray-900 p-4 rounded border border-gray-700">
              <div className="text-gray-400 text-sm mb-2">Value at Risk (95%)</div>
              <div className="text-3xl font-bold text-red-400">{(var95 * 100).toFixed(2)}%</div>
              <div className="text-xs text-gray-500 mt-2">
                Worst expected loss with 95% confidence over 1 day
              </div>
            </div>

            <div className="bg-gray-900 p-4 rounded border border-gray-700">
              <div className="text-gray-400 text-sm mb-2">Current Drawdown</div>
              <div className={`text-3xl font-bold ${maxDrawdown > 0 ? 'text-green-400' : 'text-red-400'}`}>
                {maxDrawdown.toFixed(2)}%
              </div>
              <div className="text-xs text-gray-500 mt-2">
                Loss from starting capital to current equity
              </div>
            </div>

            <div className="bg-gray-900 p-4 rounded border border-gray-700">
              <div className="text-gray-400 text-sm mb-2">Largest Position</div>
              {positions.length > 0 && (
                <>
                  <div className="text-xl font-bold text-gray-100">
                    {positions.reduce((max, p) => (p.quantity * p.currentPrice > max.quantity * max.currentPrice ? p : max)).ticker}
                  </div>
                  <div className="text-xs text-gray-500 mt-2">
                    {(
                      (positions.reduce((max, p) =>
                        p.quantity * p.currentPrice > max.quantity * max.currentPrice ? p : max
                      ).quantity *
                        positions.reduce((max, p) =>
                          p.quantity * p.currentPrice > max.quantity * max.currentPrice ? p : max
                        ).currentPrice) /
                      currentEquity
                    ) * 100).toFixed(1)}% of portfolio
                  </div>
                </>
              )}
            </div>

            <div className="bg-blue-900/20 border border-blue-700/30 rounded p-3 text-xs text-blue-300">
              ✓ Limit single position to max 10% of capital<br/>
              ✓ Limit sector exposure to max 30% each<br/>
              ✓ Keep portfolio delta between -0.3 and 0.3 for neutral strategy<br/>
              ✓ Monitor vega daily during earnings season
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
