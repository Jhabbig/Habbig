#!/usr/bin/env python3
"""
FastAPI Server for Trading Dashboard.
Exposes REST endpoints and WebSocket for real-time market data + indicators.
"""

import asyncio
import json
import logging
from typing import Set, Dict
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from tier1_adapters import get_facade, RealtimeFacade
from backtest_engine import SimpleBacktestEngine, BacktestResult

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
log = logging.getLogger("api")

# Global state
facade: RealtimeFacade = None
connected_clients: Dict[str, Set[WebSocket]] = {}  # ticker -> set of WebSockets


# ============================================================================
# Pydantic Models
# ============================================================================

class BarResponse(BaseModel):
    """OHLCV bar response."""
    ticker: str
    interval: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float = 0.0
    count: int = 0


class IndicatorResponse(BaseModel):
    """Indicator values response."""
    timestamp: int
    rsi_14: float
    rsi_7: float
    rsi_21: float
    macd_line: float
    macd_signal: float
    macd_histogram: float
    bb_upper_20: float
    bb_middle_20: float
    bb_lower_20: float
    bb_position: float
    atr_14: float
    atr_7: float
    obv: float
    roc_5: float
    roc_10: float


class GreeksResponse(BaseModel):
    """Greeks calculation response."""
    strike: float
    call: Dict
    put: Dict


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    timestamp: float
    facade_connected: bool


class BacktestRequest(BaseModel):
    """Backtest request."""
    ticker: str
    strategy: str  # "rsi", "ma_crossover"
    days: int = 30
    initial_capital: float = 100000.0
    position_size_pct: float = 0.1

    # RSI strategy params
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    rsi_period: int = 14

    # MA crossover params
    fast_period: int = 12
    slow_period: int = 26


class BacktestResponse(BaseModel):
    """Backtest results response."""
    ticker: str
    strategy: str
    start_date: str
    end_date: str
    initial_capital: float
    final_equity: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    avg_drawdown_pct: float
    equity_curve: list
    trades: list
    bar_count: int


# ============================================================================
# Lifecycle
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    global facade

    # Startup
    log.info("Starting Trading Dashboard API")
    facade = await get_facade()
    yield

    # Shutdown
    log.info("Shutting down Trading Dashboard API")
    if facade:
        await facade.disconnect()


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Trading Dashboard API",
    description="Real-time market data, indicators, and Greeks",
    version="0.1.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# REST Endpoints
# ============================================================================

@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "timestamp": datetime.now().timestamp(),
        "facade_connected": facade is not None,
    }


@app.get("/api/bars", response_model=list[BarResponse])
async def get_bars(
    ticker: str = Query(..., example="AAPL"),
    interval: str = Query("1m", example="1m"),
    limit: int = Query(100, ge=1, le=1000)
):
    """
    Get historical bars for a ticker.
    Intervals: 1m, 5m, 15m, 1h, 1d
    """
    if not facade:
        return {"error": "Facade not initialized"}

    try:
        facade.subscribe(ticker)
        bars = facade.get_bars(ticker, interval, limit)
        return bars
    except Exception as e:
        log.error(f"Error fetching bars: {e}")
        return {"error": str(e)}


@app.get("/api/indicators", response_model=IndicatorResponse)
async def get_indicators(ticker: str = Query(..., example="AAPL")):
    """Get latest indicator values for a ticker."""
    if not facade:
        return {"error": "Facade not initialized"}

    try:
        indicators = facade.get_indicators(ticker)
        if not indicators:
            return {"error": f"No indicators for {ticker}"}
        return indicators
    except Exception as e:
        log.error(f"Error fetching indicators: {e}")
        return {"error": str(e)}


@app.get("/api/greeks", response_model=list[GreeksResponse])
async def get_greeks(
    ticker: str = Query(..., example="AAPL"),
    spot_price: float = Query(..., gt=0, example=150.0),
    expiration_days: float = Query(30, gt=0, example=30),
):
    """
    Compute Greeks for an option chain.
    Returns Greeks for ATM ± 5 strikes.
    """
    if not facade:
        return {"error": "Facade not initialized"}

    try:
        greeks = facade.compute_greeks_chain(
            ticker,
            spot_price=spot_price,
            expiration_days=expiration_days
        )
        return greeks
    except Exception as e:
        log.error(f"Error computing Greeks: {e}")
        return {"error": str(e)}


# ============================================================================
# WebSocket Endpoints
# ============================================================================

