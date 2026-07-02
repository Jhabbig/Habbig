"""Shared pytest fixtures: tiny seeded synthetic series for fast model tests."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import MarketData, generate_synthetic_prices, log_returns  # noqa: E402


@pytest.fixture(scope="session")
def tiny_md():
    """A small but structurally rich MarketData: 6 assets, 400 days, seed 42."""
    tickers = ["AAPL", "MSFT", "JPM", "XOM", "KO", "DIS"]
    df = generate_synthetic_prices(tickers=tickers, T=400, seed=42)
    P = df.to_numpy(dtype=float)
    R = log_returns(P)
    return MarketData(P=P, R=R, tickers=tickers, dates=df.index, source="synthetic")


@pytest.fixture(scope="session")
def micro_md():
    """A minimal MarketData (3 assets, 120 days) for shape-only assertions."""
    tickers = ["AAPL", "MSFT", "JPM"]
    df = generate_synthetic_prices(tickers=tickers, T=120, seed=7)
    P = df.to_numpy(dtype=float)
    R = log_returns(P)
    return MarketData(P=P, R=R, tickers=tickers, dates=df.index, source="synthetic")
