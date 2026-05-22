import React, { useState } from 'react';
import { BacktestForm, BacktestParams } from '../components/BacktestForm';
import { BacktestResults, BacktestResult } from '../components/BacktestResults';
import { AlertCircle } from 'lucide-react';

export const BacktestPage: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (params: BacktestParams) => {
    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const response = await fetch('/api/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const data = await response.json();
      if (data.error) {
        throw new Error(data.error);
      }

      setResult(data);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unknown error';
      setError(message);
      console.error('Backtest error:', err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-4">
      {/* Form */}
      <BacktestForm onSubmit={handleSubmit} loading={loading} />

      {/* Error Display */}
      {error && (
        <div className="bg-red-900 border border-red-700 rounded-lg p-4 flex gap-3">
          <AlertCircle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
          <div>
            <h4 className="text-red-100 font-semibold mb-1">Backtest Error</h4>
            <p className="text-red-200 text-sm">{error}</p>
          </div>
        </div>
      )}

      {/* Loading State */}
      {loading && (
        <div className="bg-blue-900 border border-blue-700 rounded-lg p-4 text-center text-blue-100">
          <div className="inline-block animate-spin mr-2">⏳</div>
          Running backtest... This may take a moment.
        </div>
      )}

      {/* Results */}
      {result && !loading && (
        <>
          <div className="bg-green-900 border border-green-700 rounded-lg p-3 text-green-100 text-sm">
            ✓ Backtest completed successfully
          </div>
          <BacktestResults result={result} />
        </>
      )}

      {/* Empty State */}
      {!result && !loading && !error && (
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-8 text-center">
          <div className="text-gray-400 mb-2">📊</div>
          <h3 className="text-gray-100 font-semibold mb-1">No backtest results yet</h3>
          <p className="text-gray-400 text-sm">Configure parameters above and click "Run Backtest" to see results.</p>
        </div>
      )}
    </div>
  );
};
