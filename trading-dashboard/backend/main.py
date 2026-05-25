#!/usr/bin/env python3
"""
FastAPI Server for Trading Dashboard.
Exposes REST endpoints and WebSocket for real-time market data + indicators.
"""

import asyncio
import json
import logging
import os
import re
import secrets
from typing import Set, Dict, Optional
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

from tier1_adapters import get_facade, RealtimeFacade
from backtest_engine import SimpleBacktestEngine, BacktestResult
from signal_engine import EnsembleSignalEngine, Signal
from options_scanner import OptionsScanEngine, ScanResult

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
log = logging.getLogger("api")

# Environment-driven configuration
ENV = os.environ.get("ENV", "dev").lower()
IS_DEV = ENV == "dev"
# Comma-separated list of allowed origins; in dev defaults to localhost.
_default_dev_origins = "http://localhost:5173,http://127.0.0.1:5173"
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", _default_dev_origins if IS_DEV else "").split(",")
    if o.strip()
]

# Global state
facade: RealtimeFacade = None
connected_clients: Dict[str, Set[WebSocket]] = {}  # ticker -> set of WebSockets
session_tokens: Set[str] = set()  # Valid session tokens for WebSocket auth


# ============================================================================
# Pydantic Models
# ============================================================================

class ErrorResponse(BaseModel):
    """Standardized error response."""
    error: str = Field(..., description="Error message")
    detail: str = Field(default="", description="Additional error details")
    status_code: int = Field(..., description="HTTP status code")


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
    """Backtest request with comprehensive validation."""
    ticker: str = Field(..., min_length=1, max_length=10, pattern=r"^[A-Z0-9.\-]{1,10}$",
                       description="Stock ticker symbol (e.g., AAPL, SPY)")
    strategy: str = Field(..., pattern=r"^(rsi|ma_crossover)$",
                         description="Strategy: 'rsi' or 'ma_crossover'")
    days: int = Field(30, ge=1, le=365, description="Number of days (1-365)")
    initial_capital: float = Field(100000.0, gt=0, le=1_000_000_000,
                                  description="Starting capital in USD")
    position_size_pct: float = Field(0.1, gt=0, le=1.0,
                                    description="Position size % (0.01-1.0)")

    # RSI strategy params
    rsi_oversold: float = Field(30.0, ge=0, le=100, description="RSI oversold threshold")
    rsi_overbought: float = Field(70.0, ge=0, le=100, description="RSI overbought threshold")
    rsi_period: int = Field(14, ge=2, le=200, description="RSI lookback period")

    # MA crossover params
    fast_period: int = Field(12, ge=2, le=200, description="Fast MA period")
    slow_period: int = Field(26, ge=2, le=500, description="Slow MA period")


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


class SignalRequest(BaseModel):
    """AI signal generation request."""
    ticker: str = Field(..., min_length=1, max_length=10, pattern=r"^[A-Z0-9.\-]{1,10}$")
    price: float = Field(..., gt=0)
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
    sma_20: float = 0.0
    sma_50: float = 0.0
    sma_200: float = 0.0
    ema_12: float = 0.0
    ema_26: float = 0.0


class SignalResponse(BaseModel):
    """AI signal response."""
    timestamp: int
    ticker: str
    signal: str
    confidence: float
    price: float
    reasoning: list[str]


class OptionChainItem(BaseModel):
    """Single option in chain."""
    strike: float
    volume: int = 0
    iv: float = 0.0
    iv_percentile: float = 0.0


class ScanRequest(BaseModel):
    """Options scanner request."""
    ticker: str = Field(..., min_length=1, max_length=10, pattern=r"^[A-Z0-9.\-]{1,10}$")
    calls: list[OptionChainItem] = Field(..., max_length=500)
    puts: list[OptionChainItem] = Field(..., max_length=500)
    spot_price: float = Field(..., gt=0)
    screening_type: str = Field(
        "all",
        pattern=r"^(all|unusual_volume|iv_spike|skew_shifts|earnings_move)$",
    )


