import React, { useState } from 'react';
import { AssetSelector, type Asset } from '../components/AssetSelector';
import { StrategyBuilder } from '../components/StrategyBuilder';
import { Chart } from '../components/Chart';
import { Indicators } from '../components/Indicators';

// Mock data for different assets
const MOCK_BARS = {
  stock: Array.from({ length: 100 }, (_, i) => ({
    timestamp: Date.now() - (100 - i) * 60000,
    open: 150 + Math.random() * 10,
    high: 155 + Math.random() * 10,
    low: 145 + Math.random() * 10,
    close: 150 + Math.random() * 10,
    volume: Math.random() * 1000000,
  })),
  future: Array.from({ length: 100 }, (_, i) => ({
    timestamp: Date.now() - (100 - i) * 60000,
    open: 4500 + Math.random() * 50,
    high: 4525 + Math.random() * 50,
    low: 4475 + Math.random() * 50,
    close: 4500 + Math.random() * 50,
    volume: Math.random() * 100000,
  })),
  forex: Array.from({ length: 100 }, (_, i) => ({
    timestamp: Date.now() - (100 - i) * 60000,
    open: 1.0890 + Math.random() * 0.01,
    high: 1.0905 + Math.random() * 0.01,
    low: 1.0875 + Math.random() * 0.01,
    close: 1.0890 + Math.random() * 0.01,
    volume: Math.random() * 10000,
  })),
  crypto: Array.from({ length: 100 }, (_, i) => ({
    timestamp: Date.now() - (100 - i) * 60000,
    open: 42500 + Math.random() * 500,
    high: 42750 + Math.random() * 500,
    low: 42250 + Math.random() * 500,
    close: 42500 + Math.random() * 500,
    volume: Math.random() * 50000,
  })),
};

