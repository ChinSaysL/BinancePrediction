"""Pin the suite to a deterministic profile.

`settings` is pydantic-settings, so it reads `.env` from the working directory.
On the VPS that file is the LIVE trading profile -- and it is synced separately
from the code, so the tests there ran under `entry_mode=impulse` while the same
tests ran under `entry_mode=model` locally. Two of them failed on the server for
exactly that reason: they document MODEL-mode behaviour that the live profile
deliberately turns off.

A suite whose meaning depends on which profile happens to be deployed is not a
suite. These pins restore the config.py defaults; a test that wants the other
branch sets it explicitly (see test_impulse_fastpath.py), and its own
monkeypatch wins because it runs after this fixture.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_entry_mode(monkeypatch):
    """Test the model path unless a test says otherwise, whatever .env holds."""
    monkeypatch.setattr("btcpredict.config.settings.entry_mode", "model")
    monkeypatch.setattr("btcpredict.config.settings.impulse_on_tick", False)
    # ⛔ THE CROSS FIXTURES PREDATE THE MARGIN FLOOR, SO THE FLOOR IS OFF HERE.
    # They build a 1-2 TICK crossing to exercise the order path, the caps and the
    # routing -- and at the shipped `cross_min_margin_bps = 1.0` such a crossing
    # is refused before any of that runs (69 tests said so). The gate itself is
    # tested in tests/test_cross_min_margin.py, which sets it explicitly; a test
    # that wants the live rule sets the knob itself, and its own monkeypatch wins
    # because it runs after this fixture.
    monkeypatch.setattr("btcpredict.config.settings.cross_min_margin_bps", 0.0)
    # ⛔ NO TEST MAY HIT THE VENUE'S REST BOOK. The take path prices its limit
    # from `GET /book?token_id=` (`btcpredict.main.venue_top`, shipped
    # 2026-09-18). Left real, every cross fixture would open a socket -- and,
    # worse, the `await` would suspend `_cross_execute`, which the fixtures run
    # as a task that nothing drains: the task is cancelled when `asyncio.run`
    # closes the loop and the order path looks like it was never reached
    # (5 tests failed exactly that way, with an empty `seen`).
    # Default: UNREADABLE, which is the pre-2026-09-18 base -- so every legacy
    # fixture keeps the behaviour it was written against, and a test that wants
    # a venue ask patches `btcpredict.main.venue_top` itself (its own monkeypatch
    # wins because it runs after this fixture). See
    # tests/test_cross_venue_take.py.
    async def _unreadable(_token):
        return None

    monkeypatch.setattr("btcpredict.main.venue_top", _unreadable)
