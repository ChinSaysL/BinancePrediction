"""The per-family TRADE switch: recorded, not traded.

WHY IT EXISTS. `MARKETS` decides what is RECORDED and `CROSS_TRADED`
decides what may RISK MONEY; they were the same set until `cross_disabled_families`
narrowed the second one. A family that fails `cross_traded()` must keep its
rounds, its quote tape and its websocket book -- exactly the state the three
never-traded families have always been in -- so nothing is lost and re-enabling
is one edit.

eth-updown-5m WAS the disabled family for part of 2026-09-18 and was taken off
the same day, so the switch currently disables NOTHING. It went on because every
settled PM-era round, first crossing in tau [5,60], no band and no floor
(`tools/pm_family_signal_check.py`) gave it -1.4pp against a break-even ask of
0.645 while every other family's trigger was worth 6-22pp more than its price --
a fee paid for a coin flip, and an explicit OPERATOR risk decision, since it
overrode the repo's both-halves cut rule (eth's halves were -6.6/+3.9).

It came off because both inputs of that table changed underneath it: the fc
wire it was measured on averaged the WRONG WINDOW (`[ts-60, ts]` instead of
`[bell-60, ts]`), and the arm stopped trading tau [5,60] when the window moved
to [5,180]. In the window the arm does trade, eth is n=170 hit 64.7%
be-ask 0.546 edge +10.1pp, per-share +0.0941, t +2.46, halves +8.9/+11.3,
repeating by day and by side -- and at tau [5,60] on the fixed wire it is still
-4.7pp, which is why the WINDOW, not the fix, is what moved. The replay makes
it the largest 5m family at the 0.5 bps floor (n=113, +$19.33, t +3.89).

What is NOT measured: eth has never been live-FILLED, so its venue ask is
untested (the xrp case showed one 0.16 above our ladder) and the replay pays the
recorded ask. Kill criterion: eth-5m live fills at <= 0/share over ~50 entries
puts the prefix back.
"""
from __future__ import annotations

import pathlib

from btcpredict.config import (CROSS_TRADED, Settings, cross_disabled,
                               cross_traded)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_traded_set_is_the_three_deepest_samples():
    """⭐ NARROWED 2026-09-19 from "nothing disabled" to the three families with
    the most evidence -- eth-5m (n=240), bnb-5m (n=220), btc-5m (n=175).

    A RISK decision on a small account, not an edge one: every family dropped
    has 141 entries or fewer and eth-15m has 25. bnb-5m is KEPT at t +0.78 while
    bnb-15m (+10.23, t +2.46) is DROPPED, which is the tell that this selects on
    sample depth and not on return. From $6.36 at the $1 venue minimum it gives
    up ~29% of return (+99.82 vs +140.03) to never dip below the starting
    balance (low 6.36 vs 4.11), cut max drawdown 12.13 -> 9.50 and shorten the
    worst losing streak 8 -> 6.  See config.cross_disabled_families."""
    assert cross_disabled() == frozenset({
        "xrp-updown-5m", "sol-updown-5m", "doge-updown-5m",
        "eth-updown-15m", "btc-updown-15m", "bnb-updown-15m"})
    for fam in ("eth-updown-5m", "btc-updown-5m", "bnb-updown-5m"):
        assert cross_traded(fam) is True, fam


def test_the_dropped_families_cannot_order():
    """Recorded, quoted and book-followed -- but never risking money."""
    for fam in ("xrp-updown-5m", "sol-updown-5m", "doge-updown-5m",
                "eth-updown-15m", "btc-updown-15m", "bnb-updown-15m"):
        assert cross_traded(fam) is False, fam


def test_the_switch_can_still_be_emptied():
    """The revert in the note has to actually work."""
    import btcpredict.config as cfg
    from unittest.mock import patch
    with patch.object(cfg.settings, "cross_disabled_families", ""):
        assert cross_disabled() == frozenset()
        assert cross_traded("xrp-updown-5m") is True


def test_the_switch_still_flips_exactly_one_family(monkeypatch):
    """The mechanism the eth-5m episode exercised must keep working."""
    import btcpredict.config as cfg
    monkeypatch.setattr(cfg.settings, "cross_disabled_families",
                        "eth-updown-5m")
    assert cross_traded("eth-updown-5m") is False
    # With the string overridden to ONE family, everything else trades again --
    # which is the mechanism, independent of what ships in the string.
    for fam in ("btc-updown-5m", "sol-updown-5m", "xrp-updown-5m",
                "doge-updown-5m", "bnb-updown-5m", "eth-updown-15m"):
        assert cross_traded(fam) is True, fam


def test_eth_5m_is_traded_by_default():
    """eth-5m stays the arm's best family (n=240, 70% win, t +3.84). eth-15m
    is no longer traded -- n=25 and -3.58 is not a measurement either way, and
    on a small account an unmeasured family is a risk, not an option."""
    assert cross_traded("eth-updown-5m") is True
    assert cross_traded("eth-updown-15m") is False


def test_a_family_outside_CROSS_TRADED_is_never_traded():
    assert cross_traded("hype-updown-5m") is False
    assert cross_traded("") is False
    assert cross_traded(None) is False


def test_the_knob_is_a_comma_list_and_empty_means_nothing_disabled(monkeypatch):
    """`""` must re-enable everything -- that is the documented revert."""
    import btcpredict.config as cfg
    monkeypatch.setattr(cfg.settings, "cross_disabled_families", "")
    assert cross_disabled() == frozenset()
    assert cross_traded("eth-updown-5m") is True
    monkeypatch.setattr(cfg.settings, "cross_disabled_families",
                        "eth-updown-5m, bnb-updown-5m ,")
    assert cross_disabled() == frozenset({"eth-updown-5m", "bnb-updown-5m"})
    assert cross_traded("bnb-updown-5m") is False


def test_the_recording_path_still_runs_for_every_market():
    """The switch must gate `trade=`, NOT the loop over MARKETS -- otherwise the
    disabled family stops being recorded, which is the opposite of the intent."""
    src = (ROOT / "btcpredict" / "main.py").read_text()
    assert "for prefix, sym, _ in MARKETS:" in src
    assert "trade=cross_traded(prefix)," in src
    assert "trade=(prefix in CROSS_TRADED)," not in src, \
        "the raw membership test must not remain: it ignores the switch"


def test_the_disabled_family_is_still_in_MARKETS():
    from btcpredict.config import MARKETS
    assert "eth-updown-5m" in {p for p, _, _ in MARKETS}
    assert "eth-updown-5m" in CROSS_TRADED      # the tape, not the risk


def test_the_decision_and_its_revert_are_recorded_where_the_knob_is():
    cfg = (ROOT / "btcpredict" / "config.py").read_text()
    assert "cross_disabled_families" in cfg
    assert "REVERT: `cross_disabled_families=\"\"`" in cfg
    assert "OPERATOR's risk decision" in cfg, \
        "an override of the repo's cut rule must say so"
    assert "+21.9pp" in cfg and "-1.4pp" in cfg, \
        "the family table that decided it must ride with the knob"
    assert "+10.1pp" in cfg and "+$19.33" in cfg, \
        "the re-measurement that undid it must ride there too"
    assert "KILL CRITERION" in cfg, \
        "a risk decision needs a number that ends it"
