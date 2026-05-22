import React, { useState } from 'react';
import { Plus, X, Save, Play, Copy } from 'lucide-react';

export interface StrategyCondition {
  id: string;
  indicator: string;
  operator: string;
  value: number | string;
}

export interface StrategyRule {
  id: string;
  name: string;
  conditions: StrategyCondition[];
  action: 'BUY' | 'SELL' | 'HOLD';
  enabled: boolean;
  createdAt: number;
}

const INDICATORS = [
  { label: 'RSI', value: 'rsi', range: [0, 100] },
  { label: 'MACD Histogram', value: 'macd_hist', range: [-1, 1] },
  { label: 'Moving Average Crossover', value: 'ma_cross', range: [-1, 1] },
  { label: 'Bollinger Bands Position', value: 'bb_position', range: [-1, 1] },
  { label: 'Price vs SMA200', value: 'price_sma200', range: [-0.5, 0.5] },
  { label: 'ATR Volatility', value: 'atr', range: [0, 100] },
  { label: 'Options IV Percentile', value: 'iv_percentile', range: [0, 100] },
];

const OPERATORS = [
  { label: 'Above', value: '>' },
  { label: 'Below', value: '<' },
  { label: 'Equals', value: '=' },
  { label: 'Above/Crosses', value: 'cross_above' },
  { label: 'Below/Crosses', value: 'cross_below' },
];

interface StrategyBuilderProps {
  onSave?: (rule: StrategyRule) => void;
  onTest?: (rule: StrategyRule) => void;
}

