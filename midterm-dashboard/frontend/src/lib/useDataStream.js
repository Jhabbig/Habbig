import { useEffect, useRef } from 'react'

// Subscribe to the dashboard's SSE feed and invoke onUpdate whenever the
// backend broadcasts a "data_updated" event. Falls back gracefully when SSE
// isn't available (Redis offline) — the caller's existing polling stays
// active.
export function useDataStream(onUpdate, { enabled = true } = {}) {
  const handler = useRef(onUpdate)
  handler.current = onUpdate

  useEffect(() => {
    if (!enabled || typeof EventSource === 'undefined') return undefined

    let es
    try {
      es = new EventSource('/data/stream')
    } catch (e) {
      return undefined
    }

    const onMessage = (evt) => {
      try {
        const payload = JSON.parse(evt.data)
        handler.current?.(payload)
      } catch {
        /* ignore malformed frame */
      }
    }
    es.addEventListener('data_updated', onMessage)
    es.addEventListener('connected', onMessage)

    return () => {
      try {
        es.close()
      } catch { /* noop */ }
    }
  }, [enabled])
}
