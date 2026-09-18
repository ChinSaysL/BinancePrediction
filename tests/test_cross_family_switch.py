"""The per-family TRADE switch: recorded, not traded.

WHAT THIS EXISTS FOR. `MARKETS` decides what is RECORDED and `CROSS_TRADED`
decides what may RISK MONEY; they were the same set until `cross_disabled_families`
narrowed the second one. A family that fails `cross_traded()` must keep its
rounds, its quote tape and its websocket book -- exactly the state the three
never-traded families have always been in -- so nothing is lost and re-enabling
is one edit.

WHY eth-updown-5m IS OFF (2026-09-18, operator decision on a measured zero):
over every settled PM-era round, first crossing in tau [5,60], no band and no
floor (`tools/pm_family_signal_check.py`):
    bnb-5m +21.9pp | btc-5m +14.1pp | sol-5m +12.9pp | xrp-5m +11.2pp
    doge-5m +5.8pp | eth-5m -1.4pp (hit 63.2% against a break-even ask of 0.645)
Every other family's trigger is worth 6-22pp more than the book charges; eth's is
worth less than its price, so every entry pays the taker fee for a coin flip.
This DELIBERATELY overrides the repo's own cut rule (eth's halves are -6.6/+3.9,
so it fails the both-halves test and its money sample is 13-16 entries): it is a
risk decision about paying fees on a zero-edge family, recorded as such.
"""
from __future__ import annotations

import pathlib

from btcpredict.config import (CROSS_TRADED, Settings, cross_disabled,
                               cross_traded)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_eth_5m_is_the_disabled_family_by_default():
    assert Settings().cross_disabled_families == "eth-updown-5m"


def test_the_switch_flips_exactly_one_family():
    assert cross_traded("eth-updown-5m") is False
    # eth-15m measured +9.7pp on n=5 and is deliberately left ON
    assert cross_traded("eth-updown-15m") is True
    for fam in ("btc-updown-5m", "sol-updown-5m", "xrp-updown-5m",
                "doge-updown-5m", "bnb-updown-5m"):
        assert cross_traded(fam) is True, fam


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
