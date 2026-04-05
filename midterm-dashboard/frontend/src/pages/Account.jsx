import React from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../App'
import { User, Crown, Shield, Mail, Palette } from 'lucide-react'

export default function Account() {
  const { user } = useAuth()
  return (
    <div className="max-w-2xl mx-auto mt-8">
      <h1 className="text-3xl font-semibold text-stone-800 mb-6">Account</h1>
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-6">
        <div className="flex items-center gap-4 mb-6">
          <div className="w-14 h-14 bg-stone-100 rounded-full flex items-center justify-center">
            <User className="h-7 w-7 text-stone-900" />
          </div>
          <div>
            <div className="font-semibold text-lg text-stone-800">{user.display_name || 'User'}</div>
            <div className="text-stone-500 text-sm flex items-center gap-1"><Mail className="h-3 w-3" /> {user.email}</div>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div className="bg-stone-50 rounded-lg p-4">
            <div className="text-xs text-stone-400 mb-1">Plan</div>
            <div className="flex items-center gap-1">
              {user.tier === 'premium' && <Crown className="h-4 w-4 text-amber-600" />}
              {user.tier === 'admin' && <Shield className="h-4 w-4 text-amber-600" />}
              <span className="font-medium text-stone-800 capitalize">{user.tier}</span>
            </div>
          </div>
          <div className="bg-stone-50 rounded-lg p-4">
            <div className="text-xs text-stone-400 mb-1">Subscription</div>
            <span className="font-medium text-stone-800 capitalize">{user.subscription_status || 'Free'}</span>
          </div>
        </div>
      </div>
      <Link to="/settings" className="flex items-center gap-2 bg-white shadow-sm border border-stone-100 rounded-xl p-4 mb-6 hover:bg-stone-50 transition-colors group">
        <div className="w-9 h-9 bg-stone-100 rounded-lg flex items-center justify-center group-hover:bg-stone-200 transition-colors">
          <Palette className="h-4 w-4 text-stone-600" />
        </div>
        <div>
          <div className="text-sm font-medium text-stone-800">Customize appearance</div>
          <div className="text-xs text-stone-400">Theme, colors, density, chart preferences</div>
        </div>
      </Link>
      {user.tier === 'free' && (
        <div className="bg-white shadow-sm border border-amber-200 rounded-xl p-6">
          <h3 className="font-semibold text-stone-800 flex items-center gap-2 mb-3"><Crown className="h-5 w-5 text-amber-600" /> Upgrade to Premium</h3>
          <ul className="text-sm text-stone-500 space-y-1 mb-4">
            <li>&#8226; Custom watchlists with alerts</li>
            <li>&#8226; Deep orderbook comparison data</li>
            <li>&#8226; Campaign finance integration</li>
            <li>&#8226; Priority data refresh</li>
          </ul>
          <button className="btn-primary">Upgrade — $9.99/mo</button>
        </div>
      )}
    </div>
  )
}
