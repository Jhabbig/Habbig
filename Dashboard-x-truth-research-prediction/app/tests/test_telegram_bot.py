"""Telegram bot command-handler tests. No real network — handler is pure-ish."""
from __future__ import annotations

import pytest

from app.models import Prediction, RawPost, Source
from app.telegram_bot import _format_source_reply, _handle_command


def _src(**kw):
    base = dict(handle="alice", platform="twitter", global_credibility=0.7,
                qualifying_predictions=10, correct_qualifying=7, accuracy_unlocked=True,
                accuracy_global=0.7, brier_score=0.18, brier_n=8)
    base.update(kw)
    return Source(**base)


def test_format_source_reply():
    s = _src()
    out = _format_source_reply(s)
    assert "@alice" in out
    assert "0.70" in out  # credibility
    assert "Brier: `0.180" in out  # n=8 included


@pytest.mark.asyncio
async def test_handle_help():
    out = await _handle_command("/help")
    assert "narve.ai signals bot" in out
    assert "/edge" in out


@pytest.mark.asyncio
async def test_handle_non_command_returns_none():
    out = await _handle_command("just chatting")
    assert out is None


@pytest.mark.asyncio
async def test_handle_unknown_command_returns_none():
    out = await _handle_command("/foo bar")
    assert out is None


@pytest.mark.asyncio
async def test_handle_source_unknown_returns_friendly(session, async_engine):
    import app.db as db_module
    import app.telegram_bot as tb_module
    db_module.engine = async_engine
    tb_module.engine = async_engine
    out = await _handle_command("/source @nonexistent")
    assert "No source" in out


@pytest.mark.asyncio
async def test_handle_source_existing(session, async_engine):
    import app.db as db_module
    import app.telegram_bot as tb_module
    db_module.engine = async_engine
    tb_module.engine = async_engine
    session.add(_src(handle="alice"))
    await session.commit()
    out = await _handle_command("/source @alice")
    assert "@alice" in out and "Credibility" in out


@pytest.mark.asyncio
async def test_handle_edge_no_signals(session, async_engine):
    import app.db as db_module
    import app.telegram_bot as tb_module
    db_module.engine = async_engine
    tb_module.engine = async_engine
    out = await _handle_command("/edge @noone")
    assert "No tracked signals" in out


@pytest.mark.asyncio
async def test_handle_edge_returns_top_signals(session, async_engine):
    import app.db as db_module
    import app.telegram_bot as tb_module
    db_module.engine = async_engine
    tb_module.engine = async_engine
    rp = RawPost(id="t:99", platform="twitter", author_handle="alice", content="x" * 30)
    session.add(rp)
    p = Prediction(raw_post_id="t:99", category="politics", predicted_outcome="Yes",
                   bet_side="YES", market_implied_probability=0.45, ev_score=0.30,
                   market_question="Will Trump win 2028?")
    session.add(p)
    await session.commit()
    out = await _handle_command("/edge @alice")
    assert "Top signals from @alice" in out
    assert "EV +0.30" in out
