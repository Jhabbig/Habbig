import React, { useState, useEffect } from 'react'
import { api } from './api'
import { MessageCircle, Send, Trash2 } from 'lucide-react'

export default function Comments({ raceKey, currentUser }) {
  const [comments, setComments] = useState([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const isPremium = currentUser && (currentUser.tier === 'premium' || currentUser.tier === 'admin')
  const isAdmin = currentUser?.tier === 'admin'

  useEffect(() => {
    if (!raceKey) return
    api.comments(raceKey).then(d => setComments(d.comments || [])).catch(() => {})
  }, [raceKey])

  async function post(e) {
    e.preventDefault()
    if (!text.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      await api.postComment(raceKey, text.trim())
      setText('')
      const d = await api.comments(raceKey)
      setComments(d.comments || [])
    } catch (e) {
      setError(e.message || 'Failed to post')
    } finally {
      setBusy(false)
    }
  }

  async function remove(id) {
    if (!confirm('Delete this comment?')) return
    try {
      await api.deleteComment(id)
      setComments(c => c.filter(x => x.id !== id))
    } catch (e) {
      setError(e.message || 'Failed to delete')
    }
  }

  return (
    <section aria-labelledby="comments-heading"
      className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 sm:p-6 mb-6">
      <h3 id="comments-heading" className="text-lg font-semibold text-stone-800 flex items-center gap-2 mb-4">
        <MessageCircle className="h-5 w-5 text-stone-500" aria-hidden="true" />
        Discussion <span className="text-xs font-normal text-stone-400">({comments.length})</span>
      </h3>

      {isPremium ? (
        <form onSubmit={post} className="flex gap-2 mb-4">
          <label htmlFor="comment-input" className="sr-only">Add a comment</label>
          <textarea id="comment-input" value={text} onChange={e => setText(e.target.value)}
            placeholder="Share your read on this race…" rows={2} maxLength={2000}
            className="flex-1 bg-stone-50 border border-stone-200 rounded-lg p-2 text-sm focus:outline-none focus:ring-2 focus:ring-stone-900/10" />
          <button type="submit" disabled={busy || !text.trim()}
            aria-label="Post comment"
            className="bg-stone-800 text-white px-3 rounded-lg hover:bg-stone-700 disabled:opacity-50 transition-colors">
            <Send className="h-4 w-4" aria-hidden="true" />
          </button>
        </form>
      ) : (
        <div className="bg-stone-50 border border-stone-100 rounded-lg p-3 text-xs text-stone-500 mb-4">
          {currentUser ? 'Premium subscribers can post comments.' : 'Sign in to join the discussion.'}
        </div>
      )}

      {error && <div role="alert" className="text-xs text-red-600 mb-2">{error}</div>}

      {comments.length === 0 ? (
        <div className="text-sm text-stone-400">No comments yet.</div>
      ) : (
        <ul className="divide-y divide-stone-100">
          {comments.map(c => (
            <li key={c.id} className="py-2.5 flex items-start gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 text-xs text-stone-500 mb-0.5">
                  <span className="font-medium text-stone-700">{c.user_email}</span>
                  {c.user_tier === 'admin' && <span className="bg-stone-800 text-white px-1.5 py-0.5 rounded text-[9px] font-bold">ADMIN</span>}
                  {c.user_tier === 'premium' && <span className="bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded text-[9px] font-bold">PRO</span>}
                  <span className="text-stone-400">{(c.created_at || '').slice(0, 16).replace('T', ' ')}</span>
                </div>
                <div className="text-sm text-stone-800 whitespace-pre-wrap">{c.body}</div>
              </div>
              {(isAdmin || c.user_email === (currentUser?.email || '').split('@')[0]) && (
                <button onClick={() => remove(c.id)} aria-label="Delete comment"
                  className="text-stone-400 hover:text-red-600 transition-colors shrink-0">
                  <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