export const StrategyBuilder: React.FC<StrategyBuilderProps> = ({ onSave, onTest }) => {
  const [rules, setRules] = useState<StrategyRule[]>([
    {
      id: '1',
      name: 'RSI Oversold Buy',
      conditions: [{ id: 'c1', indicator: 'rsi', operator: '<', value: 30 }],
      action: 'BUY',
      enabled: true,
      createdAt: Date.now(),
    },
    {
      id: '2',
      name: 'RSI Overbought Sell',
      conditions: [{ id: 'c2', indicator: 'rsi', operator: '>', value: 70 }],
      action: 'SELL',
      enabled: true,
      createdAt: Date.now(),
    },
  ]);

  const [editingRule, setEditingRule] = useState<StrategyRule | null>(null);
  const [newRuleName, setNewRuleName] = useState('');

  const createNewRule = () => {
    const rule: StrategyRule = {
      id: Math.random().toString(36).substr(2, 9),
      name: newRuleName || 'New Strategy',
      conditions: [{ id: Math.random().toString(36).substr(2, 9), indicator: 'rsi', operator: '>', value: 50 }],
      action: 'BUY',
      enabled: true,
      createdAt: Date.now(),
    };
    setRules([...rules, rule]);
    setNewRuleName('');
    setEditingRule(rule);
  };

  const addCondition = (ruleId: string) => {
    setRules(
      rules.map((r) => {
        if (r.id === ruleId) {
          return {
            ...r,
            conditions: [
              ...r.conditions,
              { id: Math.random().toString(36).substr(2, 9), indicator: 'rsi', operator: '>', value: 50 },
            ],
          };
        }
        return r;
      })
    );
  };

  const removeCondition = (ruleId: string, conditionId: string) => {
    setRules(
      rules.map((r) => {
        if (r.id === ruleId) {
          return {
            ...r,
            conditions: r.conditions.filter((c) => c.id !== conditionId),
          };
        }
        return r;
      })
    );
  };

  const updateCondition = (ruleId: string, conditionId: string, field: string, value: any) => {
    setRules(
      rules.map((r) => {
        if (r.id === ruleId) {
          return {
            ...r,
            conditions: r.conditions.map((c) => {
              if (c.id === conditionId) {
                return { ...c, [field]: value };
              }
              return c;
            }),
          };
        }
        return r;
      })
    );
  };

  const updateRule = (ruleId: string, field: string, value: any) => {
    setRules(
      rules.map((r) => {
        if (r.id === ruleId) {
          return { ...r, [field]: value };
        }
        return r;
      })
    );
  };

  const deleteRule = (ruleId: string) => {
    setRules(rules.filter((r) => r.id !== ruleId));
    if (editingRule?.id === ruleId) {
      setEditingRule(null);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold text-gray-100 mb-2">Strategy Builder</h3>
        <p className="text-sm text-gray-400">Create custom trading rules with visual condition editor</p>
      </div>

      {/* Rules List */}
      <div className="bg-gray-800 border border-gray-700 rounded-lg overflow-hidden">
        <div className="p-4 border-b border-gray-700 flex justify-between items-center">
          <h4 className="font-semibold text-gray-100">Trading Rules ({rules.length})</h4>
          <button
            onClick={createNewRule}
            className="bg-blue-600 hover:bg-blue-700 text-white px-3 py-1 rounded text-sm font-medium flex items-center gap-1 transition"
          >
            <Plus className="w-4 h-4" />
            New Rule
          </button>
        </div>

        <div className="space-y-2 p-4">
          {rules.map((rule) => (
            <div
              key={rule.id}
              className={`p-4 rounded border cursor-pointer transition ${
                editingRule?.id === rule.id
                  ? 'bg-blue-900/30 border-blue-700'
                  : 'bg-gray-900 border-gray-700 hover:border-gray-600'
              }`}
              onClick={() => setEditingRule(rule)}
            >
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    checked={rule.enabled}
                    onChange={(e) => {
                      e.stopPropagation();
                      updateRule(rule.id, 'enabled', e.target.checked);
                    }}
                    className="w-4 h-4"
                  />
                  <div>
                    <div className="font-semibold text-gray-100">{rule.name}</div>
                    <div className="text-xs text-gray-400">{rule.conditions.length} condition(s)</div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span
                    className={`px-2 py-1 rounded text-xs font-semibold ${
                      rule.action === 'BUY'
                        ? 'bg-green-900/30 text-green-400'
                        : rule.action === 'SELL'
                        ? 'bg-red-900/30 text-red-400'
                        : 'bg-gray-700 text-gray-300'
                    }`}
                  >
                    {rule.action}
                  </span>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteRule(rule.id);
                    }}
                    className="text-gray-400 hover:text-red-400 transition"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              </div>

              {/* Conditions Preview */}
              <div className="text-xs text-gray-400 space-y-1">
                {rule.conditions.map((cond) => {
                  const indicator = INDICATORS.find((i) => i.value === cond.indicator);
                  return (
                    <div key={cond.id} className="ml-7">
                      {indicator?.label} {cond.operator} {cond.value}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Rule Editor */}
      {editingRule && (
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-6 space-y-4">
          <div className="flex justify-between items-center">
            <h4 className="text-lg font-semibold text-gray-100">Edit Rule</h4>
            <button
              onClick={() => setEditingRule(null)}
              className="text-gray-400 hover:text-gray-200"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Rule Name */}
          <div>
            <label className="block text-sm text-gray-400 mb-2">Rule Name</label>
            <input
              type="text"
              value={editingRule.name}
              onChange={(e) => updateRule(editingRule.id, 'name', e.target.value)}
              className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500"
            />
          </div>

          {/* Action */}
          <div>
            <label className="block text-sm text-gray-400 mb-2">Action</label>
            <div className="flex gap-2">
              {(['BUY', 'SELL', 'HOLD'] as const).map((action) => (
                <button
                  key={action}
                  onClick={() => updateRule(editingRule.id, 'action', action)}
                  className={`flex-1 py-2 rounded font-medium transition ${
                    editingRule.action === action
                      ? action === 'BUY'
                        ? 'bg-green-600'
                        : action === 'SELL'
                        ? 'bg-red-600'
                        : 'bg-gray-600'
                      : 'bg-gray-700 hover:bg-gray-600'
                  }`}
                >
                  {action}
                </button>
              ))}
            </div>
          </div>

          {/* Conditions */}
          <div className="space-y-3">
            <h5 className="font-semibold text-gray-100">Conditions (ALL must be true)</h5>

            {editingRule.conditions.map((cond) => {
              const indicator = INDICATORS.find((i) => i.value === cond.indicator);
              return (
                <div key={cond.id} className="bg-gray-900 p-3 rounded border border-gray-700 flex gap-2 items-end">
                  <select
                    value={cond.indicator}
                    onChange={(e) => updateCondition(editingRule.id, cond.id, 'indicator', e.target.value)}
                    className="flex-1 bg-gray-700 text-white px-3 py-2 rounded text-sm"
                  >
                    {INDICATORS.map((ind) => (
                      <option key={ind.value} value={ind.value}>
                        {ind.label}
                      </option>
                    ))}
                  </select>

                  <select
                    value={cond.operator}
                    onChange={(e) => updateCondition(editingRule.id, cond.id, 'operator', e.target.value)}
                    className="bg-gray-700 text-white px-3 py-2 rounded text-sm"
                  >
                    {OPERATORS.map((op) => (
                      <option key={op.value} value={op.value}>
                        {op.label}
                      </option>
                    ))}
                  </select>

                  <input
                    type="number"
                    value={cond.value}
                    onChange={(e) => updateCondition(editingRule.id, cond.id, 'value', parseFloat(e.target.value) || 0)}
                    className="w-20 bg-gray-700 text-white px-3 py-2 rounded text-sm"
                  />

                  <button
                    onClick={() => removeCondition(editingRule.id, cond.id)}
                    className="text-red-400 hover:text-red-300 p-2"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              );
            })}

            <button
              onClick={() => addCondition(editingRule.id)}
              className="w-full py-2 rounded border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-600 transition text-sm font-medium"
            >
              + Add Condition
            </button>
          </div>

          {/* Actions */}
          <div className="flex gap-2 pt-4 border-t border-gray-700">
            <button
              onClick={() => {
                onTest?.(editingRule);
              }}
              className="flex-1 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded font-medium flex items-center justify-center gap-2 transition"
            >
              <Play className="w-4 h-4" />
              Backtest
            </button>
            <button
              onClick={() => {
                onSave?.(editingRule);
                setEditingRule(null);
              }}
              className="flex-1 bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded font-medium flex items-center justify-center gap-2 transition"
            >
              <Save className="w-4 h-4" />
              Save
            </button>
          </div>
        </div>
      )}

      {/* Template Strategies */}
      <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
        <h4 className="font-semibold text-gray-100 mb-3">Strategy Templates</h4>
        <div className="grid grid-cols-2 gap-2">
          {[
            { name: 'RSI Mean Reversion', desc: 'Buy oversold, sell overbought' },
            { name: 'MA Crossover', desc: 'Golden/Death cross' },
            { name: 'Bollinger Squeeze', desc: 'Trade breakouts from squeeze' },
            { name: 'Vol Expansion', desc: 'Buy on IV spikes' },
          ].map((template) => (
            <button
              key={template.name}
              className="p-3 bg-gray-900 hover:bg-gray-700 border border-gray-700 rounded text-left transition"
            >
              <div className="text-sm font-semibold text-gray-100">{template.name}</div>
              <div className="text-xs text-gray-400">{template.desc}</div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
};
