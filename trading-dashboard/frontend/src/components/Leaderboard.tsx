import React, { useState } from 'react';
import { Trophy, Copy, TrendingUp } from 'lucide-react';

export interface LeaderboardEntry {
  rank: number;
  username: string;
  returnPct: number;
  sharpeRatio: number;
  winRate: number;
  totalTrades: number;
  trades: number;
  followers: number;
}

interface LeaderboardProps {
  onCopy?: (username: string) => void;
}

export const Leaderboard: React.FC<LeaderboardProps> = ({ onCopy }) => {
  const [timeframe, setTimeframe] = useState<'week' | 'month' | 'all'>('month');

  // Mock data
  const leaders: LeaderboardEntry[] = [
    {
      rank: 1,
      username: 'TrendKing',
      returnPct: 47.3,
      sharpeRatio: 2.15,
      winRate: 68.5,
      totalTrades: 142,
      trades: 5230,
      followers: 3421,
    },
    {
      rank: 2,
      username: 'VolatilityHunter',
      returnPct: 42.1,
      sharpeRatio: 1.89,
      winRate: 62.3,
      totalTrades: 118,
      trades: 4120,
      followers: 2841,
    },
    {
      rank: 3,
      username: 'MomentumMaster',
      returnPct: 38.7,
      sharpeRatio: 1.76,
      winRate: 59.8,
      totalTrades: 156,
      trades: 3920,
      followers: 2156,
    },
    {
      rank: 4,
      username: 'ReveralSpecialist',
      returnPct: 35.2,
      sharpeRatio: 1.54,
      winRate: 57.2,
      totalTrades: 98,
      trades: 3450,
      followers: 1843,
    },
    {
      rank: 5,
      username: 'OptionsGenius',
      returnPct: 31.5,
      sharpeRatio: 1.42,
      winRate: 54.1,
      totalTrades: 127,
      trades: 2980,
      followers: 1625,
    },
    {
      rank: 6,
      username: 'SwingTraderPro',
      returnPct: 28.9,
      sharpeRatio: 1.31,
      winRate: 51.7,
      totalTrades: 145,
      trades: 2750,
      followers: 1402,
    },
    {
      rank: 7,
      username: 'DayTradeQueen',
      returnPct: 26.3,
      sharpeRatio: 1.18,
      winRate: 49.2,
      totalTrades: 213,
      trades: 2540,
      followers: 1189,
    },
    {
      rank: 8,
      username: 'ValueInvestor',
      returnPct: 23.7,
      sharpeRatio: 1.05,
      winRate: 46.5,
      totalTrades: 82,
      trades: 2320,
      followers: 987,
    },
    {
      rank: 9,
      username: 'TechFocused',
      returnPct: 21.2,
      sharpeRatio: 0.92,
      winRate: 44.1,
      totalTrades: 156,
      trades: 2100,
      followers: 845,
    },
    {
      rank: 10,
      username: 'Diversified',
      returnPct: 18.5,
      sharpeRatio: 0.78,
      winRate: 41.3,
      totalTrades: 234,
      trades: 1870,
      followers: 723,
    },
  ];

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="p-4 border-b border-gray-700">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
            <Trophy className="w-6 h-6 text-yellow-400" />
            Leaderboard
          </h2>
        </div>

        {/* Timeframe Filter */}
        <div className="flex gap-2">
          {(['week', 'month', 'all'] as const).map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              className={`px-3 py-1 rounded text-sm font-medium transition ${
                timeframe === tf
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-700 text-gray-400 hover:text-gray-200'
              }`}
            >
              {tf === 'week' ? 'This Week' : tf === 'month' ? 'This Month' : 'All Time'}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-gray-700 bg-gray-900">
              <th className="text-left py-3 px-4 text-gray-400 font-medium">Rank</th>
              <th className="text-left py-3 px-4 text-gray-400 font-medium">Trader</th>
              <th className="text-right py-3 px-4 text-gray-400 font-medium">Return %</th>
              <th className="text-right py-3 px-4 text-gray-400 font-medium">Sharpe</th>
              <th className="text-right py-3 px-4 text-gray-400 font-medium">Win Rate</th>
              <th className="text-right py-3 px-4 text-gray-400 font-medium">Trades</th>
              <th className="text-right py-3 px-4 text-gray-400 font-medium">Followers</th>
              <th className="text-center py-3 px-4 text-gray-400 font-medium">Action</th>
            </tr>
          </thead>
          <tbody>
            {leaders.map((leader) => (
              <tr key={leader.rank} className="border-b border-gray-700 hover:bg-gray-700/50 transition">
                <td className="py-3 px-4">
                  <div className="flex items-center gap-2">
                    {leader.rank <= 3 && (
                      <div
                        className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold ${
                          leader.rank === 1
                            ? 'bg-yellow-600 text-yellow-100'
                            : leader.rank === 2
                            ? 'bg-gray-400 text-gray-900'
                            : 'bg-orange-600 text-orange-100'
                        }`}
                      >
                        {leader.rank}
                      </div>
                    )}
                    {leader.rank > 3 && <div className="text-gray-400 font-semibold">{leader.rank}</div>}
                  </div>
                </td>
                <td className="py-3 px-4 text-gray-100 font-semibold">{leader.username}</td>
                <td className="text-right py-3 px-4">
                  <span className="text-green-400 font-bold">{leader.returnPct.toFixed(1)}%</span>
                </td>
                <td className="text-right py-3 px-4 text-blue-400 font-semibold">{leader.sharpeRatio.toFixed(2)}</td>
                <td className="text-right py-3 px-4 text-purple-400 font-semibold">{leader.winRate.toFixed(1)}%</td>
                <td className="text-right py-3 px-4 text-gray-300">{leader.totalTrades}</td>
                <td className="text-right py-3 px-4 text-gray-300">{leader.followers.toLocaleString()}</td>
                <td className="text-center py-3 px-4">
                  <button
                    onClick={() => onCopy?.(leader.username)}
                    className="inline-flex items-center gap-1 bg-blue-600/20 hover:bg-blue-600/40 text-blue-400 px-2 py-1 rounded text-xs font-medium transition"
                    title="Copy trading strategy"
                  >
                    <Copy className="w-3 h-3" />
                    Copy
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Footer */}
      <div className="bg-gray-900/50 p-4 border-t border-gray-700">
        <div className="flex items-center gap-2 text-sm text-gray-400">
          <TrendingUp className="w-4 h-4" />
          <span>Results show {timeframe === 'week' ? 'this week' : timeframe === 'month' ? 'this month' : 'all-time'} performance. Copy any strategy to simulate following their trades.</span>
        </div>
      </div>
    </div>
  );
};