@app.websocket("/ws/{ticker}")
async def websocket_endpoint(websocket: WebSocket, ticker: str):
    """
    WebSocket endpoint for real-time bar + indicator streaming.

    Sends JSON messages:
    {
        "type": "bar",
        "ticker": "AAPL",
        "bar": {...},
        "indicators": {...}
    }
    """
    await websocket.accept()
    log.info(f"Client connected to {ticker}")

    # Track connection
    if ticker not in connected_clients:
        connected_clients[ticker] = set()
    connected_clients[ticker].add(websocket)

    # Subscribe to ticker
    facade.subscribe(ticker)

    # Send initial bars (last 50)
    try:
        bars = facade.get_bars(ticker, "1m", limit=50)
        for bar in bars:
            message = {
                "type": "initial_bar",
                "ticker": ticker,
                "bar": bar,
            }
            await websocket.send_json(message)
    except Exception as e:
        log.error(f"Error sending initial bars: {e}")

    # Callback to broadcast new bars
    async def on_bar(bar, indicators):
        if websocket in connected_clients.get(ticker, set()):
            try:
                message = {
                    "type": "bar",
                    "ticker": ticker,
                    "bar": {
                        "ticker": bar.ticker,
                        "interval": bar.interval.value,
                        "timestamp": bar.timestamp,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "vwap": bar.vwap,
                        "count": bar.count,
                    },
                    "indicators": indicators if indicators else {}
                }
                await websocket.send_json(message)
            except Exception as e:
                log.error(f"Error sending bar to client: {e}")

    # Register callback (sync wrapper for async)
    def sync_callback(bar, indicators):
        # Queue the async call
        asyncio.create_task(on_bar(bar, indicators))

    facade.add_bar_callback(sync_callback)

    # Listen for disconnect
    try:
        while True:
            # Receive any client messages (for future use)
            data = await websocket.receive_text()
            log.debug(f"Received from client: {data}")
    except WebSocketDisconnect:
        log.info(f"Client disconnected from {ticker}")
        if ticker in connected_clients:
            connected_clients[ticker].discard(websocket)
            if not connected_clients[ticker]:
                del connected_clients[ticker]


# ============================================================================
# Backtest Endpoint
# ============================================================================

@app.post("/api/backtest", response_model=BacktestResponse)
async def run_backtest(request: BacktestRequest):
    """
    Run a backtest with specified strategy and parameters.

    Strategies:
    - rsi: RSI-based buy/sell signals
    - ma_crossover: Moving average crossover
    """
    if not facade:
        return {"error": "Facade not initialized"}

    try:
        # Fetch historical bars (demo: generate synthetic data)
        from datetime import datetime, timedelta
        import random

        bars = []
        price = 150.0
        ts = int((datetime.now() - timedelta(days=request.days)).timestamp())

        for i in range(request.days * 48):  # Assume ~48 5-min bars per day
            change = random.gauss(0, 0.8)
            price += change
            price = max(price, 50)

            bars.append({
                'timestamp': ts + (i * 300),
                'open': price,
                'high': price + abs(random.gauss(0, 0.5)),
                'low': price - abs(random.gauss(0, 0.5)),
                'close': price + random.gauss(0, 0.3),
                'volume': random.randint(100000, 1000000)
            })

        # Run backtest
        engine = SimpleBacktestEngine(initial_capital=request.initial_capital)

        if request.strategy.lower() == "rsi":
            result = engine.run_rsi_strategy(
                bars,
                rsi_oversold=request.rsi_oversold,
                rsi_overbought=request.rsi_overbought,
                rsi_period=request.rsi_period,
                position_size_pct=request.position_size_pct,
            )
        elif request.strategy.lower() == "ma_crossover":
            result = engine.run_ma_crossover_strategy(
                bars,
                fast_period=request.fast_period,
                slow_period=request.slow_period,
                position_size_pct=request.position_size_pct,
            )
        else:
            return {"error": f"Unknown strategy: {request.strategy}"}

        # Convert result to dict
        result_dict = {
            "ticker": request.ticker,
            "strategy": request.strategy,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "initial_capital": result.initial_capital,
            "final_equity": result.final_equity,
            "total_return_pct": result.total_return_pct,
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "win_rate": result.win_rate,
            "avg_win": result.avg_win,
            "avg_loss": result.avg_loss,
            "profit_factor": result.profit_factor,
            "sharpe_ratio": result.sharpe_ratio,
            "sortino_ratio": result.sortino_ratio,
            "calmar_ratio": result.calmar_ratio,
            "max_drawdown_pct": result.max_drawdown_pct,
            "avg_drawdown_pct": result.avg_drawdown_pct,
            "equity_curve": result.equity_curve,
            "trades": result.trades,
            "bar_count": result.bar_count,
        }
        return result_dict

    except Exception as e:
        log.error(f"Error running backtest: {e}", exc_info=True)
        return {"error": str(e)}


# ============================================================================
# Demo/Test Endpoint
# ============================================================================

@app.post("/api/demo/start")
async def demo_start(
    tickers: list[str] = Query(["AAPL", "TSLA", "MSFT"]),
    duration_sec: float = Query(300)
):
    """Start demo streaming for testing."""
    if not facade:
        return {"error": "Facade not initialized"}

    try:
        # Start demo in background
        asyncio.create_task(facade.demo_stream(tickers, duration_sec=duration_sec))
        return {"status": "demo started", "tickers": tickers, "duration": duration_sec}
    except Exception as e:
        log.error(f"Error starting demo: {e}")
        return {"error": str(e)}


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    log.info("Starting Trading Dashboard Backend on http://localhost:8000")
    log.info("API docs: http://localhost:8000/docs")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
