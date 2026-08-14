/**
 * TypeScript types for Trading Dashboard
 */

export interface Bar {
  ticker: string;
  interval: string;
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  vwap: number;
  count: number;
}

export interface IndicatorValues {
  timestamp: number;
  rsi_14: number;
  rsi_7: number;
  rsi_21: number;
  macd_line: number;
  macd_signal: number;
  macd_histogram: number;
  bb_upper_20: number;
  bb_middle_20: number;
  bb_lower_20: number;
  bb_position: number;
  atr_14: number;
  atr_7: number;
  obv: number;
  roc_5: number;
  roc_10: number;
  sma_20: number;
  sma_50: number;
  sma_200: number;
  ema_12: number;
  ema_26: number;
}

export interface GreeksValue {
  delta: number;
  gamma: number;
  vega: number;
  theta: number;
  rho: number;
  price: number;
}

export interface GreeksChain {
  strike: number;
  call: GreeksValue;
  put: GreeksValue;
}

export interface WebSocketMessage {
  type: 'bar' | 'initial_bar' | 'error';
  ticker: string;
  bar?: Bar;
  indicators?: IndicatorValues;
  error?: string;
}

export interface ChartData {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}
