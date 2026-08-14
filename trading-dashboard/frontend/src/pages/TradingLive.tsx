import React, { useState } from 'react';
import { OrderForm, Trade } from '../components/OrderForm';
import { PositionsPanel } from '../components/PositionsPanel';
import { TradeJournal } from '../components/TradeJournal';

interface TradingLiveProps {
  currentPrice: number;
}

export const TradingLive: React.FC<TradingLiveProps> = ({ currentPrice }) => {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [sessionStats, setSessionStats] = useState({
    startCapital: 100000,
    currentEquity: 100000,
  });

  const handleNewTrade = (trade: Trade) => {
    setTrades([...trades, trade]);
  };

  const handleClosePosition = (tradeId: string) => {
    setTrades(
      trades.map((trade) => {
        if (trade.id === tradeId && !trade.exitPrice) {
          const pnl = (currentPrice - trade.entryPrice) * trade.quantity;
          const pnlPct = ((currentPrice - trade.entryPrice) / trade.entryPrice) * 100;
          return {
            ...trade,
            exitPrice: currentPrice,
            exitTime: Math.floor(Date.now() / 1000),
            pnl,
            pnlPct,
            reason: 'Manual close',
          };
        }
        return trade;
      })
    );
  };

  const openPositions = trades.filter((t) => !t.exitPrice);
  const closedTrades = trades.filter((t) => t.exitPrice);
  const totalOpenValue = openPositions.reduce((sum, p) => sum + p.entryPrice * p.quantity, 0);
  const totalOpenPnL = openPositions.reduce((sum, p) => sum + (currentPrice - p.entryPrice) * p.quantity, 0);
  const totalClosedPnL = closedTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
  const currentEquity = sessionStats.startCapital + totalOpenPnL + totalClosedPnL;

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
            ${currentEquity.toLocaleString()}
          </div>
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Total P&L</div>
          <div
            className={`text-2xl font-bold ${currentEquity - sessionStats.startCapital >= 0 ? 'text-green-400' : 'text-red-400'}`}
          >
            {currentEquity - sessionStats.startCapital >= 0 ? '+' : ''}${(currentEquity - sessionStats.startCapital).toLocaleString()}
          </div>
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Return %</div>
          <div
            className={`text-2xl font-bold ${
              (currentEquity - sessionStats.startCapital) / sessionStats.startCapital >= 0 ? 'text-green-400' : 'text-red-400'
            }`}
          >
            {((currentEquity - sessionStats.startCapital) / sessionStats.startCapital * 100).toFixed(2)}%
          </div>
        </div>
      </div>

      {/* Main Layout */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left: Order Form */}
        <div>
          <OrderForm currentPrice={currentPrice} onTrade={handleNewTrade} />
        </div>

        {/* Right: Positions and Journal */}
        <div className="lg:col-span-2 space-y-6">
          {/* Positions */}
          <PositionsPanel positions={openPositions} currentPrice={currentPrice} onClosePosition={handleClosePosition} />

          {/* Trade Journal */}
          <TradeJournal trades={trades} />
        </div>
      </div>
    </div>
  );
};