export const MultiAsset: React.FC = () => {
  const [selectedAsset, setSelectedAsset] = useState<Asset>({
    id: 'aapl',
    symbol: 'AAPL',
    name: 'Apple',
    type: 'stock',
    exchange: 'NASDAQ',
    price: 150.25,
  });

  const [activeTab, setActiveTab] = useState<'chart' | 'strategy'>('chart');

  const mockBars = MOCK_BARS[selectedAsset.type];
  const mockIndicators = {
    timestamp: Date.now(),
    rsi_14: 50 + Math.random() * 20,
    rsi_7: 50 + Math.random() * 20,
    rsi_21: 50 + Math.random() * 20,
    macd_line: 0,
    macd_signal: 0,
    macd_histogram: 0,
    bb_upper_20: selectedAsset.price + 5,
    bb_middle_20: selectedAsset.price,
    bb_lower_20: selectedAsset.price - 5,
    bb_position: 0,
    atr_14: 1.5,
    atr_7: 1.2,
    obv: 1000000000,
    roc_5: 0.5,
    roc_10: 1.0,
    sma_20: selectedAsset.price,
    sma_50: selectedAsset.price - 2,
    sma_200: selectedAsset.price - 5,
    ema_12: selectedAsset.price + 0.5,
    ema_26: selectedAsset.price - 0.5,
  };

  return (
    <div className="space-y-6">
      {/* Asset Selector */}
      <div className="flex items-center gap-4 bg-gray-800 border border-gray-700 rounded-lg p-4">
        <div className="flex-1">
          <p className="text-sm text-gray-400 mb-2">Select Asset Class</p>
          <AssetSelector selectedAsset={selectedAsset} onAssetChange={setSelectedAsset} />
        </div>

        {/* Asset Info */}
        <div className="text-right">
          <div className="text-sm text-gray-400">Current Price</div>
          <div className="text-2xl font-bold text-white">
            {selectedAsset.type === 'forex' ? selectedAsset.price.toFixed(4) : selectedAsset.type === 'crypto' ? selectedAsset.price.toFixed(2) : '$' + selectedAsset.price.toFixed(2)}
          </div>
          <div className="text-xs text-gray-500 mt-1">{selectedAsset.exchange}</div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-2 border-b border-gray-700">
        {(['chart', 'strategy'] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`py-3 px-4 font-medium border-b-2 transition ${
              activeTab === tab
                ? 'border-blue-500 text-blue-400'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            {tab === 'chart' && '📊 Chart Analysis'}
            {tab === 'strategy' && '🔧 Strategy Builder'}
          </button>
        ))}
      </div>

      {/* Chart Tab */}
      {activeTab === 'chart' && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2">
            <Chart bars={mockBars} indicators={mockIndicators} ticker={selectedAsset.symbol} />
          </div>
          <div>
            <Indicators indicators={mockIndicators} />
          </div>
        </div>
      )}

      {/* Strategy Tab */}
      {activeTab === 'strategy' && (
        <StrategyBuilder
          onSave={(rule) => {
            console.log('Strategy saved:', rule);
          }}
          onTest={(rule) => {
            console.log('Testing strategy:', rule);
          }}
        />
      )}

      {/* Asset Class Info */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="bg-blue-900/20 border border-blue-700/30 rounded-lg p-4">
          <div className="text-2xl mb-2">📈</div>
          <h4 className="font-semibold text-blue-100 mb-1">Stocks</h4>
          <p className="text-xs text-blue-200">Equities from major exchanges. Options available.</p>
        </div>

        <div className="bg-yellow-900/20 border border-yellow-700/30 rounded-lg p-4">
          <div className="text-2xl mb-2">⚡</div>
          <h4 className="font-semibold text-yellow-100 mb-1">Futures</h4>
          <p className="text-xs text-yellow-200">Index, commodity, energy futures. 24-hour trading.</p>
        </div>

        <div className="bg-green-900/20 border border-green-700/30 rounded-lg p-4">
          <div className="text-2xl mb-2">💱</div>
          <h4 className="font-semibold text-green-100 mb-1">Forex</h4>
          <p className="text-xs text-green-200">Currency pairs. Highest liquidity, 24/5 trading.</p>
        </div>

        <div className="bg-orange-900/20 border border-orange-700/30 rounded-lg p-4">
          <div className="text-2xl mb-2">🪙</div>
          <h4 className="font-semibold text-orange-100 mb-1">Crypto</h4>
          <p className="text-xs text-orange-200">Bitcoin, Ethereum, altcoins. 24/7 volatility.</p>
        </div>
      </div>

      {/* Comparison Table */}
      <div className="bg-gray-800 border border-gray-700 rounded-lg overflow-hidden">
        <div className="p-4 border-b border-gray-700">
          <h3 className="text-lg font-semibold text-gray-100">Asset Class Comparison</h3>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-900 border-b border-gray-700">
                <th className="text-left p-4 text-gray-400">Feature</th>
                <th className="text-center p-4 text-gray-400">Stocks</th>
                <th className="text-center p-4 text-gray-400">Futures</th>
                <th className="text-center p-4 text-gray-400">Forex</th>
                <th className="text-center p-4 text-gray-400">Crypto</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b border-gray-700 hover:bg-gray-700/50">
                <td className="p-4 text-gray-300">Leverage</td>
                <td className="text-center p-4">1-4x</td>
                <td className="text-center p-4">10-50x</td>
                <td className="text-center p-4">50-100x</td>
                <td className="text-center p-4">Unrestricted</td>
              </tr>
              <tr className="border-b border-gray-700 hover:bg-gray-700/50">
                <td className="p-4 text-gray-300">Trading Hours</td>
                <td className="text-center p-4">9:30-16:00 EST</td>
                <td className="text-center p-4">24 hours</td>
                <td className="text-center p-4">24/5</td>
                <td className="text-center p-4">24/7</td>
              </tr>
              <tr className="border-b border-gray-700 hover:bg-gray-700/50">
                <td className="p-4 text-gray-300">Volatility</td>
                <td className="text-center p-4">Low-Medium</td>
                <td className="text-center p-4">Medium-High</td>
                <td className="text-center p-4">Low</td>
                <td className="text-center p-4">Very High</td>
              </tr>
              <tr className="border-b border-gray-700 hover:bg-gray-700/50">
                <td className="p-4 text-gray-300">Fees</td>
                <td className="text-center p-4">0.001-0.1%</td>
                <td className="text-center p-4">0.01-0.05%</td>
                <td className="text-center p-4">0.001-0.01%</td>
                <td className="text-center p-4">0.05-0.5%</td>
              </tr>
              <tr className="hover:bg-gray-700/50">
                <td className="p-4 text-gray-300">Regulation</td>
                <td className="text-center p-4">🟢 Heavy</td>
                <td className="text-center p-4">🟢 Heavy</td>
                <td className="text-center p-4">🟡 Light</td>
                <td className="text-center p-4">🔴 Minimal</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};
