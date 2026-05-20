import React, { useState } from 'react';
import { AlertTriangle, TrendingUp, TrendingDown, Filter } from 'lucide-react';

export interface ScanResult {
  ticker: string;
  strike: number;
  option_type: string;
  signal: string;
  severity: string;
  value: number;
  timestamp: number;
}

interface OptionsScanProps {
  ticker: string;
}

export const OptionsScan: React.FC<OptionsScanProps> = ({ ticker }) => {
  const [results, setResults] = useState<ScanResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [severityFilter, setSeverityFilter] = useState<'all' | 'high' | 'medium' | 'low'>('all');
  const [screeningType, setScreeningType] = useState<string>('all');

  const handleScan = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/scan/options', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ticker,
          calls: [
            { strike: 140, volume: 50000, iv: 0.22, iv_percentile: 65 },
            { strike: 145, volume: 150000, iv: 0.20, iv_percentile: 58 },
            { strike: 150, volume: 80000, iv: 0.19, iv_percentile: 52 },
            { strike: 155, volume: 120000, iv: 0.25, iv_percentile: 85 },
            { strike: 160, volume: 60000, iv: 0.23, iv_percentile: 75 },
          ],
          puts: [
            { strike: 140, volume: 120000, iv: 0.25, iv_percentile: 85 },
            { strike: 145, volume: 60000, iv: 0.23, iv_percentile: 75 },
            { strike: 150, volume: 200000, iv: 0.22, iv_percentile: 68 },
            { strike: 155, volume: 90000, iv: 0.24, iv_percentile: 80 },
            { strike: 160, volume: 40000, iv: 0.20, iv_percentile: 55 },
          ],
          spot_price: 150.0,
          screening_type: screeningType,
        }),
      });

      if (response.ok) {
        const data = await response.json();
        setResults(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      console.error('Error scanning options:', err);
    } finally {
      setLoading(false);
    }
  };

  const filteredResults = results.filter(
    (r) => severityFilter === 'all' || r.severity === severityFilter
  );

  const severityColor = (severity: string) => {
    switch (severity) {
      case 'high':
        return 'text-red-400 bg-red-900/20';
      case 'medium':
        return 'text-orange-400 bg-orange-900/20';
      case 'low':
        return 'text-yellow-400 bg-yellow-900/20';
      default:
        return 'text-gray-400';
    }
  };

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="p-4 border-b border-gray-700">
        <h3 className="text-lg font-semibold text-gray-100 mb-3">Options Scanner</h3>

        <div className="grid grid-cols-3 gap-3 mb-3">
          <select
            value={screeningType}
            onChange={(e) => setScreeningType(e.target.value)}
            className="bg-gray-700 text-white px-3 py-2 rounded text-sm border border-gray-600 focus:outline-none focus:border-blue-500"
          >
            <option value="all">All Scans</option>
            <option value="unusual_volume">Unusual Volume</option>
            <option value="iv_spike">IV Spike</option>
            <option value="skew_shifts">Skew Shifts</option>
            <option value="earnings_move">Earnings Move</option>
          </select>

          <select
            value={severityFilter}
            onChange={(e) => setSeverityFilter(e.target.value as any)}
            className="bg-gray-700 text-white px-3 py-2 rounded text-sm border border-gray-600 focus:outline-none focus:border-blue-500"
          >
            <option value="all">All Severities</option>
            <option value="high">High Only</option>
            <option value="medium">Medium Only</option>
            <option value="low">Low Only</option>
          </select>

          <button
            onClick={handleScan}
            disabled={loading}
            className="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 text-white px-4 py-2 rounded font-medium transition"
          >
            {loading ? 'Scanning...' : 'Scan'}
          </button>
        </div>
      </div>

      {/* Results */}
      {filteredResults.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-700 bg-gray-900">
                <th className="text-left py-3 px-4 text-gray-400">Ticker</th>
                <th className="text-right py-3 px-4 text-gray-400">Strike</th>
                <th className="text-left py-3 px-4 text-gray-400">Type</th>
                <th className="text-left py-3 px-4 text-gray-400">Signal</th>
                <th className="text-left py-3 px-4 text-gray-400">Severity</th>
                <th className="text-right py-3 px-4 text-gray-400">Value</th>
                <th className="text-left py-3 px-4 text-gray-400">Time</th>
              </tr>
            </thead>
            <tbody>
              {filteredResults.map((result, idx) => (
                <tr key={idx} className="border-b border-gray-700 hover:bg-gray-700/50">
                  <td className="py-3 px-4 text-gray-300 font-medium">{result.ticker}</td>
                  <td className="text-right py-3 px-4 text-gray-300">${result.strike.toFixed(2)}</td>
                  <td className="py-3 px-4">
                    <span className={`text-xs font-semibold ${result.option_type === 'call' ? 'text-green-400' : 'text-red-400'}`}>
                      {result.option_type.toUpperCase()}
                    </span>
                  </td>
                  <td className="py-3 px-4 text-gray-300">
                    <span className="text-xs bg-gray-700 px-2 py-1 rounded">
                      {result.signal.replace(/_/g, ' ').toUpperCase()}
                    </span>
                  </td>
                  <td className="py-3 px-4">
                    <span className={`text-xs font-semibold px-2 py-1 rounded ${severityColor(result.severity)}`}>
                      {result.severity.toUpperCase()}
                    </span>
                  </td>
                  <td className="text-right py-3 px-4 text-gray-300 font-medium">
                    {typeof result.value === 'number' && result.value > 1
                      ? result.value.toFixed(0)
                      : result.value.toFixed(4)}
                  </td>
                  <td className="py-3 px-4 text-gray-400 text-xs">
                    {new Date(result.timestamp * 1000).toLocaleTimeString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="p-8 text-center text-gray-400">
          {results.length === 0 ? (
            <div>
              <AlertTriangle className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <p>Click "Scan" to find unusual options activity</p>
            </div>
          ) : (
            <p>No results matching filters</p>
          )}
        </div>
      )}

      {/* Summary */}
      {results.length > 0 && (
        <div className="border-t border-gray-700 p-4 bg-gray-900/50">
          <div className="grid grid-cols-4 gap-4 text-sm">
            <div>
              <div className="text-gray-400 text-xs">Total Results</div>
              <div className="text-gray-100 font-semibold">{results.length}</div>
            </div>
            <div>
              <div className="text-gray-400 text-xs">High Severity</div>
              <div className="text-red-400 font-semibold">{results.filter((r) => r.severity === 'high').length}</div>
            </div>
            <div>
              <div className="text-gray-400 text-xs">Medium Severity</div>
              <div className="text-orange-400 font-semibold">{results.filter((r) => r.severity === 'medium').length}</div>
            </div>
            <div>
              <div className="text-gray-400 text-xs">Low Severity</div>
              <div className="text-yellow-400 font-semibold">{results.filter((r) => r.severity === 'low').length}</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
