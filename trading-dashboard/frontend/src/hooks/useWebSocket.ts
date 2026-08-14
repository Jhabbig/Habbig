import { useEffect, useRef, useState, useCallback } from 'react';
import { Bar, IndicatorValues, WebSocketMessage } from '../types';

interface UseWebSocketReturn {
  bars: Bar[];
  indicators: IndicatorValues | null;
  connected: boolean;
  error: string | null;
}

export function useWebSocket(ticker: string): UseWebSocketReturn {
  const [bars, setBars] = useState<Bar[]>([]);
  const [indicators, setIndicators] = useState<IndicatorValues | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ws = useRef<WebSocket | null>(null);
  const reconnectAttempts = useRef(0);
  const maxReconnectAttempts = 5;

  const connect = useCallback(() => {
    if (ws.current?.readyState === WebSocket.OPEN) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/${ticker}`;

    try {
      ws.current = new WebSocket(wsUrl);

      ws.current.onopen = () => {
        console.log(`Connected to ${ticker}`);
        setConnected(true);
        setError(null);
        reconnectAttempts.current = 0;
      };

      ws.current.onmessage = (event) => {
        try {
          const message: WebSocketMessage = JSON.parse(event.data);

          if (message.type === 'initial_bar' || message.type === 'bar') {
            if (message.bar) {
              setBars((prev) => {
                const updated = [...prev, message.bar!];
                // Keep only last 500 bars in memory
                return updated.slice(-500);
              });
            }
            if (message.indicators) {
              setIndicators(message.indicators);
            }
          } else if (message.type === 'error') {
            setError(message.error || 'Unknown error');
          }
        } catch (e) {
          console.error('Failed to parse WebSocket message:', e);
        }
      };

      ws.current.onerror = (event) => {
        console.error('WebSocket error:', event);
        setError('Connection error');
        setConnected(false);
      };

      ws.current.onclose = () => {
        console.log(`Disconnected from ${ticker}`);
        setConnected(false);

        // Attempt reconnect with exponential backoff
        if (reconnectAttempts.current < maxReconnectAttempts) {
          const delay = 1000 * Math.pow(2, reconnectAttempts.current);
          reconnectAttempts.current += 1;
          console.log(`Reconnecting in ${delay}ms...`);
          setTimeout(connect, delay);
        } else {
          setError('Max reconnect attempts reached');
        }
      };
    } catch (e) {
      console.error('Failed to create WebSocket:', e);
      setError('Failed to connect');
    }
  }, [ticker]);

  useEffect(() => {
    connect();

    return () => {
      if (ws.current) {
        ws.current.close();
      }
    };
  }, [ticker, connect]);

  return { bars, indicators, connected, error };
}
