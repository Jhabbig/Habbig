import React, { useState } from 'react';
import { GreeksSurface3D } from '../components/GreeksSurface3D';
import { IVTermStructure } from '../components/IVTermStructure';
import { SkewAnalysis } from '../components/SkewAnalysis';

export const AdvancedGreeks: React.FC = () => {
  const [selectedTab, setSelectedTab] = useState<'surface' | 'termstructure' | 'skew'>('surface');
  const spotPrice = 150;

  return (
    <div className="space-y-6">
      {/* Tabs */}
      <div className="flex gap-2 border-b border-gray-700 overflow-x-auto">
        {[
          { id: 'surface', label: '📊 Greeks Surface' },
          { id: 'termstructure', label: '📈 IV Term Structure' },
          { id: 'skew', label: '🔀 Volatility Skew' },
        ].map((tab) => (
          <button
            key={tab.id}
            onClick={() => setSelectedTab(tab.id as any)}
            className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
              selectedTab === tab.id
                ? 'border-blue-500 text-blue-400'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="space-y-6">
        {selectedTab === 'surface' && <GreeksSurface3D spotPrice={spotPrice} />}
        {selectedTab === 'termstructure' && <IVTermStructure spotPrice={spotPrice} />}
        {selectedTab === 'skew' && <SkewAnalysis spotPrice={spotPrice} expirationDays={30} />}
      </div>

      {/* Info Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-gradient-to-br from-blue-900 to-blue-800 border border-blue-700 rounded-lg p-4">
          <div className="text-2xl mb-2">📊</div>
          <h4 className="font-semibold text-blue-100 mb-2">Greeks Surface</h4>
          <p className="text-blue-200 text-sm">
            Heatmap of Greeks (Delta, Gamma, Vega, Theta) across all strikes and expiration dates. Shows concentration points where Greeks change most.
          </p>
        </div>

        <div className="bg-gradient-to-br from-purple-900 to-purple-800 border border-purple-700 rounded-lg p-4">
          <div className="text-2xl mb-2">📈</div>
          <h4 className="font-semibold text-purple-100 mb-2">IV Term Structure</h4>
          <p className="text-purple-200 text-sm">
            Implied volatility curve across expiration dates. Contango (normal) vs Backwardation (high risk). Trade the curve for profit.
          </p>
        </div>

        <div className="bg-gradient-to-br from-orange-900 to-orange-800 border border-orange-700 rounded-lg p-4">
          <div className="text-2xl mb-2">🔀</div>
          <h4 className="font-semibold text-orange-100 mb-2">Volatility Skew</h4>
          <p className="text-orange-200 text-sm">
            Put/Call IV imbalance reveals market risk sentiment. High put skew = bearish outlook. Use skew to guide hedging strategy.
          </p>
        </div>
      </div>

      {/* Advanced Tips */}
      <div className="bg-gray-800 border border-gray-700 rounded-lg p-6 space-y-4">
        <h3 className="text-xl font-semibold text-gray-100">Advanced Trading Strategies</h3>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <h4 className="font-semibold text-gray-100 mb-2 flex items-center gap-2">
              📊 <span>Greeks Surface Trading</span>
            </h4>
            <ul className="space-y-2 text-sm text-gray-300">
              <li>✓ <strong>High Gamma:</strong> ATM options, sell for premium decay</li>
              <li>✓ <strong>High Vega:</strong> Volatility expansion play, long calls/puts</li>
              <li>✓ <strong>High Theta:</strong> Time decay advantage, sell options</li>
              <li>✓ <strong>High Delta:</strong> Deep ITM calls, act like stock positions</li>
            </ul>
          </div>

          <div>
            <h4 className="font-semibold text-gray-100 mb-2 flex items-center gap-2">
              📈 <span>Term Structure Plays</span>
            </h4>
            <ul className="space-y-2 text-sm text-gray-300">
              <li>✓ <strong>Contango:</strong> Sell near-term premium, buy far-term protection</li>
              <li>✓ <strong>Backwardation:</strong> High uncertainty, widen spreads</li>
              <li>✓ <strong>Curve Flattening:</strong> Near-term IV drops, sell calls</li>
              <li>✓ <strong>Curve Steepening:</strong> Far-term IV rises, buy calls</li>
            </ul>
          </div>

          <div>
            <h4 className="font-semibold text-gray-100 mb-2 flex items-center gap-2">
              🔀 <span>Skew-Based Strategies</span>
            </h4>
            <ul className="space-y-2 text-sm text-gray-300">
              <li>✓ <strong>High Put Skew:</strong> Sell puts (collect fear premium), buy calls</li>
              <li>✓ <strong>Low Put Skew:</strong> Buy puts for cheap protection, sell OTM calls</li>
              <li>✓ <strong>Skew Flattening:</strong> Premium moves from puts to calls, pairs trades</li>
              <li>✓ <strong>Skew Steepening:</strong> Tail risk premium increasing, hedge portfolio</li>
            </ul>
          </div>

          <div>
            <h4 className="font-semibold text-gray-100 mb-2 flex items-center gap-2">
              🎯 <span>Relative Value Plays</span>
            </h4>
            <ul className="space-y-2 text-sm text-gray-300">
              <li>✓ <strong>Ratios:</strong> Buy 1 call, sell 2 calls (risk defined)</li>
              <li>✓ <strong>Spreads:</strong> Long call spread (defined risk, lower cost)</li>
              <li>✓ <strong>Straddles:</strong> Buy ATM call + put (vol expansion play)</li>
              <li>✓ <strong>Calendar Spreads:</strong> Sell near, buy far-term (theta decay)</li>
            </ul>
          </div>
        </div>
      </div>

      {/* Risk Warning */}
      <div className="bg-red-900/20 border border-red-700/30 rounded-lg p-4 flex gap-3">
        <span className="text-2xl">⚠️</span>
        <div>
          <h4 className="font-semibold text-red-100">Options are Leveraged Instruments</h4>
          <p className="text-red-200 text-sm mt-1">
            Greeks change non-linearly with underlying price movements. Gamma (delta change) can cause rapid losses. Always use position sizing and stop losses. Use Greeks to manage risk, not just predict returns.
          </p>
        </div>
      </div>
    </div>
  );
};
