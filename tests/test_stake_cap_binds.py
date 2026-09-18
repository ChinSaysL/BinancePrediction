"""The stake cap must bind on EVERY sizing rule, not just the one that takes it.

MEASURED FAILURE, 2026-09-16 17:29:39 UTC, `btc-updown-5m-1789579500`. The
account was configured with `stake_cap_usdt = 1.00`. The bot spent **$5.00**:
500 shares at the $0.01 entry floor (`ceil(5.00 / 0.01)`), plus a $0.3465 fee.
The round settled Up against the Down that was bought, so **-$5.3465 was
realised** -- and there is no `decisions` row for that slug, so the ledger does
not show it. The user's instruction was $1.

Root cause: `_base_stake_for` passes `cap=` to `stake_for_fraction` but
`stake_for_edge` takes no cap at all, and the only clamp sat INSIDE the
`stake_tilt == "inv_z"` branch. So with `stake_tilt = "none"` -- the live
setting -- the cap ran for the fraction mode and silently did not for the edge
mode, which drew its stake straight from [min_stake_usdt, max_stake_usdt].

These tests pin the property rather than the arithmetic: for EVERY mode, the
returned stake is <= the cap.
"""
from __future__ import annotations

import pytest

from btcpredict import main as M
from btcpredict.edge.signal import Signal

SIG = Signal(side="Up", fair=0.62, quote=0.4, edge=0.22, hurdle=0.0,
             ev_per_dollar=0.3, act=True, reason="ok",
             fair_up_model=0.62, ev_valid=True)


class Cfg:
    """A stand-in for the module-level `settings` the sizing functions read."""

    def __init__(self, **kw):
        # Defaults mirror config.Settings, overridden per test.
        self.stake_cap_usdt = 1.0
        self.stake_tilt = "none"
        self.sizing_mode = "edge"
        self.sizing_metric = "ev"
        self.stake_ev_lo = 0.15
        self.stake_ev_hi = 0.45
        self.min_edge = 0.12
        self.stake_edge_hi = 0.35
        self.min_stake_usdt = 5.0
        self.max_stake_usdt = 10.0
        self.min_order_usdt = 1.0
        self.stake_fraction = 0.0224
        self.stake_floor_to_minimum = True
        self.kelly_fraction = 0.25
        self.tilt_reference_z = 0.5
        self.tilt_min = 0.5
        self.tilt_max = 1.5
        for k, v in kw.items():
            setattr(self, k, v)


def _bot(bankroll=22.75):
    bot = M.Bot.__new__(M.Bot)          # skip network setup
    bot.bankroll = bankroll
    return bot


def test_the_measured_five_dollar_case_is_now_one(monkeypatch):
    """The exact configuration that spent $5: edge sizing, a $5 band floor, a
    $1 cap, no tilt. Before the fix this returned 5.0."""
    monkeypatch.setattr(M, "settings", Cfg(sizing_mode="edge",
                                           min_stake_usdt=5.0))
    assert _bot()._stake_for(SIG, 0.0) == pytest.approx(1.0)


@pytest.mark.parametrize("mode", ["edge", "fraction", "kelly", "nonsense"])
def test_the_cap_binds_in_every_mode(monkeypatch, mode):
    """`nonsense` is deliberate: an unrecognised sizing_mode falls through to
    the edge branch, which is how a mode that never reads `cap` can end up
    live after a config typo."""
    monkeypatch.setattr(M, "settings", Cfg(sizing_mode=mode))
    assert _bot()._stake_for(SIG, 0.0) <= 1.0 + 1e-9


@pytest.mark.parametrize("mode", ["edge", "fraction"])
def test_the_cap_binds_with_the_tilt_on(monkeypatch, mode):
    """The tilt is the branch the clamp used to live in. It must not be the
    only path that gets one."""
    monkeypatch.setattr(M, "settings", Cfg(sizing_mode=mode,
                                           stake_tilt="inv_z"))
    assert _bot()._stake_for(SIG, 0.0) <= 1.0 + 1e-9


def test_a_cap_of_zero_still_means_NO_CAP(monkeypatch):
    """The repo's convention for every other limit. The fix must not turn the
    disabled cap into a $0 cap, which would silently stop the bot."""
    monkeypatch.setattr(M, "settings", Cfg(stake_cap_usdt=0.0,
                                           sizing_mode="edge",
                                           min_stake_usdt=5.0,
                                           max_stake_usdt=10.0))
    assert _bot()._stake_for(SIG, 0.0) >= 5.0


def test_a_cap_below_the_venue_minimum_yields_a_skippable_stake(monkeypatch):
    """If the cap and the venue floor leave no legal size, the honest answer is
    to sit out -- the arms already skip on `stake < min_order_usdt`. What must
    NOT happen is rounding up to the floor and ignoring the cap."""
    monkeypatch.setattr(M, "settings", Cfg(stake_cap_usdt=1.0,
                                           min_order_usdt=5.0,
                                           sizing_mode="edge",
                                           min_stake_usdt=5.0))
    stake = _bot()._stake_for(SIG, 0.0)
    assert stake <= 1.0
    assert stake < 5.0            # -> every caller's min_order_usdt check skips


def test_the_size_guard_and_the_stake_cap_agree_on_the_notional():
    """Belt and braces: even if a stake above the cap reached the broker, the
    broker refuses the (shares, price) pair. Measured against the same $1."""
    from btcpredict.venues.polymarket import broker as B
    # $1 at a 0.90 limit is 5 shares = $4.50 -> refused.
    assert B._size_above_cap(5, 0.90, 1.0)
    # At the $0.01 floor the OLD path bought 500 shares = $5.00 -> also refused.
    assert B._size_above_cap(500, 0.01, 1.0)
