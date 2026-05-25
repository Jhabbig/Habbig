import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Bell, AlertTriangle, Zap, TrendingUp, Volume2, X } from 'lucide-react';

export interface Alert {
  id: string;
  type: 'signal' | 'scan' | 'risk' | 'trade';
  severity: 'low' | 'medium' | 'high';
  title: string;
  message: string;
  timestamp: number;
  ticker?: string;
  read: boolean;
}

interface AlertManagerProps {
  alerts?: Alert[];
}

const AlertManagerComponent: React.FC<AlertManagerProps> = ({ alerts: initialAlerts = [] }) => {
  const [alerts, setAlerts] = useState<Alert[]>(initialAlerts);
  const [expanded, setExpanded] = useState(false);

  // Auto-add mock alerts for demo
  useEffect(() => {
    const interval = setInterval(() => {
      const mockAlerts: Alert[] = [
        {
          id: Math.random().toString(),
          type: 'signal',
          severity: 'high',
          title: 'BUY Signal Generated',
          message: 'AAPL: RSI oversold at 28, MACD positive - Strong buy signal (95% confidence)',
          timestamp: Math.floor(Date.now() / 1000),
          ticker: 'AAPL',
          read: false,
        },
        {
          id: Math.random().toString(),
          type: 'scan',
          severity: 'medium',
          title: 'Unusual Options Activity',
          message: 'TSLA 155 CALL: Volume spike 5x average - Possible insider accumulation',
          timestamp: Math.floor(Date.now() / 1000),
          ticker: 'TSLA',
          read: false,
        },
        {
          id: Math.random().toString(),
          type: 'risk',
          severity: 'high',
          title: 'Risk Alert',
          message: 'Portfolio drawdown reached -8%. Consider tightening stop losses.',
          timestamp: Math.floor(Date.now() / 1000),
          read: false,
        },
      ];

      // Random chance of alert
      if (Math.random() > 0.7) {
        setAlerts((prev) => [mockAlerts[Math.floor(Math.random() * mockAlerts.length)], ...prev.slice(0, 9)]);
      }
    }, 10000);

    return () => clearInterval(interval);
  }, []);

  const unreadCount = useMemo(() => alerts.filter((a) => !a.read).length, [alerts]);

  const getAlertIcon = useCallback((type: Alert['type']) => {
    switch (type) {
      case 'signal':
        return <TrendingUp className="w-4 h-4" />;
      case 'scan':
        return <Volume2 className="w-4 h-4" />;
      case 'risk':
        return <AlertTriangle className="w-4 h-4" />;
      case 'trade':
        return <Zap className="w-4 h-4" />;
    }
  }, []);

  const getSeverityColor = useCallback((severity: Alert['severity']) => {
    switch (severity) {
      case 'high':
        return 'bg-red-900/20 border-red-700/50 text-red-100';
      case 'medium':
        return 'bg-orange-900/20 border-orange-700/50 text-orange-100';
      case 'low':
        return 'bg-yellow-900/20 border-yellow-700/50 text-yellow-100';
    }
  }, []);

  const handleDismiss = useCallback((id: string) => {
    setAlerts((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const handleMarkRead = useCallback((id: string) => {
    setAlerts((prev) => prev.map((a) => (a.id === id ? { ...a, read: true } : a)));
  }, []);

  return (
    <div className="fixed bottom-4 right-4 z-50 space-y-2">
      {/* Alert Bell Button */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="relative bg-blue-600 hover:bg-blue-700 text-white rounded-full p-3 shadow-lg transition"
      >
        <Bell className="w-5 h-5" />
        {unreadCount > 0 && (
          <span className="absolute top-0 right-0 bg-red-500 text-white text-xs font-bold rounded-full w-5 h-5 flex items-center justify-center">
            {unreadCount}
          </span>
        )}
      </button>

      {/* Alert Panel */}
      {expanded && (
        <div className="bg-gray-800 border border-gray-700 rounded-lg shadow-xl w-96 max-h-96 overflow-hidden flex flex-col">
          {/* Header */}
          <div className="p-4 border-b border-gray-700 flex justify-between items-center">
            <h3 className="font-semibold text-gray-100">Alerts ({unreadCount} unread)</h3>
            <button
              onClick={() => setExpanded(false)}
              className="text-gray-400 hover:text-gray-200"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* Alerts List */}
          <div className="overflow-y-auto flex-1">
            {alerts.length === 0 ? (
              <div className="p-8 text-center text-gray-400">
                <Bell className="w-8 h-8 mx-auto mb-2 opacity-50" />
                <p>No alerts yet</p>
              </div>
            ) : (
              <div className="space-y-2 p-3">
                {alerts.map((alert) => (
                  <div
                    key={alert.id}
                    className={`p-3 rounded border transition ${getSeverityColor(alert.severity)} ${!alert.read ? 'ring-1 ring-blue-400' : ''}`}
                    onClick={() => handleMarkRead(alert.id)}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex items-start gap-2 flex-1">
                        <div className="text-lg mt-0.5">{getAlertIcon(alert.type)}</div>
                        <div className="flex-1">
                          <div className="font-semibold text-sm">{alert.title}</div>
                          <div className="text-xs mt-1 opacity-90">{alert.message}</div>
                          <div className="text-xs mt-2 opacity-75">
                            {new Date(alert.timestamp * 1000).toLocaleTimeString()}
                          </div>
                        </div>
                      </div>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDismiss(alert.id);
                        }}
                        className="text-gray-400 hover:text-gray-200 flex-shrink-0"
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Footer */}
          {alerts.length > 0 && (
            <div className="p-3 border-t border-gray-700 bg-gray-900/50 text-xs text-gray-400">
              Click alert to mark as read • Right X to dismiss
            </div>
          )}
        </div>
      )}

      {/* Toast Notifications (top alerts only) */}
      <div className="space-y-2 fixed bottom-20 right-4">
        {alerts.slice(0, 3).map((alert) => (
          <div
            key={alert.id}
            className={`p-3 rounded-lg shadow-lg border max-w-sm animate-slide-in ${getSeverityColor(alert.severity)}`}
          >
            <div className="flex items-start gap-2">
              <div>{getAlertIcon(alert.type)}</div>
              <div className="flex-1">
                <div className="font-semibold text-sm">{alert.title}</div>
                <div className="text-xs mt-0.5">{alert.message}</div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export const AlertManager = React.memo(AlertManagerComponent);