class ScanResultResponse(BaseModel):
    """Options scan result response."""
    ticker: str
    strike: float
    option_type: str
    signal: str
    severity: str
    value: float
    timestamp: int


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
# Security Headers Middleware
# ============================================================================

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
        return response


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Trading Dashboard API",
    description="Real-time market data, indicators, and Greeks",
    version="0.1.0",
    lifespan=lifespan,
    # Disable interactive docs outside dev — they leak the full API surface.
    docs_url="/docs" if IS_DEV else None,
    redoc_url="/redoc" if IS_DEV else None,
    openapi_url="/openapi.json" if IS_DEV else None,
)

# Rate limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, lambda r, e: JSONResponse(
    status_code=429,
    content={"detail": "Rate limit exceeded"},
))

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# CORS — explicit origin list, never the wildcard. If ALLOWED_ORIGINS is empty
# in a non-dev environment, CORS is effectively closed.
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
        max_age=600,
    )
else:
    log.warning("CORS is disabled: ALLOWED_ORIGINS is empty.")


# Replace ad-hoc {"error": ...} dicts with proper HTTPException responses,
# and centralize unexpected errors so internal stack traces / module paths
# never leak to clients.
@app.exception_handler(Exception)
async def _generic_exception_handler(_, exc):
    """Generic exception handler with standardized error response."""
    log.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": "An unexpected error occurred. Please try again.",
            "status_code": 500
        }
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(_, exc: HTTPException):
    """HTTP exception handler with standardized format."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail if isinstance(exc.detail, str) else "Request error",
            "detail": "",
            "status_code": exc.status_code
        },
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


class SessionTokenResponse(BaseModel):
    """Session token for WebSocket authentication."""
    token: str


@app.get("/api/session-token", response_model=SessionTokenResponse)
@limiter.limit("10/minute")
async def get_session_token(request: Request):
    """Generate a session token for WebSocket authentication."""
    token = secrets.token_urlsafe(32)
    session_tokens.add(token)
    return {"token": token}


TICKER_PATTERN = r"^[A-Z0-9.\-]{1,10}$"


@app.get("/api/bars", response_model=list[BarResponse])
async def get_bars(
    ticker: str = Query(..., example="AAPL", min_length=1, max_length=10, pattern=TICKER_PATTERN),
    interval: str = Query("1m", pattern=r"^(1m|5m|15m|1h|1d)$"),
    limit: int = Query(100, ge=1, le=1000),
):
    """
    Get historical bars for a ticker.
    Intervals: 1m, 5m, 15m, 1h, 1d
    """
    if not facade:
        raise HTTPException(status_code=503, detail="Service unavailable")

    try:
        facade.subscribe(ticker)
        return facade.get_bars(ticker, interval, limit)
    except Exception as e:
        log.exception("Error fetching bars")
        raise HTTPException(status_code=500, detail="Failed to fetch bars") from e


@app.get("/api/indicators", response_model=IndicatorResponse)
async def get_indicators(
    ticker: str = Query(..., example="AAPL", min_length=1, max_length=10, pattern=TICKER_PATTERN),
):
    """Get latest indicator values for a ticker."""
    if not facade:
        raise HTTPException(status_code=503, detail="Service unavailable")

    try:
        indicators = facade.get_indicators(ticker)
        if not indicators:
            raise HTTPException(status_code=404, detail="No indicators available")
        return indicators
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Error fetching indicators")
        raise HTTPException(status_code=500, detail="Failed to fetch indicators") from e


@app.get("/api/greeks", response_model=list[GreeksResponse])
async def get_greeks(
    ticker: str = Query(..., example="AAPL", min_length=1, max_length=10, pattern=TICKER_PATTERN),
    spot_price: float = Query(..., gt=0, le=1_000_000, example=150.0),
    expiration_days: float = Query(30, gt=0, le=3650, example=30),
):
    """
    Compute Greeks for an option chain.
    Returns Greeks for ATM ± 5 strikes.
    """
    if not facade:
        raise HTTPException(status_code=503, detail="Service unavailable")

    try:
        return facade.compute_greeks_chain(
            ticker, spot_price=spot_price, expiration_days=expiration_days
        )
    except Exception as e:
        log.exception("Error computing Greeks")
        raise HTTPException(status_code=500, detail="Failed to compute Greeks") from e


# ============================================================================
# AI Signals Endpoint
# ============================================================================

@app.post("/api/signals", response_model=SignalResponse)
async def generate_signal(request: SignalRequest):
    """
    Generate AI trading signal using ensemble of technical indicators.
    Returns BUY/SELL/HOLD with confidence and reasoning.
    """
    try:
        from stock_dashboard.data.streaming_indicators import IndicatorValues

        indicators = IndicatorValues(
            timestamp=int(datetime.now().timestamp()),
            rsi_14=request.rsi_14,
            rsi_7=request.rsi_7,
            rsi_21=request.rsi_21,
            macd_line=request.macd_line,
            macd_signal=request.macd_signal,
            macd_histogram=request.macd_histogram,
            bb_upper_20=request.bb_upper_20,
            bb_middle_20=request.bb_middle_20,
            bb_lower_20=request.bb_lower_20,
            bb_position=request.bb_position,
            atr_14=request.atr_14,
            atr_7=request.atr_7,
            obv=request.obv,
            roc_5=request.roc_5,
            roc_10=request.roc_10,
            sma_20=request.sma_20,
            sma_50=request.sma_50,
            sma_200=request.sma_200,
            ema_12=request.ema_12,
            ema_26=request.ema_26,
        )

        signal = EnsembleSignalEngine.generate_signal(
            indicators,
            price=request.price,
            timestamp=int(datetime.now().timestamp()),
            ticker=request.ticker
        )

        return {
            "timestamp": signal.timestamp,
            "ticker": signal.ticker,
            "signal": signal.signal,
            "confidence": signal.confidence,
            "price": signal.price,
            "reasoning": signal.reasoning,
        }
    except Exception as e:
        log.exception("Error generating signal")
        raise HTTPException(status_code=500, detail="Failed to generate signal") from e


# ============================================================================
# Options Scanner Endpoint
# ============================================================================

@app.post("/api/scan/options", response_model=list[ScanResultResponse])
@limiter.limit("30/minute")
async def scan_options(request: Request, scan_request: ScanRequest):
    """
    Scan options chain for unusual activity.
    Detects: volume spikes, IV spikes, skew shifts, implied earnings moves.
    """
    try:
        scanner = OptionsScanEngine()

        # Convert to dict format expected by scanner
        calls = [{"strike": c.strike, "volume": c.volume, "iv": c.iv, "iv_percentile": c.iv_percentile} for c in scan_request.calls]
        puts = [{"strike": p.strike, "volume": p.volume, "iv": p.iv, "iv_percentile": p.iv_percentile} for p in scan_request.puts]

        timestamp = int(datetime.now().timestamp())
        results = []

        if scan_request.screening_type in ["all", "unusual_volume"]:
            results.extend(scanner.scan_unusual_volume(scan_request.ticker, calls, puts, timestamp))
        if scan_request.screening_type in ["all", "iv_spike"]:
            results.extend(scanner.scan_iv_spikes(scan_request.ticker, calls, puts, timestamp))
        if scan_request.screening_type in ["all", "skew_shifts"]:
            results.extend(scanner.scan_skew_shifts(scan_request.ticker, calls, puts, scan_request.spot_price, timestamp))
        if scan_request.screening_type in ["all", "earnings_move"]:
            results.extend(scanner.scan_earnings_move(scan_request.ticker, calls, puts, scan_request.spot_price, timestamp))

        # Convert ScanResult to response format
        response = [
            {
                "ticker": r.ticker,
                "strike": r.strike,
                "option_type": r.option_type,
                "signal": r.signal,
                "severity": r.severity,
                "value": r.value,
                "timestamp": r.timestamp
            }
            for r in results
        ]

        return response
    except Exception as e:
        log.exception("Error scanning options")
        raise HTTPException(status_code=500, detail="Failed to scan options") from e


# ============================================================================
# WebSocket Endpoints
# ============================================================================

@app.websocket("/ws/{ticker}")
async def websocket_endpoint(websocket: WebSocket, ticker: str, token: str = Query(...)):
    """
    WebSocket endpoint for real-time bar + indicator streaming.
    Requires valid session token for authentication.

    Sends JSON messages:
    {
        "type": "bar",
        "ticker": "AAPL",
        "bar": {...},
        "indicators": {...}
    }
    """
    # Validate token
    if token not in session_tokens:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    # CSWSH protection: check Origin header matches allowed origins
    origin = websocket.headers.get("origin", "")
    if ALLOWED_ORIGINS and origin not in ALLOWED_ORIGINS:
        await websocket.close(code=1008, reason="Forbidden origin")
        return

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
@limiter.limit("20/minute")
async def run_backtest(request: Request, backtest_request: BacktestRequest):
    """
    Run a backtest with specified strategy and parameters.

    Strategies:
    - rsi: RSI-based buy/sell signals
    - ma_crossover: Moving average crossover
    """
    if not facade:
        raise HTTPException(status_code=503, detail="Service unavailable")

    try:
        # Fetch historical bars (demo: generate synthetic data).
        # NOTE: This data is synthetic, not historical. Surface that to clients
        # so it isn't misrepresented as a real backtest.
        from datetime import datetime, timedelta
        import random

        bars = []
        price = 150.0
        ts = int((datetime.now() - timedelta(days=backtest_request.days)).timestamp())

        # `backtest_request.days` is bounded 1..365 by the Pydantic model, so the loop
        # is bounded to at most 365 * 48 = 17,520 iterations.
        for i in range(backtest_request.days * 48):  # Assume ~48 5-min bars per day
            change = random.gauss(0, 0.8)
            price += change
            price = max(price, 50)

            bars.append({
                'timestamp': ts + (i * 300),
                'open': price,
                'high': price + abs(random.gauss(0, 0.5)),
                'low': price - abs(random.gauss(0, 0.5)),
                'close': price + random.gauss(0, 0.3),
                'volume': random.randint(100000, 1000000),
            })

        engine = SimpleBacktestEngine(initial_capital=backtest_request.initial_capital)

        strategy = backtest_request.strategy.lower()
        if strategy == "rsi":
            result = engine.run_rsi_strategy(
                bars,
                rsi_oversold=backtest_request.rsi_oversold,
                rsi_overbought=backtest_request.rsi_overbought,
                rsi_period=backtest_request.rsi_period,
                position_size_pct=backtest_request.position_size_pct,
            )
        elif strategy == "ma_crossover":
            result = engine.run_ma_crossover_strategy(
                bars,
                fast_period=backtest_request.fast_period,
                slow_period=backtest_request.slow_period,
                position_size_pct=backtest_request.position_size_pct,
            )
        else:
            # Should be unreachable thanks to the Pydantic pattern.
            raise HTTPException(status_code=400, detail="Unknown strategy")

        if result is None:
            raise HTTPException(status_code=501, detail=f"Strategy '{strategy}' not implemented")

        result_dict = {
            "ticker": backtest_request.ticker,
            "strategy": backtest_request.strategy,
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

    except HTTPException:
        raise
    except Exception as e:
        log.exception("Error running backtest")
        raise HTTPException(status_code=500, detail="Backtest failed") from e


# ============================================================================
# Demo/Test Endpoint
# ============================================================================

_demo_task: asyncio.Task | None = None


@app.post("/api/demo/start")
async def demo_start(
    tickers: list[str] = Query(["AAPL", "TSLA", "MSFT"], max_length=20),
    duration_sec: float = Query(300, gt=0, le=3600),
):
    """Start demo streaming for testing."""
    global _demo_task
    if not facade:
        raise HTTPException(status_code=503, detail="Service unavailable")

    # Reject ticker symbols that don't match the allowed pattern.
    bad = [t for t in tickers if not re.fullmatch(r"[A-Z0-9.\-]{1,10}", t)]
    if bad:
        raise HTTPException(status_code=400, detail="Invalid ticker")

    # Refuse to start a second demo task while one is already running —
    # prevents trivial resource amplification via repeated /demo/start calls.
    if _demo_task is not None and not _demo_task.done():
        raise HTTPException(status_code=409, detail="Demo already running")

    try:
        _demo_task = asyncio.create_task(facade.demo_stream(tickers, duration_sec=duration_sec))
        return {"status": "demo started", "tickers": tickers, "duration": duration_sec}
    except Exception as e:
        log.exception("Error starting demo")
        raise HTTPException(status_code=500, detail="Failed to start demo") from e


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    # Bind to loopback by default. Exposing to all interfaces requires
    # explicitly setting HOST in the environment, and should only be done
    # behind a reverse proxy that terminates TLS and adds auth.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    log.info(f"Starting Trading Dashboard Backend on http://{host}:{port}")
    if IS_DEV:
        log.info(f"API docs: http://{host}:{port}/docs")

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=IS_DEV,
        log_level="info",
    )
