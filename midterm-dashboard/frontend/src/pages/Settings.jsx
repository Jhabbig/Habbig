import React, { useState, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { useSettings, CURRENCIES } from '../lib/settings'
import { LANGUAGES, useT } from '../lib/i18n'
import { ArrowLeft, Check, Sun, Moon, Monitor, BarChart3, AreaChart } from 'lucide-react'

const ACCENT_COLORS = [
  { name: 'blue', value: '#3b82f6', bg: 'bg-blue-500' },
  { name: 'rose', value: '#f43f5e', bg: 'bg-rose-500' },
  { name: 'amber', value: '#f59e0b', bg: 'bg-amber-500' },
  { name: 'emerald', value: '#10b981', bg: 'bg-emerald-500' },
  { name: 'violet', value: '#8b5cf6', bg: 'bg-violet-500' },
  { name: 'orange', value: '#f97316', bg: 'bg-orange-500' },
  { name: 'cyan', value: '#06b6d4', bg: 'bg-cyan-500' },
  { name: 'stone', value: '#78716c', bg: 'bg-stone-500' },
]

const DATA_SOURCES = [
  { key: 'polymarket', label: 'Polymarket' },
  { key: 'kalshi', label: 'Kalshi' },
  { key: 'predictit', label: 'PredictIt' },
  { key: 'polling', label: '538 Polling' },
]

const HISTORY_OPTIONS = [7, 14, 30, 60, 90]

function Toast({ message, visible }) {
  return (
    <div className={`fixed bottom-6 right-6 z-50 transition-all duration-300 ${visible ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-2 pointer-events-none'}`}>
      <div className="bg-stone-800 text-white text-sm px-4 py-2.5 rounded-lg shadow-lg flex items-center gap-2">
        <Check className="h-3.5 w-3.5" />
        {message}
      </div>
    </div>
  )
}

function ToggleGroup({ options, value, onChange }) {
  return (
    <div className="flex bg-stone-100 rounded-lg p-0.5">
      {options.map(opt => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={`flex items-center gap-1.5 px-3.5 py-1.5 rounded-md text-sm transition-all ${
            value === opt.value
              ? 'bg-white text-stone-800 font-medium shadow-sm'
              : 'text-stone-500 hover:text-stone-700'
          }`}
        >
          {opt.icon && <opt.icon className="h-3.5 w-3.5" />}
          {opt.label}
        </button>
      ))}
    </div>
  )
}

function ToggleSwitch({ checked, onChange }) {
  return (
    <button
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
        checked ? 'bg-stone-800' : 'bg-stone-300'
      }`}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
        checked ? 'translate-x-[18px]' : 'translate-x-[3px]'
      }`} />
    </button>
  )
}

