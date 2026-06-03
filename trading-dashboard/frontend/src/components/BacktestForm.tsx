import React, { useState, useCallback } from 'react';
import { Play, Settings } from 'lucide-react';

interface BacktestFormProps {
  onSubmit: (params: BacktestParams) => void;
  loading: boolean;
}

export interface BacktestParams {
  ticker: string;
  strategy: string;
  days: number;
  initial_capital: number;
  position_size_pct: number;
  rsi_oversold: number;
  rsi_overbought: number;
  rsi_period: number;
  fast_period: number;
  slow_period: number;
}

const BacktestFormComponent: React.FC<BacktestFormProps> = ({ onSubmit, loading }) => {
  const [params, setParams] = useState<BacktestParams>({
    ticker: 'AAPL',
    strategy: 'rsi',
    days: 30,
    initial_capital: 100000,
    position_size_pct: 0.1,
    rsi_oversold: 30,
    rsi_overbought: 70,
    rsi_period: 14,
    fast_period: 12,
    slow_period: 26,
  });

  const handleChange = useCallback((key: keyof BacktestParams, value: any) => {
    setParams((prev) => ({ ...prev, [key]: value }));
  }, []);

  const handleSubmit = useCallback((e: React.FormEvent) => {
    e.preventDefault();
    onSubmit(params);
  }, [params, onSubmit]);

  return (
    <form onSubmit={handleSubmit} className="bg-gray-800 border border-gray-700 rounded-lg p-4 space-y-4">
      <div className="flex items-center gap-2 mb-4">
        <Settings className="w-5 h-5 text-blue-400" />
        <h3 className="text-lg font-semibold text-gray-100">Backtest Configuration</h3>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Ticker */}
        <div>
          <label className="block text-sm font-medium text-gray-400 mb-1">Ticker</label>
          <input
            type="text"
            value={params.ticker}
            onChange={(e) => handleChange('ticker', e.target.value.toUpperCase())}
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500"
            placeholder="AAPL"
            maxLength={5}
          />
        </div>

        {/* Strategy */}
        <div>
          <label className="block text-sm font-medium text-gray-400 mb-1">Strategy</label>
          <select
            value={params.strategy}
            onChange={(e) => handleChange('strategy', e.target.value)}
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500"
          >
            <option value="rsi">RSI Reversal</option>
            <option value="ma_crossover">MA Crossover</option>
          </select>
        </div>

        {/* Days */}
        <div>
          <label className="block text-sm font-medium text-gray-400 mb-1">Days of History</label>
          <input
            type="number"
            value={params.days}
            onChange={(e) => handleChange('days', parseInt(e.target.value))}
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500"
            min={1}
            max={365}
          />
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Initial Capital */}
        <div>
          <label className="block text-sm font-medium text-gray-400 mb-1">Initial Capital</label>
          <div className="relative">
            <span className="absolute left-3 top-2.5 text-gray-400">$</span>
            <input
              type="number"
              value={params.initial_capital}
              onChange={(e) => handleChange('initial_capital', parseInt(e.target.value))}
              className="w-full bg-gray-700 text-white px-3 py-2 pl-7 rounded border border-gray-600 focus:outline-none focus:border-blue-500"
              min={1000}
              step={10000}
            />
          </div>
        </div>

        {/* Position Size */}
        <div>
          <label className="block text-sm font-medium text-gray-400 mb-1">Position Size %</label>
          <input
            type="number"
            value={params.position_size_pct * 100}
            onChange={(e) => handleChange('position_size_pct', parseInt(e.target.value) / 100)}
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500"
            min={1}
            max={100}
            step={5}
          />
        </div>

        {/* RSI Period */}
        {params.strategy === 'rsi' && (
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">RSI Period</label>
            <input
              type="number"
              value={params.rsi_period}
              onChange={(e) => handleChange('rsi_period', parseInt(e.target.value))}
              className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500"
              min={2}
              max={100}
            />
          </div>
        )}
      </div>

      {/* RSI Parameters */}
      {params.strategy === 'rsi' && (
        <div className="grid grid-cols-2 gap-4 p-3 bg-gray-700 rounded border border-gray-600">
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">Oversold Level (Buy)</label>
            <input
              type="number"
              value={params.rsi_oversold}
              onChange={(e) => handleChange('rsi_oversold', parseInt(e.target.value))}
              className="w-full bg-gray-600 text-white px-3 py-2 rounded border border-gray-500 focus:outline-none focus:border-blue-500"
              min={0}
              max={100}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">Overbought Level (Sell)</label>
            <input
              type="number"
              value={params.rsi_overbought}
              onChange={(e) => handleChange('rsi_overbought', parseInt(e.target.value))}
              className="w-full bg-gray-600 text-white px-3 py-2 rounded border border-gray-500 focus:outline-none focus:border-blue-500"
              min={0}
              max={100}
            />
          </div>
        </div>
      )}

      {/* MA Crossover Parameters */}
      {params.strategy === 'ma_crossover' && (
        <div className="grid grid-cols-2 gap-4 p-3 bg-gray-700 rounded border border-gray-600">
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">Fast MA Period</label>
            <input
              type="number"
              value={params.fast_period}
              onChange={(e) => handleChange('fast_period', parseInt(e.target.value))}
              className="w-full bg-gray-600 text-white px-3 py-2 rounded border border-gray-500 focus:outline-none focus:border-blue-500"
              min={2}
              max={100}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">Slow MA Period</label>
            <input
              type="number"
              value={params.slow_period}
              onChange={(e) => handleChange('slow_period', parseInt(e.target.value))}
              className="w-full bg-gray-600 text-white px-3 py-2 rounded border border-gray-500 focus:outline-none focus:border-blue-500"
              min={2}
              max={200}
            />
          </div>
        </div>
      )}

      <button
        type="submit"
        disabled={loading}
        className="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 text-white font-semibold py-2 px-4 rounded flex items-center justify-center gap-2 transition"
      >
        <Play className="w-5 h-5" />
        {loading ? 'Running Backtest...' : 'Run Backtest'}
      </button>
    </form>
  );
};

export const BacktestForm = React.memo(BacktestFormComponent);
