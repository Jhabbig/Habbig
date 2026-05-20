import React from 'react';
import { IndicatorValues } from '../types';
import { TrendingUp, TrendingDown } from 'lucide-react';

interface IndicatorsProps {
  indicators: IndicatorValues | null;
}

export const Indicators: React.FC<IndicatorsProps> = ({ indicators }) => {
  if (!indicators) {
    return (
      <div className="text-gray-400 text-sm p-4">Waiting for indicator data...</div>
    );
  }

  const rsiColor = indicators.rsi_14 > 70 ? 'text-red-400' : indicators.rsi_14 < 30 ? 'text-green-400' : 'text-gray-300';
  const macdColor = indicators.macd_histogram > 0 ? 'text-green-400' : 'text-red-400';

  return (
    <div className="space-y-4 p-4 bg-gray-900 rounded-lg border border-gray-700">
      <h3 className="text-lg font-semibold text-gray-100">Indicators</h3>

      <div className="grid grid-cols-2 gap-4">
        {/* RSI */}
        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <p className="text-gray-400 text-xs mb-1">RSI(14)</p>
          <p className={`text-lg font-bold ${rsiColor}`}>
            {indicators.rsi_14.toFixed(2)}
          </p>
          <div className="text-xs text-gray-500 mt-1">
            {indicators.rsi_14 > 70 ? 'Overbought' : indicators.rsi_14 < 30 ? 'Oversold' : 'Neutral'}
          </div>
        </div>

        {/* MACD */}
        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <p className="text-gray-400 text-xs mb-1">MACD</p>
          <p className={`text-lg font-bold ${macdColor}`}>
            {indicators.macd_histogram.toFixed(4)}
          </p>
          <div className="text-xs text-gray-500 mt-1">
            {indicators.macd_histogram > 0 ? 'Bullish' : 'Bearish'}
          </div>
        </div>

        {/* Bollinger Bands Position */}
        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <p className="text-gray-400 text-xs mb-1">BB Position</p>
          <p className="text-lg font-bold text-blue-400">
            {(indicators.bb_position * 100).toFixed(0)}%
          </p>
          <div className="text-xs text-gray-500 mt-1">
            {indicators.bb_position > 0.5 ? 'Upper' : indicators.bb_position < -0.5 ? 'Lower' : 'Middle'}
          </div>
        </div>

        {/* ATR */}
        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <p className="text-gray-400 text-xs mb-1">ATR(14)</p>
          <p className="text-lg font-bold text-yellow-400">
            {indicators.atr_14.toFixed(2)}
          </p>
          <div className="text-xs text-gray-500 mt-1">Volatility</div>
        </div>

        {/* ROC */}
        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <p className="text-gray-400 text-xs mb-1">ROC(5)</p>
          <p className={`text-lg font-bold ${indicators.roc_5 > 0 ? 'text-green-400' : 'text-red-400'}`}>
            {indicators.roc_5.toFixed(2)}%
          </p>
        </div>

        {/* OBV */}
        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <p className="text-gray-400 text-xs mb-1">OBV</p>
          <p className="text-lg font-bold text-indigo-400">
            {(indicators.obv / 1e6).toFixed(1)}M
          </p>
        </div>

        {/* SMA(20) */}
        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <p className="text-gray-400 text-xs mb-1">SMA(20)</p>
          <p className="text-lg font-bold text-amber-400">
            {indicators.sma_20.toFixed(2)}
          </p>
        </div>

        {/* EMA(12) */}
        <div className="bg-gray-800 p-3 rounded border border-gray-700">
          <p className="text-gray-400 text-xs mb-1">EMA(12)</p>
          <p className="text-lg font-bold text-pink-400">
            {indicators.ema_12.toFixed(2)}
          </p>
        </div>
      </div>

      {/* Detailed Info */}
      <div className="text-xs text-gray-500 space-y-1 pt-3 border-t border-gray-700">
        <div className="flex justify-between">
          <span>RSI(7)</span>
          <span className="text-gray-300">{indicators.rsi_7.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span>RSI(21)</span>
          <span className="text-gray-300">{indicators.rsi_21.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span>MACD Line</span>
          <span className="text-gray-300">{indicators.macd_line.toFixed(4)}</span>
        </div>
        <div className="flex justify-between">
          <span>MACD Signal</span>
          <span className="text-gray-300">{indicators.macd_signal.toFixed(4)}</span>
        </div>
      </div>
    </div>
  );
};
