import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { OrderForm, Trade } from '../components/OrderForm';
import { PositionsPanel } from '../components/PositionsPanel';
import { TradeJournal } from '../components/TradeJournal';

interface TradingLiveProps {
  selectedTicker: string;
  currentPrice: number;
}

const sideDirection = (side: Trade['side']) => (side === 'buy' ? 1 : -1);

export const TradingLive: React.FC<TradingLiveProps> = ({ selectedTicker, currentPrice }) => {
  const [trades, setTrades] = useState<Trade[]>([]);
  const sessionStats = { startCapital: 100000 };

  // Per-ticker mark prices. Each position is valued against the latest price
  // we have for its own ticker, not the dashboard's currently selected one.
  const [priceMap, setPriceMap] = useState<Record<string, number>>({});

  useEffect(() => {
    if (!selectedTicker || !Number.isFinite(currentPrice) || currentPrice <= 0) return;
    setPriceMap((prev) =>
      prev[selectedTicker] === currentPrice ? prev : { ...prev, [selectedTicker]: currentPrice }
    );
  }, [selectedTicker, currentPrice]);

  const priceFor = useCallback(
    (ticker: string): number | null => {
      const p = priceMap[ticker];
      return Number.isFinite(p) && p > 0 ? p : null;
    },
    [priceMap]
  );

  const handleNewTrade = (trade: Trade) => {
    setPriceMap((prev) => (prev[trade.ticker] ? prev : { ...prev, [trade.ticker]: trade.entryPrice }));
    setTrades((prev) => [...prev, trade]);
  };

  const handleClosePosition = (tradeId: string) => {
    setTrades((prev) =>
      prev.map((trade) => {
        if (trade.id !== tradeId || trade.exitPrice) return trade;
        const mark = priceFor(trade.ticker);
        if (mark === null) return trade; // Refuse to mark at a fictitious price.
        const dir = sideDirection(trade.side);
        const pnl = (mark - trade.entryPrice) * trade.quantity * dir;
        const pnlPct = ((mark - trade.entryPrice) / trade.entryPrice) * 100 * dir;
        return {
          ...trade,
          exitPrice: mark,
          exitTime: Math.floor(Date.now() / 1000),
          pnl,
          pnlPct,
          reason: 'Manual close',
        };
      })
    );
  };

  const openPositions = useMemo(() => trades.filter((t) => !t.exitPrice), [trades]);
  const closedTrades = useMemo(() => trades.filter((t) => t.exitPrice), [trades]);

  const totalOpenPnL = openPositions.reduce((sum, p) => {
    const mark = priceFor(p.ticker);
    if (mark === null) return sum;
    return sum + (mark - p.entryPrice) * p.quantity * sideDirection(p.side);
  }, 0);
  const totalClosedPnL = closedTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
  const currentEquity = sessionStats.startCapital + totalOpenPnL + totalClosedPnL;
  const totalPnL = currentEquity - sessionStats.startCapital;
  const returnPct = (totalPnL / sessionStats.startCapital) * 100;

  return (
    <div className="space-y-6">
      {/* Session Stats */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Starting Capital</div>
          <div className="text-2xl font-bold text-gray-100">${sessionStats.startCapital.toLocaleString()}</div>
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Current Equity</div>
          <div className={`text-2xl font-bold ${currentEquity >= sessionStats.startCapital ? 'text-green-400' : 'text-red-400'}`}>
            ${currentEquity.toLocaleString(undefined, { maximumFractionDigits: 2 })}
          </div>
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Total P&L</div>
          <div className={`text-2xl font-bold ${totalPnL >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {totalPnL >= 0 ? '+' : ''}${totalPnL.toLocaleString(undefined, { maximumFractionDigits: 2 })}
          </div>
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Return %</div>
          <div className={`text-2xl font-bold ${returnPct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {returnPct.toFixed(2)}%
          </div>
        </div>
      </div>

      {/* Main Layout */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left: Order Form */}
        <div>
          <OrderForm selectedTicker={selectedTicker} currentPrice={currentPrice} onTrade={handleNewTrade} />
        </div>

        {/* Right: Positions and Journal */}
        <div className="lg:col-span-2 space-y-6">
          <PositionsPanel
            positions={openPositions}
            priceFor={priceFor}
            onClosePosition={handleClosePosition}
          />
          <TradeJournal trades={trades} />
        </div>
      </div>
    </div>
  );
};
