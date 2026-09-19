"""The locked-in tail: buy the near-certain favourite while the book discounts it.

WHY THIS ONE AND NOT THE CROSSING. It is the only edge measured on this tape
that does NOT decay with execution delay, on 15,755 qualifying ticks:

    0ms +$0.0806/bet | 1000ms +$0.0768 | 2000ms +$0.0786 | 5000ms +$0.0929

The cross arm, same tape, fell from +6.2 points to +0.9 over that first
second. This does not decay because "the outcome is locked" is not a print the
venue races to reprice -- it is a slow drift toward 1.00.

⛔ AND IT IS NOT PROVEN. Per-tick statistics are inflated because ticks inside
a round share one outcome (9.7 ticks per round, so ~sqrt(9.7) of inflation).
Corrected to one bet per round: +2.3%/bet, t+1.04, day-clustered t+0.32, three
of four days negative. That is why it ships OFF and SHADOW -- the guards below
exist to keep it that way until weeks of live data say otherwise.
"""
from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace as NS

import pytest

from btcpredict.config import Settings
from btcpredict.main import Bot, TWAP_REF_LOOKBACK_S


class TestTheDefaults:
    def test_it_ships_off(self) -> None:
        assert Settings.model_fields["locked_tail_enabled"].default is False

    def test_it_ships_in_shadow(self) -> None:
        """Round-level t is +1.04. Nothing this thin trades on day one."""
        assert Settings.model_fields["locked_tail_shadow"].default is True

    def test_the_window_is_where_the_edge_was_measured(self) -> None:
        """tau 60-300 measured +$0.004/bet -- nothing. The edge is 5-60."""
        assert Settings.model_fields["locked_tail_tau_lo"].default == 5.0
        assert Settings.model_fields["locked_tail_tau_hi"].default == 60.0


def _bot(monkeypatch, *, spot, level_ticks, tau, ask=0.93, enabled=True,
         shadow=True):
    monkeypatch.setattr("btcpredict.config.settings.locked_tail_enabled", enabled)
    monkeypatch.setattr("btcpredict.config.settings.locked_tail_shadow", shadow)
    b = Bot.__new__(Bot)
    b.mode = "live"; b.bankroll = 50.0
    b.broker = NS(mode="live")
    b.buys = []

    async def buy(**k):
        b.buys.append(k); return NS(price=k["price"])
    b.broker.buy = buy
    b.spots = {"BTCUSDT": NS(ticks=level_ticks)}
    b._ck = lambda n, r=None: 1.0
    b.ws_book = NS(best=lambda r, a: NS(ask=lambda side: ask))
    return b


def _flat_tape(n=400, px=100.0, end_ms=1_000_000, step=1000):
    return [(end_ms - (n - i) * step, px) for i in range(n)]


