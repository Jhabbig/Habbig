import React, { useState, useEffect } from 'react';
import { TrendingUp, TrendingDown, AlertCircle, ChevronDown } from 'lucide-react';

export interface SignalData {
  timestamp: number;
  ticker: string;
  signal: string;
  confidence: number;
  price: number;
  reasoning: string[];
}

interface SignalPanelProps {
  ticker: string;
  price: number;
  indicators: any;
}

export const SignalPanel: React.FC<SignalPanelProps> = ({ ticker, price, indicators }) => {
  const [signal, setSignal] = useState<SignalData | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!indicators) return;

    const fetchSignal = async () => {
      setLoading(true);
      try {
        const response = await fetch('/api/signals', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ticker,
            price,
            rsi_14: indicators.rsi_14 || 50,
            rsi_7: indicators.rsi_7 || 50,
            rsi_21: indicators.rsi_21 || 50,
            macd_line: indicators.macd_line || 0,
            macd_signal: indicators.macd_signal || 0,
            macd_histogram: indicators.macd_histogram || 0,
            bb_upper_20: indicators.bb_upper_20 || price + 5,
            bb_middle_20: indicators.bb_middle_20 || price,
            bb_lower_20: indicators.bb_lower_20 || price - 5,
            bb_position: indicators.bb_position || 0,
            atr_14: indicators.atr_14 || 1,
            atr_7: indicators.atr_7 || 1,
            obv: indicators.obv || 0,
            roc_5: indicators.roc_5 || 0,
            roc_10: indicators.roc_10 || 0,
            sma_20: indicators.sma_20 || price,
            sma_50: indicators.sma_50 || price,
            sma_200: indicators.sma_200 || price,
            ema_12: indicators.ema_12 || price,
            ema_26: indicators.ema_26 || price,
          }),
        });

        if (response.ok) {
          const data = await response.json();
          setSignal(data);
        }
      } catch (err) {
        console.error('Error fetching signal:', err);
      } finally {
        setLoading(false);
      }
    };

    fetchSignal();
  }, [ticker, price, indicators]);

  if (!signal) {
    return null;
  }

  const isBuy = signal.signal === 'BUY';
  const isSell = signal.signal === 'SELL';
  const isHold = signal.signal === 'HOLD';

  const bgColor = isBuy ? 'bg-green-900' : isSell ? 'bg-red-900' : 'bg-gray-800';
  const borderColor = isBuy ? 'border-green-700' : isSell ? 'border-red-700' : 'border-gray-700';
  const textColor = isBuy ? 'text-green-100' : isSell ? 'text-red-100' : 'text-gray-100';
  const badgeColor = isBuy ? 'bg-green-600' : isSell ? 'bg-red-600' : 'bg-gray-600';
  const badgeTextColor = isBuy ? 'text-green-100' : isSell ? 'text-red-100' : 'text-gray-100';

  return (
    <div className={`${bgColor} border ${borderColor} rounded-lg overflow-hidden`}>
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full p-4 flex items-center justify-between hover:opacity-90 transition"
      >
        <div className="flex items-center gap-3">
          <div className={`${badgeColor} ${badgeTextColor} px-4 py-2 rounded-lg font-bold text-lg`}>
            {signal.signal}
          </div>
          <div className={textColor}>
            <div className="text-sm font-medium text-gray-400">Confidence</div>
            <div className="text-2xl font-bold">{signal.confidence.toFixed(0)}%</div>
          </div>
        </div>
        <ChevronDown className={`w-5 h-5 transition ${expanded ? 'rotate-180' : ''}`} />
      </button>

      {/* Expanded Content */}
      {expanded && (
        <div className="border-t border-gray-700 p-4 space-y-3">
          <div>
            <div className="text-xs text-gray-400 mb-2 font-medium">SIGNAL REASONING</div>
            <div className="space-y-2">
              {signal.reasoning.map((reason, idx) => (
                <div key={idx} className="flex gap-2 text-xs">
                  <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-yellow-400" />
                  <span className="text-gray-300">{reason}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 text-xs">
            <div className="bg-gray-900 p-2 rounded">
              <div className="text-gray-400">Price</div>
              <div className="text-gray-100 font-semibold">${signal.price.toFixed(2)}</div>
            </div>
            <div className="bg-gray-900 p-2 rounded">
              <div className="text-gray-400">Timestamp</div>
              <div className="text-gray-100 font-semibold">
                {new Date(signal.timestamp * 1000).toLocaleTimeString()}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
