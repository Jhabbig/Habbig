import React, { useEffect, useState } from 'react';
import { Plus } from 'lucide-react';

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
  selectedTicker: string;
  currentPrice: number;
  onTrade: (trade: Trade) => void;
}

const newTradeId = (): string => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.floor(Math.random() * 1e9).toString(36)}`;
};

export const OrderForm: React.FC<OrderFormProps> = ({ selectedTicker, currentPrice, onTrade }) => {
  const [side, setSide] = useState<'buy' | 'sell'>('buy');
  const [quantity, setQuantity] = useState<number>(100);
  const [price, setPrice] = useState<string>('');
  const [orderType, setOrderType] = useState<'market' | 'limit' | 'stop'>('market');
  const [touchedPrice, setTouchedPrice] = useState(false);

  // Keep the limit/stop input echoing the live price until the user touches it.
  useEffect(() => {
    if (touchedPrice) return;
    if (Number.isFinite(currentPrice) && currentPrice > 0) {
      setPrice(currentPrice.toFixed(2));
    }
  }, [currentPrice, touchedPrice]);

  const hasLivePrice = Number.isFinite(currentPrice) && currentPrice > 0;
  const parsedLimit = parseFloat(price);
  const limitValid = Number.isFinite(parsedLimit) && parsedLimit > 0;
  const canSubmit =
    selectedTicker.length > 0 &&
    quantity > 0 &&
    (orderType === 'market' ? hasLivePrice : limitValid);

  // Validation error messages
  const validationErrors: string[] = [];
  if (!selectedTicker || selectedTicker.length === 0) validationErrors.push('Select a ticker');
  if (quantity <= 0) validationErrors.push('Quantity must be > 0');
  if (orderType !== 'market' && !limitValid) validationErrors.push('Enter a valid price > 0');
  if (orderType === 'market' && !hasLivePrice) validationErrors.push('No live price available');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;

    const tradePrice = orderType === 'market' ? currentPrice : parsedLimit;

    const trade: Trade = {
      id: newTradeId(),
      ticker: selectedTicker,
      side,
      entryPrice: tradePrice,
      quantity,
      entryTime: Math.floor(Date.now() / 1000),
      reason: `Manual ${orderType} order`,
    };

    onTrade(trade);

    setQuantity(100);
    setTouchedPrice(false);
    if (hasLivePrice) setPrice(currentPrice.toFixed(2));
  };

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
      <h3 className="text-lg font-semibold text-gray-100 mb-4 flex items-center gap-2">
        <Plus className="w-5 h-5" />
        Place Trade
      </h3>

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Ticker (read-only, locked to the dashboard's selection) */}
        <div>
          <label className="text-sm text-gray-400 mb-1 block">Ticker</label>
          <input
            type="text"
            value={selectedTicker}
            readOnly
            className="w-full bg-gray-900 text-white px-3 py-2 rounded border border-gray-700 text-sm cursor-not-allowed"
          />
          <p className="text-xs text-gray-500 mt-1">
            Switch the dashboard ticker to trade a different symbol.
          </p>
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
            onChange={(e) => {
              const v = parseInt(e.target.value, 10);
              setQuantity(Number.isFinite(v) ? v : 0);
            }}
            onBlur={() => setQuantity((q) => (q >= 1 ? q : 1))}
            min={1}
            step={1}
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500 text-sm"
          />
        </div>

        {/* Order Type */}
        <div>
          <label className="text-sm text-gray-400 mb-1 block">Order Type</label>
          <select
            value={orderType}
            onChange={(e) => setOrderType(e.target.value as 'market' | 'limit' | 'stop')}
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
              onChange={(e) => {
                setTouchedPrice(true);
                setPrice(e.target.value);
              }}
              min={0.01}
              step={0.01}
              className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500 text-sm"
            />
            {!limitValid && (
              <p className="text-xs text-red-400 mt-1">Enter a positive price.</p>
            )}
          </div>
        )}

        {/* Current Price Display */}
        <div className="bg-gray-900 p-3 rounded border border-gray-700">
          <div className="text-xs text-gray-400">Current Price</div>
          <div className="text-lg font-bold text-gray-100">
            {hasLivePrice ? `$${currentPrice.toFixed(2)}` : <span className="text-gray-500">waiting for quote…</span>}
          </div>
        </div>

        {/* Validation Errors */}
        {validationErrors.length > 0 && !canSubmit && (
          <div className="bg-red-900/20 border border-red-700 rounded p-2">
            <p className="text-xs text-red-400 font-medium">Cannot submit:</p>
            {validationErrors.map((err, i) => (
              <p key={i} className="text-xs text-red-300 mt-1">• {err}</p>
            ))}
          </div>
        )}

        {/* Submit */}
        <button
          type="submit"
          disabled={!canSubmit}
          className={`w-full py-2 rounded font-semibold transition ${
            !canSubmit
              ? 'bg-gray-700 text-gray-500 cursor-not-allowed'
              : side === 'buy'
              ? 'bg-green-600 hover:bg-green-700 text-white'
              : 'bg-red-600 hover:bg-red-700 text-white'
          }`}
        >
          {side === 'buy' ? 'Buy' : 'Sell'} {quantity} {selectedTicker}
        </button>
      </form>
    </div>
  );
};
