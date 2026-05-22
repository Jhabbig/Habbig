import { useEffect, useRef, useState } from 'react';
import { Bar, IndicatorValues, WebSocketMessage } from '../types';

interface UseWebSocketReturn {
  bars: Bar[];
  indicators: IndicatorValues | null;
  connected: boolean;
  error: string | null;
}

const MAX_RECONNECT_ATTEMPTS = 5;
const MAX_BARS = 500;

export function useWebSocket(ticker: string): UseWebSocketReturn {
  const [bars, setBars] = useState<Bar[]>([]);
  const [indicators, setIndicators] = useState<IndicatorValues | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // We re-create connect() per ticker so callbacks always see the current
  // ticker. Pending reconnect timers and a "cancelled" flag are tracked in
  // refs so the effect cleanup can stop in-flight reconnect attempts.
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;

    setBars([]);          // discard previous ticker's bars
    setIndicators(null);

    const clearReconnect = () => {
      if (reconnectTimer.current !== null) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
    };

    const connect = () => {
      if (cancelled) return;
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${protocol}//${window.location.host}/ws/${ticker}`;

      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl);
      } catch (e) {
        console.error('Failed to create WebSocket:', e);
        if (!cancelled) setError('Failed to connect');
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        if (cancelled) return;
        setConnected(true);
        setError(null);
        attempts = 0;
      };

      ws.onmessage = (event) => {
        if (cancelled) return;
        try {
          const message: WebSocketMessage = JSON.parse(event.data);
          if (message.type === 'initial_bar' || message.type === 'bar') {
            if (message.bar) {
              const incoming = message.bar;
              setBars((prev) => {
                // Update-in-place if the last bar has the same timestamp
                // (server may re-emit an in-progress bar); otherwise append
                // and cap the buffer to MAX_BARS.
                if (prev.length > 0 && prev[prev.length - 1].timestamp === incoming.timestamp) {
                  const next = prev.slice(0, -1);
                  next.push(incoming);
                  return next;
                }
                const next = [...prev, incoming];
                if (next.length > MAX_BARS) return next.slice(-MAX_BARS);
                return next;
              });
            }
            if (message.indicators) setIndicators(message.indicators);
          } else if (message.type === 'error') {
            setError(message.error || 'Unknown error');
          }
        } catch (e) {
          console.error('Failed to parse WebSocket message:', e);
        }
      };

      ws.onerror = (event) => {
        if (cancelled) return;
        console.error('WebSocket error:', event, 'readyState:', ws.readyState, 'url:', wsUrl);
        setError('Connection error');
        setConnected(false);
      };

      ws.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        if (attempts < MAX_RECONNECT_ATTEMPTS) {
          const delay = 1000 * Math.pow(2, attempts);
          attempts += 1;
          reconnectTimer.current = setTimeout(connect, delay);
        } else {
          setError('Max reconnect attempts reached');
        }
      };
    };

    connect();

    return () => {
      cancelled = true;
      clearReconnect();
      const ws = wsRef.current;
      if (ws) {
        // Detach handlers so a late onclose can't schedule another reconnect.
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close();
        }
      }
      wsRef.current = null;
    };
  }, [ticker]);

  return { bars, indicators, connected, error };
}
