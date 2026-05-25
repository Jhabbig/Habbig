import React, { useState } from 'react';
import { Plus, X } from 'lucide-react';

export interface Order {
  id: string;
  ticker: string;
  side: 'buy' | 'sell';
  quantity: number;
  price: number;
  orderType: 'market' | 'limit' | 'stop';
  timestamp: number;
}

export interface Trade {
  id: string;
  ticker: string;
  side: 'buy' | 'sell';
  entryPrice: number;
  exitPrice?: number;
  quantity: number;
  entryTime: number;
  exitTime?: number;
  pnl?: number;
  pnlPct?: number;
  reason?: string;
}

interface OrderFormProps {
  currentPrice: number;
  onTrade: (trade: Trade) => void;
}

export const OrderForm: React.FC<OrderFormProps> = ({ currentPrice, onTrade }) => {
  const [ticker, setTicker] = useState('AAPL');
  const [side, setSide] = useState<'buy' | 'sell'>('buy');
  const [quantity, setQuantity] = useState(100);
  const [price, setPrice] = useState(currentPrice.toString());
  const [orderType, setOrderType] = useState<'market' | 'limit' | 'stop'>('market');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    const tradePrice = orderType === 'market' ? currentPrice : parseFloat(price);
    const trade: Trade = {
      id: Math.random().toString(36).substr(2, 9),
      ticker,
      side,
      entryPrice: tradePrice,
      quantity,
      entryTime: Math.floor(Date.now() / 1000),
      reason: `Manual ${orderType} order`,
    };

    onTrade(trade);

    // Reset form
    setQuantity(100);
    setPrice(currentPrice.toString());
  };

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
      <h3 className="text-lg font-semibold text-gray-100 mb-4 flex items-center gap-2">
        <Plus className="w-5 h-5" />
        Place Trade
      </h3>

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Ticker */}
        <div>
          <label className="text-sm text-gray-400 mb-1 block">Ticker</label>
          <input
            type="text"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            maxLength={5}
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500 text-sm"
          />
        </div>

        {/* Side */}
        <div>
          <label className="text-sm text-gray-400 mb-1 block">Side</label>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setSide('buy')}
              className={`flex-1 px-3 py-2 rounded font-medium transition ${
                side === 'buy'
                  ? 'bg-green-600 text-white'
                  : 'bg-gray-700 text-gray-400 hover:text-gray-200'
              }`}
            >
              Buy
            </button>
            <button
              type="button"
              onClick={() => setSide('sell')}
              className={`flex-1 px-3 py-2 rounded font-medium transition ${
                side === 'sell'
                  ? 'bg-red-600 text-white'
                  : 'bg-gray-700 text-gray-400 hover:text-gray-200'
              }`}
            >
              Sell
            </button>
          </div>
        </div>

        {/* Quantity */}
        <div>
          <label className="text-sm text-gray-400 mb-1 block">Quantity</label>
          <input
            type="number"
            value={quantity}
            onChange={(e) => setQuantity(Math.max(1, parseInt(e.target.value) || 1))}
            min="1"
            step="1"
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500 text-sm"
          />
        </div>

        {/* Order Type */}
        <div>
          <label className="text-sm text-gray-400 mb-1 block">Order Type</label>
          <select
            value={orderType}
            onChange={(e) => setOrderType(e.target.value as any)}
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500 text-sm"
          >
            <option value="market">Market</option>
            <option value="limit">Limit</option>
            <option value="stop">Stop</option>
          </select>
        </div>

        {/* Price (for limit/stop) */}
        {orderType !== 'market' && (
          <div>
            <label className="text-sm text-gray-400 mb-1 block">Price</label>
            <input
              type="number"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              step="0.01"
              className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500 text-sm"
            />
          </div>
        )}

        {/* Current Price Display */}
        <div className="bg-gray-900 p-3 rounded border border-gray-700">
          <div className="text-xs text-gray-400">Current Price</div>
          <div className="text-lg font-bold text-gray-100">${currentPrice.toFixed(2)}</div>
        </div>

        {/* Submit */}
        <button
          type="submit"
          className={`w-full py-2 rounded font-semibold transition ${
            side === 'buy'
              ? 'bg-green-600 hover:bg-green-700 text-white'
              : 'bg-red-600 hover:bg-red-700 text-white'
          }`}
        >
          {side === 'buy' ? 'Buy' : 'Sell'} {quantity} {ticker}
        </button>
      </form>
    </div>
  );
};
