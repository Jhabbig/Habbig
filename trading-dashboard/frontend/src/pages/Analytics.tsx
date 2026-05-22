import React, { useMemo, useState } from 'react';
import { RiskDashboard, type PortfolioPosition } from '../components/RiskDashboard';
import { BacktestOptimizer } from '../components/BacktestOptimizer';

export const Analytics: React.FC = () => {
  const [selectedTab, setSelectedTab] = useState<'risk' | 'optimizer'>('risk');

  // Memoized so RiskDashboard's child useMemos don't recompute on every parent render.
  const mockPositions = useMemo<PortfolioPosition[]>(
    () => [
      {
        ticker: 'AAPL',
        quantity: 100,
        entryPrice: 150,
        currentPrice: 155,
        sector: 'Tech',
        Greeks: { delta: 0.65, gamma: 0.012, vega: 0.25, theta: -0.05 },
      },
      {
        ticker: 'TSLA',
        quantity: 50,
        entryPrice: 180,
        currentPrice: 175,
        sector: 'Tech',
        Greeks: { delta: -0.45, gamma: 0.008, vega: 0.18, theta: -0.03 },
      },
      {
        ticker: 'JPM',
        quantity: 150,
        entryPrice: 145,
        currentPrice: 148,
        sector: 'Finance',
        Greeks: { delta: 0.35, gamma: 0.005, vega: 0.12, theta: -0.02 },
      },
      {
        ticker: 'JNJ',
        quantity: 75,
        entryPrice: 155,
        currentPrice: 158,
        sector: 'Healthcare',
        Greeks: { delta: 0.25, gamma: 0.004, vega: 0.08, theta: -0.01 },
      },
    ],
    []
  );

  const currentEquity = mockPositions.reduce((sum, p) => sum + p.quantity * p.currentPrice, 0);
  const startCapital = 100000;

  return (
    <div className="space-y-6">
      <div className="flex gap-2 border-b border-gray-700">
        {(['risk', 'optimizer'] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setSelectedTab(tab)}
            className={`py-3 px-4 font-medium border-b-2 transition ${
              selectedTab === tab
                ? 'border-blue-500 text-blue-400'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            {tab === 'risk' && '📊 Risk Dashboard'}
            {tab === 'optimizer' && '⚙️ Strategy Optimizer'}
          </button>
        ))}
      </div>

      {selectedTab === 'risk' && (
        <RiskDashboard positions={mockPositions} currentEquity={currentEquity} startCapital={startCapital} />
      )}

      {selectedTab === 'optimizer' && <BacktestOptimizer />}
    </div>
  );
};
