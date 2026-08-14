# StockSignal Trading Dashboard - Week 1 MVP

Professional trading dashboard built with React + TradingView Lightweight Charts connected to a FastAPI backend that integrates Tier 1 analysis modules (real-time data, streaming indicators, Greeks calculation).

## Architecture

```
Frontend (React + TypeScript + Vite)
    ↓ WebSocket + REST API
Backend (FastAPI + Python)
    ↓ Imports
Tier 1 Modules
  - realtime.py (Alpaca WebSocket, bar aggregation)
  - streaming_indicators.py (10+ real-time indicators)
  - greeks.py (Black-Scholes Greeks)
  - sizing.py (position sizing)
  - stops.py (stop management)
  - metrics.py (performance analytics)
  - heat.py (portfolio monitoring)
```

## Quick Start

### Prerequisites
- Python 3.9+
- Node.js 18+
- npm or yarn

### Backend Setup

```bash
cd /home/user/Habbig/trading-dashboard/backend

# Install dependencies
pip install -r requirements.txt

# Start FastAPI server (demo mode, no API keys needed)
python main.py

# Server will start on http://localhost:8000
# API docs: http://localhost:8000/docs
# WebSocket: ws://localhost:8000/ws/{ticker}
```

### Frontend Setup

```bash
cd /home/user/Habbig/trading-dashboard/frontend

# Install dependencies
npm install

# Start development server
npm run dev

# Frontend will open on http://localhost:5173
```

### Test E2E

1. **Backend**: Start `python main.py` (will run demo streaming)
2. **Frontend**: Start `npm run dev` (will connect via proxy)
3. **Verify**:
   - Chart appears with AAPL candles
   - Ticker dropdown works
   - WebSocket connects (green "Connected" indicator)
   - Bars update in real-time
   - Indicators show values
   - Greeks heatmap populates

## Features Implemented (Week 1)

### ✅ Backend
- [x] FastAPI server with CORS + static files
- [x] Tier 1 adapter layer (tier1_adapters.py)
- [x] REST endpoints:
  - `GET /api/health` — Health check
  - `GET /api/bars?ticker=AAPL&interval=1m&limit=100` — Historical OHLCV
  - `GET /api/indicators?ticker=AAPL` — Latest indicator values
  - `GET /api/greeks?ticker=AAPL&spot_price=150.0&expiration_days=30` — Greeks chain
- [x] WebSocket endpoint `/ws/{ticker}` for real-time bars + indicators
- [x] Demo streaming mode (synthetic ticks, no API key required)

### ✅ Frontend
- [x] React + TypeScript + Vite scaffold
- [x] TradingView Lightweight Charts integration
- [x] Ticker selector dropdown (AAPL, TSLA, MSFT, GOOGL, NVDA, SPY)
- [x] Real-time OHLC candles
- [x] Bollinger Bands overlay
- [x] Indicator panel (RSI, MACD, ATR, ROC, OBV, BB position)
- [x] WebSocket reconnect logic with exponential backoff
- [x] Greeks heatmap table (calls/puts, delta/gamma/theta/vega)
- [x] Connection status indicator
- [x] Responsive design (dark mode, mobile-friendly)
- [x] Error handling + loading states

## API Reference

### REST Endpoints

#### GET /api/health
Health check.

**Response:**
```json
{
  "status": "ok",
  "timestamp": 1716241234.5,
  "facade_connected": true
}
```

#### GET /api/bars
Fetch historical bars.

**Query Parameters:**
- `ticker` (required): Stock symbol (e.g., "AAPL")
- `interval` (optional, default: "1m"): Bar interval (1m, 5m, 15m, 1h, 1d)
- `limit` (optional, default: 100): Max bars to return (1-1000)

**Response:**
```json
[
  {
    "ticker": "AAPL",
    "interval": "1m",
    "timestamp": 1716241200,
    "open": 150.0,
    "high": 150.5,
    "low": 149.8,
    "close": 150.2,
    "volume": 1000000,
    "vwap": 150.1,
    "count": 500
  }
]
```

#### GET /api/indicators
Fetch latest indicator values.

**Query Parameters:**
- `ticker` (required): Stock symbol

**Response:**
```json
{
  "timestamp": 1716241234,
  "rsi_14": 65.4,
  "rsi_7": 72.1,
  "rsi_21": 58.3,
  "macd_line": 0.2345,
  "macd_signal": 0.1234,
  "macd_histogram": 0.1111,
  "bb_upper_20": 151.5,
  "bb_middle_20": 150.0,
  "bb_lower_20": 148.5,
  "bb_position": 0.5,
  "atr_14": 1.2,
  "atr_7": 1.0,
  "obv": 1000000000,
  "roc_5": 1.5,
  "roc_10": 2.1
}
```

#### GET /api/greeks
Compute Greeks for an option chain.

**Query Parameters:**
- `ticker` (required): Stock symbol
- `spot_price` (required): Current stock price
- `expiration_days` (optional, default: 30): Days to expiration

**Response:**
```json
[
  {
    "strike": 145.0,
    "call": {
      "delta": 0.75,
      "gamma": 0.02,
      "vega": 0.12,
      "theta": -0.01,
      "rho": 0.05,
      "price": 5.5
    },
    "put": {
      "delta": -0.25,
      "gamma": 0.02,
      "vega": 0.12,
      "theta": -0.01,
      "rho": -0.05,
      "price": 0.5
    }
  }
]
```

