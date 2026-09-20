"""Tests for cross arm weekday-only gating (`cross_weekdays_only`)."""
import asyncio
import time
import pytest

from btcpredict import main as m


def _freeze(monkeypatch, wday, hour=12):
    """Pin time.gmtime to a weekday index 0=Mon .. 6=Sun, at `hour` UTC."""
    base = time.struct_time((2026, 9, 14 + wday, hour, 0, 0, wday, 257, 0))
    monkeypatch.setattr(m.time, "gmtime", lambda *a: base)


def _window(monkeypatch, sd=4, sh=22, ed=6, eh=21):
    for k, v in (("weekend_start_dow", sd), ("weekend_start_hour_utc", sh),
                 ("weekend_end_dow", ed), ("weekend_end_hour_utc", eh)):
        monkeypatch.setattr(m.settings, k, v, raising=False)


def test_cross_all_skips_on_weekend_when_weekdays_only(monkeypatch):
    _window(monkeypatch)
    _freeze(monkeypatch, 5, 12)  # Saturday noon UTC
    monkeypatch.setattr(m.settings, "cross_enabled", True)
    monkeypatch.setattr(m.settings, "cross_weekdays_only", True)

    bot = m.Bot.__new__(m.Bot)
    called = []

    async def fake_cross_tick(ctx=None):
        called.append(ctx)

    bot._cross_tick = fake_cross_tick
    bot._mkt = {"btc": {"round": None, "feed": None}}

    asyncio.run(bot._cross_all())
    assert len(called) == 0, "cross_all should not fire on weekends when cross_weekdays_only is True"


def test_cross_all_runs_on_weekday_when_weekdays_only(monkeypatch):
    _window(monkeypatch)
    _freeze(monkeypatch, 2, 12)  # Wednesday noon UTC
    monkeypatch.setattr(m.settings, "cross_enabled", True)
    monkeypatch.setattr(m.settings, "cross_weekdays_only", True)

    bot = m.Bot.__new__(m.Bot)
    called = []

    async def fake_cross_tick(ctx=None):
        called.append(ctx)

    bot._cross_tick = fake_cross_tick
    bot._mkt = {"btc": {"round": None, "feed": None}}

    asyncio.run(bot._cross_all())
    assert len(called) == 1, "cross_all should fire on weekdays when cross_weekdays_only is True"


def test_cross_all_runs_on_weekend_when_weekdays_only_false(monkeypatch):
    _window(monkeypatch)
    _freeze(monkeypatch, 5, 12)  # Saturday noon UTC
    monkeypatch.setattr(m.settings, "cross_enabled", True)
    monkeypatch.setattr(m.settings, "cross_weekdays_only", False)

    bot = m.Bot.__new__(m.Bot)
    called = []

    async def fake_cross_tick(ctx=None):
        called.append(ctx)

    bot._cross_tick = fake_cross_tick
    bot._mkt = {"btc": {"round": None, "feed": None}}

    asyncio.run(bot._cross_all())
    assert len(called) == 1, "cross_all should fire on weekends if cross_weekdays_only is False"