export default function Settings() {
  const { settings, updateSetting } = useSettings()
  const t = useT()
  const [toast, setToast] = useState({ visible: false, message: '' })

  const showToast = useCallback((message) => {
    setToast({ visible: true, message })
    setTimeout(() => setToast({ visible: false, message: '' }), 1800)
  }, [])

  const update = useCallback((key, value) => {
    updateSetting(key, value)
    showToast('Setting saved')
  }, [updateSetting, showToast])

  const toggleSource = useCallback((sourceKey) => {
    const current = settings.dataSources || []
    const next = current.includes(sourceKey)
      ? current.filter(s => s !== sourceKey)
      : [...current, sourceKey]
    updateSetting('dataSources', next)
    showToast('Setting saved')
  }, [settings.dataSources, updateSetting, showToast])

  return (
    <div className="max-w-2xl mx-auto mt-8">
      <Link to="/account" className="flex items-center gap-1 text-stone-500 hover:text-stone-700 text-sm mb-4">
        <ArrowLeft className="h-4 w-4" /> {t('common.back')}
      </Link>

      <h1 className="text-3xl font-semibold text-stone-800 mb-6">{t('set.title')}</h1>

      {/* Theme */}
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-4">
        <h2 className="text-sm font-semibold text-stone-500 uppercase tracking-wide mb-4">{t('set.appearance')}</h2>

        <div className="space-y-5">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-stone-800">Theme</div>
              <div className="text-xs text-stone-400 mt-0.5">Choose your preferred color mode</div>
            </div>
            <ToggleGroup
              value={settings.theme}
              onChange={v => update('theme', v)}
              options={[
                { value: 'light', label: 'Light', icon: Sun },
                { value: 'dark', label: 'Dark', icon: Moon },
                { value: 'system', label: 'System', icon: Monitor },
              ]}
            />
          </div>

          <div className="border-t border-stone-100 pt-5">
            <div className="text-sm font-medium text-stone-800 mb-1">Accent Color</div>
            <div className="text-xs text-stone-400 mb-3">Used for buttons and interactive elements</div>
            <div className="flex gap-2.5">
              {ACCENT_COLORS.map(c => (
                <button
                  key={c.name}
                  onClick={() => update('accentColor', c.name)}
                  className={`w-8 h-8 rounded-full ${c.bg} flex items-center justify-center transition-all ${
                    settings.accentColor === c.name
                      ? 'ring-2 ring-offset-2 ring-stone-400 scale-110'
                      : 'hover:scale-105'
                  }`}
                >
                  {settings.accentColor === c.name && (
                    <Check className="h-3.5 w-3.5 text-white" />
                  )}
                </button>
              ))}
            </div>
          </div>

          <div className="border-t border-stone-100 pt-5 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-stone-800">Contrast</div>
              <div className="text-xs text-stone-400 mt-0.5">Increase contrast for better readability</div>
            </div>
            <ToggleGroup
              value={settings.contrast}
              onChange={v => update('contrast', v)}
              options={[
                { value: 'normal', label: 'Normal' },
                { value: 'high', label: 'High' },
              ]}
            />
          </div>

          <div className="border-t border-stone-100 pt-5 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-stone-800">Density</div>
              <div className="text-xs text-stone-400 mt-0.5">Adjust spacing between elements</div>
            </div>
            <ToggleGroup
              value={settings.density}
              onChange={v => update('density', v)}
              options={[
                { value: 'comfortable', label: 'Comfortable' },
                { value: 'compact', label: 'Compact' },
              ]}
            />
          </div>
        </div>
      </div>

      {/* Units, conversions & language */}
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-4">
        <h2 className="text-sm font-semibold text-stone-500 uppercase tracking-wide mb-4">{t('set.units')}</h2>

        <div className="space-y-5">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-stone-800">Number Format</div>
              <div className="text-xs text-stone-400 mt-0.5">How numbers and dates are written</div>
            </div>
            <ToggleGroup
              value={settings.unitSystem || 'american'}
              onChange={v => update('unitSystem', v)}
              options={[
                { value: 'american', label: '\uD83C\uDDFA\uD83C\uDDF8 1,000.00' },
                { value: 'european', label: '\uD83C\uDDEA\uD83C\uDDFA 1.000,00' },
              ]}
            />
          </div>

          <div className="border-t border-stone-100 pt-5 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-stone-800">{t('set.currency')}</div>
              <div className="text-xs text-stone-400 mt-0.5">{t('set.currencyDesc')}</div>
            </div>
            <select
              value={settings.currency || 'USD'}
              onChange={e => update('currency', e.target.value)}
              className="bg-stone-100 border-0 rounded-lg text-sm text-stone-700 px-3 py-1.5 focus:ring-2 focus:ring-stone-300"
            >
              {CURRENCIES.map(c => (
                <option key={c.code} value={c.code}>{`${c.code} \u00b7 ${c.name}`}</option>
              ))}
            </select>
          </div>

          <div className="border-t border-stone-100 pt-5 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-stone-800">{t('set.language')}</div>
              <div className="text-xs text-stone-400 mt-0.5">{t('set.languageDesc')}</div>
            </div>
            <select
              value={settings.language || 'en'}
              onChange={e => update('language', e.target.value)}
              className="bg-stone-100 border-0 rounded-lg text-sm text-stone-700 px-3 py-1.5 focus:ring-2 focus:ring-stone-300"
            >
              {LANGUAGES.map(([code, name]) => (
                <option key={code} value={code}>{name}</option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Charts */}
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-4">
        <h2 className="text-sm font-semibold text-stone-500 uppercase tracking-wide mb-4">{t('set.charts')}</h2>

        <div className="space-y-5">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-stone-800">Chart Style</div>
              <div className="text-xs text-stone-400 mt-0.5">Default visualization for price history</div>
            </div>
            <ToggleGroup
              value={settings.chartStyle}
              onChange={v => update('chartStyle', v)}
              options={[
                { value: 'line', label: 'Line', icon: BarChart3 },
                { value: 'area', label: 'Area', icon: AreaChart },
              ]}
            />
          </div>

          <div className="border-t border-stone-100 pt-5 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-stone-800">Show Polling on Charts</div>
              <div className="text-xs text-stone-400 mt-0.5">Overlay 538 polling averages on price charts</div>
            </div>
            <ToggleSwitch
              checked={settings.showPollingOnCharts}
              onChange={v => update('showPollingOnCharts', v)}
            />
          </div>
        </div>
      </div>

      {/* Data */}
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-4">
        <h2 className="text-sm font-semibold text-stone-500 uppercase tracking-wide mb-4">{t('set.data')}</h2>

        <div className="space-y-5">
          <div>
            <div className="text-sm font-medium text-stone-800 mb-1">Data Sources</div>
            <div className="text-xs text-stone-400 mb-3">Choose which sources appear throughout the dashboard</div>
            <div className="grid grid-cols-2 gap-2">
              {DATA_SOURCES.map(src => (
                <label
                  key={src.key}
                  className={`flex items-center gap-2.5 px-3.5 py-2.5 rounded-lg border cursor-pointer transition-all ${
                    (settings.dataSources || []).includes(src.key)
                      ? 'border-stone-300 bg-stone-50'
                      : 'border-stone-100 hover:border-stone-200'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={(settings.dataSources || []).includes(src.key)}
                    onChange={() => toggleSource(src.key)}
                    className="rounded border-stone-300 text-stone-800 focus:ring-stone-500"
                  />
                  <span className="text-sm text-stone-700">{src.label}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="border-t border-stone-100 pt-5 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-stone-800">Default History Period</div>
              <div className="text-xs text-stone-400 mt-0.5">Time range for charts when first loaded</div>
            </div>
            <select
              value={settings.defaultHistory}
              onChange={e => update('defaultHistory', Number(e.target.value))}
              className="bg-stone-100 border-0 rounded-lg text-sm text-stone-700 px-3 py-1.5 focus:ring-2 focus:ring-stone-300"
            >
              {HISTORY_OPTIONS.map(d => (
                <option key={d} value={d}>{d} days</option>
              ))}
            </select>
          </div>
        </div>
      </div>

      <Toast message={toast.message} visible={toast.visible} />
    </div>
  )
}
