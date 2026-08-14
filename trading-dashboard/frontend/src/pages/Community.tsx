import React, { useState } from 'react';
import { Leaderboard } from '../components/Leaderboard';
import { Users, TrendingUp, Award } from 'lucide-react';

interface CopiedTrader {
  username: string;
  copiedAt: number;
  trades: number;
  returnPct: number;
}

export const Community: React.FC = () => {
  const [copiedTraders, setCopiedTraders] = useState<CopiedTrader[]>([
    {
      username: 'TrendKing',
      copiedAt: Math.floor(Date.now() / 1000) - 3600,
      trades: 5,
      returnPct: 12.3,
    },
  ]);

  const handleCopyTrader = (username: string) => {
    const existing = copiedTraders.find((t) => t.username === username);
    if (!existing) {
      setCopiedTraders([
        ...copiedTraders,
        {
          username,
          copiedAt: Math.floor(Date.now() / 1000),
          trades: 0,
          returnPct: 0,
        },
      ]);
    }
  };

  const handleUnfollow = (username: string) => {
    setCopiedTraders(copiedTraders.filter((t) => t.username !== username));
  };

  return (
    <div className="space-y-6">
      {/* Header Stats */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-gradient-to-br from-blue-900 to-blue-800 border border-blue-700 rounded-lg p-6">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm text-blue-200 mb-1">Total Community</div>
              <div className="text-3xl font-bold text-white">12,547</div>
              <div className="text-xs text-blue-300 mt-2">Active traders</div>
            </div>
            <Users className="w-12 h-12 text-blue-400 opacity-20" />
          </div>
        </div>

        <div className="bg-gradient-to-br from-green-900 to-green-800 border border-green-700 rounded-lg p-6">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm text-green-200 mb-1">Top Performer</div>
              <div className="text-3xl font-bold text-white">47.3%</div>
              <div className="text-xs text-green-300 mt-2">Monthly return</div>
            </div>
            <TrendingUp className="w-12 h-12 text-green-400 opacity-20" />
          </div>
        </div>

        <div className="bg-gradient-to-br from-purple-900 to-purple-800 border border-purple-700 rounded-lg p-6">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm text-purple-200 mb-1">You're Following</div>
              <div className="text-3xl font-bold text-white">{copiedTraders.length}</div>
              <div className="text-xs text-purple-300 mt-2">Copy-trading strategies</div>
            </div>
            <Award className="w-12 h-12 text-purple-400 opacity-20" />
          </div>
        </div>
      </div>

      {/* Leaderboard */}
      <Leaderboard onCopy={handleCopyTrader} />

      {/* Your Followed Traders */}
      {copiedTraders.length > 0 && (
        <div className="bg-gray-800 border border-gray-700 rounded-lg overflow-hidden">
          <div className="p-4 border-b border-gray-700">
            <h3 className="text-lg font-semibold text-gray-100">Traders You're Following ({copiedTraders.length})</h3>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-700 bg-gray-900">
                  <th className="text-left py-3 px-4 text-gray-400">Trader</th>
                  <th className="text-right py-3 px-4 text-gray-400">Followed</th>
                  <th className="text-right py-3 px-4 text-gray-400">Synced Trades</th>
                  <th className="text-right py-3 px-4 text-gray-400">Sync P&L</th>
                  <th className="text-center py-3 px-4 text-gray-400">Action</th>
                </tr>
              </thead>
              <tbody>
                {copiedTraders.map((trader) => {
                  const followedAgo = Math.floor((Date.now() / 1000 - trader.copiedAt) / 60);
                  const isProfit = trader.returnPct >= 0;

                  return (
                    <tr key={trader.username} className="border-b border-gray-700 hover:bg-gray-700/50">
                      <td className="py-3 px-4 text-gray-100 font-semibold">{trader.username}</td>
                      <td className="text-right py-3 px-4 text-gray-400 text-xs">
                        {followedAgo < 60 ? `${followedAgo}m ago` : `${Math.floor(followedAgo / 60)}h ago`}
                      </td>
                      <td className="text-right py-3 px-4 text-gray-300">{trader.trades}</td>
                      <td className={`text-right py-3 px-4 font-semibold ${isProfit ? 'text-green-400' : 'text-red-400'}`}>
                        {isProfit ? '+' : ''}
                        {trader.returnPct.toFixed(2)}%
                      </td>
                      <td className="text-center py-3 px-4">
                        <button
                          onClick={() => handleUnfollow(trader.username)}
                          className="text-red-400 hover:text-red-300 text-sm font-medium transition"
                        >
                          Unfollow
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* How It Works */}
      <div className="bg-gray-800 border border-gray-700 rounded-lg p-6">
        <h3 className="text-lg font-semibold text-gray-100 mb-4">How Copy-Trading Works</h3>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div>
            <div className="bg-blue-900/30 w-10 h-10 rounded-lg flex items-center justify-center mb-3 text-blue-400 font-bold">1</div>
            <h4 className="font-semibold text-gray-100 mb-2">Browse Traders</h4>
            <p className="text-gray-400 text-sm">Find top performers on the leaderboard sorted by return, Sharpe ratio, and win rate.</p>
          </div>
          <div>
            <div className="bg-blue-900/30 w-10 h-10 rounded-lg flex items-center justify-center mb-3 text-blue-400 font-bold">2</div>
            <h4 className="font-semibold text-gray-100 mb-2">Click Copy</h4>
            <p className="text-gray-400 text-sm">Follow their strategy and automatically mirror their trades in your simulated account.</p>
          </div>
          <div>
            <div className="bg-blue-900/30 w-10 h-10 rounded-lg flex items-center justify-center mb-3 text-blue-400 font-bold">3</div>
            <h4 className="font-semibold text-gray-100 mb-2">Track Results</h4>
            <p className="text-gray-400 text-sm">Watch your synced trades in real-time and see if you want to trade live with them.</p>
          </div>
        </div>
      </div>
    </div>
  );
};
