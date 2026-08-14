import React, { useState } from 'react';
import { ChevronDown, TrendingUp, Zap, Coins } from 'lucide-react';

export interface Asset {
  id: string;
  symbol: string;
  name: string;
  type: 'stock' | 'future' | 'forex' | 'crypto';
  exchange: string;
  price: number;
}

interface AssetSelectorProps {
  selectedAsset: Asset;
  onAssetChange: (asset: Asset) => void;
}

const ASSETS: Asset[] = [
  // Stocks
  { id: 'aapl', symbol: 'AAPL', name: 'Apple', type: 'stock', exchange: 'NASDAQ', price: 150.25 },
  { id: 'tsla', symbol: 'TSLA', name: 'Tesla', type: 'stock', exchange: 'NASDAQ', price: 175.80 },
  { id: 'msft', symbol: 'MSFT', name: 'Microsoft', type: 'stock', exchange: 'NASDAQ', price: 380.50 },
  { id: 'spy', symbol: 'SPY', name: 'S&P 500 ETF', type: 'stock', exchange: 'NYSE', price: 450.30 },

  // Futures
  { id: 'es', symbol: 'ES', name: 'E-mini S&P 500', type: 'future', exchange: 'CME', price: 4505.25 },
  { id: 'nq', symbol: 'NQ', name: 'E-mini Nasdaq 100', type: 'future', exchange: 'CME', price: 15850.75 },
  { id: 'gc', symbol: 'GC', name: 'Gold Futures', type: 'future', exchange: 'COMEX', price: 2045.30 },
  { id: 'cl', symbol: 'CL', name: 'Crude Oil', type: 'future', exchange: 'NYMEX', price: 78.45 },

  // Forex
  { id: 'eurusd', symbol: 'EURUSD', name: 'Euro/Dollar', type: 'forex', exchange: 'FX', price: 1.0895 },
  { id: 'gbpusd', symbol: 'GBPUSD', name: 'Pound/Dollar', type: 'forex', exchange: 'FX', price: 1.2650 },
  { id: 'usdjpy', symbol: 'USDJPY', name: 'Dollar/Yen', type: 'forex', exchange: 'FX', price: 148.50 },
  { id: 'audusd', symbol: 'AUDUSD', name: 'Aussie/Dollar', type: 'forex', exchange: 'FX', price: 0.6750 },

  // Crypto
  { id: 'btc', symbol: 'BTC', name: 'Bitcoin', type: 'crypto', exchange: 'Crypto', price: 42500.00 },
  { id: 'eth', symbol: 'ETH', name: 'Ethereum', type: 'crypto', exchange: 'Crypto', price: 2250.50 },
  { id: 'sol', symbol: 'SOL', name: 'Solana', type: 'crypto', exchange: 'Crypto', price: 98.75 },
  { id: 'ada', symbol: 'ADA', name: 'Cardano', type: 'crypto', exchange: 'Crypto', price: 0.82 },
];

export const AssetSelector: React.FC<AssetSelectorProps> = ({ selectedAsset, onAssetChange }) => {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');

  const filteredAssets = ASSETS.filter(
    (asset) =>
      asset.symbol.toLowerCase().includes(search.toLowerCase()) ||
      asset.name.toLowerCase().includes(search.toLowerCase())
  );

  const groupedAssets = {
    stock: filteredAssets.filter((a) => a.type === 'stock'),
    future: filteredAssets.filter((a) => a.type === 'future'),
    forex: filteredAssets.filter((a) => a.type === 'forex'),
    crypto: filteredAssets.filter((a) => a.type === 'crypto'),
  };

  const getTypeIcon = (type: string) => {
    switch (type) {
      case 'stock':
        return <TrendingUp className="w-4 h-4 text-blue-400" />;
      case 'future':
        return <Zap className="w-4 h-4 text-yellow-400" />;
      case 'forex':
        return <div className="w-4 h-4 text-green-400 font-bold text-xs">FX</div>;
      case 'crypto':
        return <Coins className="w-4 h-4 text-orange-400" />;
    }
  };

  const getTypeBadge = (type: string) => {
    const colors = {
      stock: 'bg-blue-900/30 text-blue-300',
      future: 'bg-yellow-900/30 text-yellow-300',
      forex: 'bg-green-900/30 text-green-300',
      crypto: 'bg-orange-900/30 text-orange-300',
    };
    return colors[type as keyof typeof colors] || colors.stock;
  };

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 text-white px-3 py-2 rounded border border-gray-600 transition"
      >
        <div className="flex items-center gap-2 flex-1">
          {getTypeIcon(selectedAsset.type)}
          <div className="text-left">
            <div className="font-semibold text-sm">{selectedAsset.symbol}</div>
            <div className="text-xs text-gray-400">${selectedAsset.price.toFixed(2)}</div>
          </div>
        </div>
        <ChevronDown className={`w-4 h-4 transition ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-gray-800 border border-gray-700 rounded-lg shadow-lg z-50 w-64">
          {/* Search */}
          <div className="p-3 border-b border-gray-700">
            <input
              type="text"
              placeholder="Search assets..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full bg-gray-700 text-white px-3 py-2 rounded text-sm focus:outline-none focus:border-blue-500"
              autoFocus
            />
          </div>

          {/* Asset Groups */}
          <div className="max-h-96 overflow-y-auto">
            {Object.entries(groupedAssets).map(([type, assets]) => {
              if (assets.length === 0) return null;

              return (
                <div key={type}>
                  <div className="px-3 py-2 text-xs font-semibold text-gray-400 uppercase bg-gray-900/50 border-t border-gray-700">
                    {type === 'stock' && '📈 Stocks'}
                    {type === 'future' && '⚡ Futures'}
                    {type === 'forex' && '💱 Forex'}
                    {type === 'crypto' && '🪙 Crypto'}
                  </div>

                  {assets.map((asset) => (
                    <button
                      key={asset.id}
                      onClick={() => {
                        onAssetChange(asset);
                        setOpen(false);
                        setSearch('');
                      }}
                      className={`w-full text-left px-3 py-2 hover:bg-gray-700/50 transition border-b border-gray-700/50 ${
                        selectedAsset.id === asset.id ? 'bg-blue-900/30' : ''
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2 flex-1">
                          {getTypeIcon(asset.type)}
                          <div>
                            <div className="text-sm font-semibold text-gray-100">{asset.symbol}</div>
                            <div className="text-xs text-gray-500">{asset.name}</div>
                          </div>
                        </div>
                        <span className={`text-xs px-2 py-1 rounded ${getTypeBadge(asset.type)}`}>
                          {asset.exchange}
                        </span>
                      </div>
                      <div className="text-xs text-gray-400 mt-1">${asset.price.toFixed(asset.type === 'forex' ? 4 : 2)}</div>
                    </button>
                  ))}
                </div>
              );
            })}
          </div>

          {/* Footer */}
          <div className="p-3 border-t border-gray-700 text-xs text-gray-500">
            {ASSETS.length} assets • Multi-asset analysis
          </div>
        </div>
      )}
    </div>
  );
};
