import React, { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../App'
import { LogIn, AlertCircle } from 'lucide-react'

export default function Login() {
  const { login, user } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  if (user) { navigate('/'); return null }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const data = await login(email, password)
      // Admin users go directly to admin dashboard
      if (data.user?.tier === 'admin') {
        navigate('/admin')
      } else {
        navigate('/')
      }
    } catch (err) { setError(err.message || 'Login failed') }
    finally { setLoading(false) }
  }

  return (
    <div className="max-w-md mx-auto mt-12">
      <div className="bg-white shadow-md border border-stone-100 rounded-xl p-6">
        <h2 className="text-xl font-semibold text-stone-800 mb-6 flex items-center gap-2"><LogIn className="h-5 w-5 text-stone-600" /> Log in</h2>
        {error && <div className="flex items-center gap-2 bg-rose-50 text-rose-500 px-4 py-2 rounded-lg mb-4 text-sm"><AlertCircle className="h-4 w-4 flex-shrink-0" /> {error}</div>}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-stone-500 mb-1">Email</label>
            <input type="email" required value={email} onChange={e => setEmail(e.target.value)} className="input-field" />
          </div>
          <div>
            <label className="block text-sm text-stone-500 mb-1">Password</label>
            <input type="password" required value={password} onChange={e => setPassword(e.target.value)} className="input-field" />
          </div>
          <button type="submit" disabled={loading} className="btn-primary w-full disabled:opacity-50">{loading ? 'Logging in...' : 'Log in'}</button>
        </form>
        <p className="text-sm text-stone-400 mt-4 text-center">Don't have an account? <Link to="/register" className="text-stone-900 hover:underline">Sign up</Link></p>
      </div>
    </div>
  )
}