class TestTheProbability:
    def test_tau_at_or_above_60_returns_nothing(self, monkeypatch) -> None:
        """Nothing of the paid window is printed yet, the forecast is just the
        spot, and the measured edge there is +$0.004 -- i.e. none."""
        b = _bot(monkeypatch, spot=100.0, level_ticks=_flat_tape(), tau=90)
        r = NS(slug="s", start_ms=700_000, end_ms=1_000_000, strike=100.0)
        assert b._locked_tail_p(r, "BTCUSDT", 90.0) is None

    def test_below_the_floor_returns_nothing(self, monkeypatch) -> None:
        b = _bot(monkeypatch, spot=100.0, level_ticks=_flat_tape(), tau=2)
        r = NS(slug="s", start_ms=700_000, end_ms=1_000_000, strike=100.0)
        assert b._locked_tail_p(r, "BTCUSDT", 2.0) is None

    def test_a_short_tape_returns_nothing(self, monkeypatch) -> None:
        """No tape, no probability. It must not guess."""
        b = _bot(monkeypatch, spot=100.0, level_ticks=_flat_tape(n=5), tau=20)
        r = NS(slug="s", start_ms=700_000, end_ms=1_000_000, strike=100.0)
        assert b._locked_tail_p(r, "BTCUSDT", 20.0) is None

    def test_a_risen_tape_prices_Up_near_certain(self, monkeypatch) -> None:
        """Flat at 100 through the open, then a clean rise: with most of the
        paid window printed above the level, Up should be near 1."""
        t = _flat_tape(n=300, px=100.0)
        t += [(t[-1][0] + (i + 1) * 1000, 100.0 + 0.5 * (i + 1)) for i in range(50)]
        b = _bot(monkeypatch, spot=t[-1][1], level_ticks=t, tau=10)
        r = NS(slug="s", start_ms=t[0][0] + 60_000,
               end_ms=t[-1][0] + 10_000, strike=100.0)
        got = b._locked_tail_p(r, "BTCUSDT", 10.0)
        assert got is not None
        p_up, level, fc = got
        assert p_up > 0.90, p_up
        assert fc > level

    def test_certainty_falls_as_the_unprinted_window_grows(self, monkeypatch) -> None:
        """⭐ THE WHOLE POINT, AND THE THING A BPS FLOOR CANNOT EXPRESS.

        Hold the forecast gap roughly CONSTANT and vary only how much of the
        paid window is still unprinted. The same gap is a certainty near the
        bell and a coin flip far from it:

            tau  6 -> P=1.0000    tau 30 -> P=0.8910
            tau 20 -> P=0.9896    tau 55 -> P=0.6869

        `cross_min_margin_bps` thresholded the GAP, which treats those four as
        identical. That is why no value of it could ever have worked: the right
        threshold is in standard deviations, and the standard deviation is a
        function of tau.

        A noiseless tape cannot test this -- sigma collapses and every P
        saturates at 1.0 -- so the tape here carries real noise and the offset
        is sized against it.
        """
        import random
        rng = random.Random(11)
        end, step = 1_000_000, 1000
        t = [(end - (380 - i) * step, 100.0 + rng.gauss(0, 0.02)) for i in range(300)]
        t += [(end - (80 - i) * step, 100.06 + rng.gauss(0, 0.02)) for i in range(80)]
        b = _bot(monkeypatch, spot=t[-1][1], level_ticks=t, tau=10)
        r = NS(slug="s", start_ms=t[0][0] + 60_000,
               end_ms=t[-1][0] + 5_000, strike=100.0)
        ps = []
        for tau in (6.0, 20.0, 30.0, 55.0):
            g = b._locked_tail_p(r, "BTCUSDT", tau)
            assert g is not None, tau
            ps.append(g[0])
        assert ps == sorted(ps, reverse=True), (
            "certainty must fall as more of the paid window stays unprinted: %r" % ps)
        assert ps[0] > 0.99 and ps[-1] < 0.80, (
            "the same gap must span near-certain to coin-flip: %r" % ps)


