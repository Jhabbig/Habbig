import React, { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../App'
import { UserPlus, AlertCircle } from 'lucide-react'

export default function Register() {
  const { register, user } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  if (user) { navigate('/'); return null }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    if (password.length < 8) { setError('Password must be at least 8 characters'); return }
    setLoading(true)
    try { await register(email, password, name); navigate('/') }
    catch (err) { setError(err.message || 'Registration failed') }
    finally { setLoading(false) }
  }

  return (
    <div className="max-w-md mx-auto mt-12">
      <div className="bg-white shadow-md border border-stone-100 rounded-xl p-6">
        <h2 className="text-xl font-semibold text-stone-800 mb-6 flex items-center gap-2"><UserPlus className="h-5 w-5 text-stone-600" /> Create Account</h2>
        {error && <div className="flex items-center gap-2 bg-rose-50 text-rose-500 px-4 py-2 rounded-lg mb-4 text-sm"><AlertCircle className="h-4 w-4 flex-shrink-0" /> {error}</div>}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-stone-500 mb-1">Display Name</label>
            <input type="text" value={name} onChange={e => setName(e.target.value)} className="input-field" />
          </div>
          <div>
            <label className="block text-sm text-stone-500 mb-1">Email</label>
            <input type="email" required value={email} onChange={e => setEmail(e.target.value)} className="input-field" />
          </div>
          <div>
            <label className="block text-sm text-stone-500 mb-1">Password</label>
            <input type="password" required value={password} onChange={e => setPassword(e.target.value)} placeholder="Min. 8 characters" className="input-field" />
          </div>
          <button type="submit" disabled={loading} className="btn-primary w-full disabled:opacity-50">{loading ? 'Creating account...' : 'Create Account'}</button>
        </form>
        <p className="text-sm text-stone-400 mt-4 text-center">Already have an account? <Link to="/login" className="text-stone-900 hover:underline">Log in</Link></p>
        <p className="text-xs text-stone-400 mt-3 text-center">Free tier includes historical data, divergence charts, and all race tracking.</p>
      </div>
    </div>
  )
}
