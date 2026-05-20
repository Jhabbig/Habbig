// Web push subscription helpers.
//
// Flow:
//   1. Frontend asks backend for VAPID public key.
//   2. Browser subscribes via the service worker registration.
//   3. Subscription (endpoint + keys) is POSTed to /premium/push/subscribe.

import { api } from './api'

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = window.atob(base64)
  const arr = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i)
  return arr
}

export function pushSupported() {
  return typeof window !== 'undefined' && 'serviceWorker' in navigator && 'PushManager' in window
}

export async function pushStatus() {
  if (!pushSupported()) return { supported: false }
  const reg = await navigator.serviceWorker.ready
  const sub = await reg.pushManager.getSubscription()
  return { supported: true, subscribed: !!sub, permission: Notification.permission }
}

export async function subscribePush() {
  if (!pushSupported()) throw new Error('Push not supported')
  const cfg = await api.pushConfig()
  if (!cfg.public_key) throw new Error('Server has no VAPID key configured')

  const permission = await Notification.requestPermission()
  if (permission !== 'granted') throw new Error('Notification permission denied')

  const reg = await navigator.serviceWorker.ready
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(cfg.public_key),
  })
  const raw = sub.toJSON()
  await api.pushSubscribe(raw.endpoint, raw.keys)
  return raw
}

export async function unsubscribePush() {
  if (!pushSupported()) return false
  const reg = await navigator.serviceWorker.ready
  const sub = await reg.pushManager.getSubscription()
  if (!sub) return false
  const endpoint = sub.endpoint
  await sub.unsubscribe()
  await api.pushUnsubscribe(endpoint, {}).catch(() => null)
  return true
}