class TestTheArm:
    def _tape_up(self):
        t = _flat_tape(n=300, px=100.0)
        t += [(t[-1][0] + (i + 1) * 1000, 100.0 + 0.5 * (i + 1)) for i in range(50)]
        return t

    def _run(self, b, r, now_ms):
        import btcpredict.main as m
        b._mkt = {m.MARKETS[0][0]: r}
        real = m.time.time
        m.time.time = lambda: now_ms / 1000.0
        try:
            asyncio.run(b._locked_tail_tick())
        finally:
            m.time.time = real

    def test_disabled_does_nothing(self, monkeypatch) -> None:
        t = self._tape_up()
        b = _bot(monkeypatch, spot=t[-1][1], level_ticks=t, tau=10, enabled=False)
        r = NS(slug="s", start_ms=t[0][0] + 60_000, end_ms=t[-1][0] + 10_000,
               strike=100.0, outcomes=[NS(name="Up", token_id="UP"),
                                       NS(name="Down", token_id="DOWN")])
        self._run(b, r, t[-1][0])
        assert b.buys == []

    def test_shadow_logs_but_never_orders(self, monkeypatch, caplog) -> None:
        """⛔ THE GUARD THAT KEEPS AN UNPROVEN EDGE OFF THE MONEY PATH."""
        t = self._tape_up()
        b = _bot(monkeypatch, spot=t[-1][1], level_ticks=t, tau=10,
                 enabled=True, shadow=True, ask=0.90)
        r = NS(slug="s", start_ms=t[0][0] + 60_000, end_ms=t[-1][0] + 10_000,
               strike=100.0, outcomes=[NS(name="Up", token_id="UP"),
                                       NS(name="Down", token_id="DOWN")])
        with caplog.at_level("INFO"):
            self._run(b, r, t[-1][0])
        assert b.buys == [], "shadow mode must not send an order"
        assert any("LOCKED TAIL" in m for m in caplog.messages)

    def test_it_will_not_pay_above_the_cap(self, monkeypatch) -> None:
        t = self._tape_up()
        monkeypatch.setattr("btcpredict.config.settings.locked_tail_max_px", 0.97)
        b = _bot(monkeypatch, spot=t[-1][1], level_ticks=t, tau=10,
                 enabled=True, shadow=False, ask=0.99)
        r = NS(slug="s", start_ms=t[0][0] + 60_000, end_ms=t[-1][0] + 10_000,
               strike=100.0, outcomes=[NS(name="Up", token_id="UP"),
                                       NS(name="Down", token_id="DOWN")])
        self._run(b, r, t[-1][0])
        assert b.buys == []

    def test_no_edge_no_trade(self, monkeypatch) -> None:
        """The ask must sit below our probability by the margin, NET OF FEE.

        The edge required is set above anything a legal ask could clear, so
        this tests the gate rather than the arithmetic of one price: the first
        version asked 0.969 against a P that was 1.0, which correctly DID
        clear a 0.02 bar and traded.
        """
        t = self._tape_up()
        monkeypatch.setattr("btcpredict.config.settings.locked_tail_min_edge", 0.50)
        b = _bot(monkeypatch, spot=t[-1][1], level_ticks=t, tau=10,
                 enabled=True, shadow=False, ask=0.90)
        r = NS(slug="s", start_ms=t[0][0] + 60_000, end_ms=t[-1][0] + 10_000,
               strike=100.0, outcomes=[NS(name="Up", token_id="UP"),
                                       NS(name="Down", token_id="DOWN")])
        self._run(b, r, t[-1][0])
        assert b.buys == []

    def test_one_shot_per_round(self, monkeypatch) -> None:
        t = self._tape_up()
        b = _bot(monkeypatch, spot=t[-1][1], level_ticks=t, tau=10,
                 enabled=True, shadow=False, ask=0.90)
        r = NS(slug="s", start_ms=t[0][0] + 60_000, end_ms=t[-1][0] + 10_000,
               strike=100.0, outcomes=[NS(name="Up", token_id="UP"),
                                       NS(name="Down", token_id="DOWN")])
        self._run(b, r, t[-1][0])
        self._run(b, r, t[-1][0] + 1000)
        assert len(b.buys) == 1

    def test_live_mode_buys_the_favourite_side(self, monkeypatch) -> None:
        t = self._tape_up()
        b = _bot(monkeypatch, spot=t[-1][1], level_ticks=t, tau=10,
                 enabled=True, shadow=False, ask=0.90)
        r = NS(slug="s", start_ms=t[0][0] + 60_000, end_ms=t[-1][0] + 10_000,
               strike=100.0, outcomes=[NS(name="Up", token_id="UP"),
                                       NS(name="Down", token_id="DOWN")])
        self._run(b, r, t[-1][0])
        assert len(b.buys) == 1
        assert b.buys[0]["side"] == "Up" and b.buys[0]["token_id"] == "UP"