### WebSocket /ws/{ticker}

Subscribe to real-time bars + indicators.

**Connection:**
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/AAPL');
```

**Messages:**

Initial bars (historical):
```json
{
  "type": "initial_bar",
  "ticker": "AAPL",
  "bar": { /* Bar object */ }
}
```

New completed bars:
```json
{
  "type": "bar",
  "ticker": "AAPL",
  "bar": { /* Bar object */ },
  "indicators": { /* IndicatorValues object */ }
}
```

## Project Structure

```
trading-dashboard/
├── backend/
│   ├── main.py                 # FastAPI server + WebSocket
│   ├── tier1_adapters.py       # Tier 1 module facade
│   ├── requirements.txt
│   └── __init__.py
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── Chart.tsx       # TradingView Lightweight Charts
│   │   │   ├── Indicators.tsx  # Indicator legend
│   │   │   └── GreeksHeatmap.tsx
│   │   ├── hooks/
│   │   │   └── useWebSocket.ts # WebSocket management
│   │   ├── types/
│   │   │   └── index.ts        # TypeScript interfaces
│   │   ├── App.tsx             # Main app component
│   │   ├── main.tsx            # Entry point
│   │   └── index.css           # Tailwind + global styles
│   ├── index.html
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── tailwind.config.js
│   ├── package.json
│   └── postcss.config.js
└── README.md
```

## Tier 1 Integration

The backend (`tier1_adapters.py`) imports and wraps these Tier 1 modules:

### Data Pipeline
- **realtime.py**: `RealtimeDataPipeline` manages WebSocket/REST polling from Alpaca
- **realtime.py**: `BarAggregator` converts ticks → 1m/5m/1h bars
- Data flows through bar callbacks → WebSocket broadcast

### Indicators
- **streaming_indicators.py**: `StreamingIndicators` computes 10+ indicators on each bar
- Values: RSI(7/14/21), MACD, Bollinger Bands, ATR(7/14), OBV, ROC(5/10)
- Efficient rolling buffers (no full history storage)

### Options
- **greeks.py**: `BlackScholesCalculator` computes Greeks on demand
- Batch processing for option chains (all strikes at once)
- Returns: delta, gamma, vega, theta, rho for calls/puts

### Risk & Analytics
- **sizing.py**: Position sizing (Kelly, volatility-adjusted, correlation-aware)
- **stops.py**: Multi-strategy stop-loss levels
- **heat.py**: Portfolio heat tracking + circuit breakers
- **metrics.py**: Performance analytics (Sharpe, Sortino, drawdown)

## Demo Mode

Backend runs in **demo mode** by default:
- Synthetic ticks simulating random walk for AAPL, TSLA, MSFT
- No Alpaca API key needed
- Bars aggregate in real-time every 1 minute
- Perfect for development/testing

To use real market data:
1. Get Alpaca API key from https://alpaca.markets
2. In `backend/main.py`, change: `RealtimeFacade(api_key="your_key_here")`
3. Comment out `demo_stream()` calls

## Next Steps (Week 2-3)

### Chart Enhancements
- [ ] Add SMA/EMA line overlays (moving average trends)
- [ ] More indicator subpanels (RSI, MACD in separate panes)
- [ ] Volume bars
- [ ] Support historical data loading (100+ bars at once)

### Frontend Features
- [ ] Timeframe switching (1m, 5m, 15m, 1h, daily)
- [ ] Order placement mockup (pre-filled from chart)
- [ ] Portfolio heat status display
- [ ] Trade journal
- [ ] Scanner integration (scan for breakouts, earnings plays)

### Backend Features
- [ ] Real Alpaca API integration (live data)
- [ ] Options scanning (unusual volume, IV spikes)
- [ ] Backtesting UI (historical performance)
- [ ] AI signal generation (Transformer-based)

## Troubleshooting

### "Connection refused" on WebSocket
- Ensure backend is running: `python main.py`
- Check port 8000 is free

### Chart shows no data
- Check browser console for errors
- Verify WebSocket connects (status indicator shows "Connected")
- Try refreshing page

### Indicators showing 0
- Indicators need 14+ bars to compute (rolling window warmup)
- Wait for enough bars to stream in (~15-20 seconds)

### Greeks heatmap empty
- Frontend needs `spot_price` from latest bar
- Make sure bars are streaming
- Check `/api/greeks` endpoint directly: http://localhost:8000/docs

## Tech Stack

### Backend
- **FastAPI** — Modern async Python web framework
- **Uvicorn** — ASGI server
- **WebSockets** — Real-time bidirectional communication
- **Pydantic** — Data validation

### Frontend
- **React 18** — UI library
- **TypeScript** — Type safety
- **Vite** — Fast build tool
- **TradingView Lightweight Charts** — Professional charting
- **Tailwind CSS** — Styling
- **Lucide React** — Icons

### Tier 1 Integration
- **NumPy/SciPy** — Numerical computation (Greeks, indicators)
- **SQLite** — Data persistence (optional)
- **Asyncio** — Async Python

## License

© 2026 StockSignal. All rights reserved.

## Contributing

See `/home/user/Habbig/CONTRIBUTING.md` for guidelines.
