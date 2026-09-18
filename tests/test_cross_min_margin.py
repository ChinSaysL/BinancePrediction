"""The trigger's OWN margin, not the spot's -- the gate the losses asked for.

WHY THIS EXISTS. `crossed_side` fires on the FIRST tick past the level, so every
entry is by construction a ZERO-MARGIN entry. On the live record that is not a
tail, it is 76% of all entries, and it is the weak bucket:

    |fc-level|  0-1 bps  n=113  win 56.6%  +0.0054/sh  halves +0.0766/-0.0646
                1-2 bps  n= 21  win 71.4%  +0.1715/sh  halves +0.2247/+0.1231
                2-3 bps  n=  9  win 77.8%  +0.1886/sh  halves +0.2259/+0.1588

WHY THE FLOOR IS 0.25 AND NOT 1.0. That n=149 table shipped 1.0 as "the bucket
boundary". A full sweep over the PM-era record (`tools/pm_param_sweep.py`, 2,246
rounds, one entry per round) re-ran the SAME sweep on each half separately, and
only 0.25 is positive in both:

    floor   n   win    per-share  halves
    0.00  113  47.8%   -0.0097   +0.0363/-0.0402
    0.25   68  55.9%   +0.0538   +0.0270/+0.0675   <- ships
    0.50   43  55.8%   +0.0574   -0.0245/+0.0929
    1.00   19  52.6%   +0.0206   -0.3451/+0.1512   <- the worst row in half 1
    1.50    9  66.7%   +0.1858   -0.1673/+0.3624

The win rate is NOT the target -- it rises with the floor by construction
(1.5 bps "wins" 66.7% on n=9) because the venue's ask rises with it. Note the
two tables use DIFFERENT level definitions (K vs the trailing-60 s mean), so
re-measure rather than re-quote.

THE LIVE FIRES THAT PROMPTED IT (2026-09-17/18, four entries, one win): three sat
at 0.00 bps of margin -- `fc 736.87 vs twap60 736.87`, `fc 1.30 vs twap60 1.30`,
`fc 76998.25 vs twap60 76998.25` -- and all three lost; the only winner sat at
2.0 bps. See config.cross_min_margin_bps.

NOTE the depth floor below still reads the SPOT (`passes_depth_floor(spot, ...)`),
so before this gate nothing measured the variable the trigger actually uses.
"""
from __future__ import annotations

import pathlib

from btcpredict.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_floor_defaults_to_the_value_that_survives_both_halves():
    """0.25 -> 0.50 on 2026-09-18, once the tau window tripled and gave the
    sweep the sample the old note said it needed (68 entries -> 854).

    0.50 PASSES the repo's full bar at the shipped look AND at look 2.0, and
    AGREEs across the two -- the cross-look FLIP is what refused it when it was
    ranked at tau [5,60] on n=42. It also carries the most money of any floor
    (+63.95), the sign flips at 0.5 bps in every tau band independently, and the
    live fills put the refused [0.25,0.50) bucket at -6.028 over 20 fills.
    See config.cross_min_margin_bps for the tables and the kill criterion."""
    assert Settings().cross_min_margin_bps == 0.50


def test_the_floor_is_not_raised_past_where_the_live_fills_agree():
    """⛔ The tape is MONOTONE toward a higher floor and the live record is
    not: entry slippage rises with the margin (a big margin is a big fast move,
    and the venue reprices before the FAK lands), so |m|>=2 bps costs 5.5c/share
    against the ask the replay books and turns the tape's best cell into the
    live record's worst (-0.1788/share on n=22). A future sweep that "improves"
    this on the tape alone is reading that artifact."""
    assert Settings().cross_min_margin_bps <= 1.0


def test_the_gate_reads_the_SIGNAL_not_the_spot():
    """The whole point: `sig_px` is the trigger's own value."""
    src = (ROOT / "btcpredict" / "main.py").read_text()
    assert 'floor = self._ck("min_margin_bps", r)' in src
    assert "margin = 1e4 * (sig_px - level) / level" in src
    assert "if abs(margin) < floor:" in src
    # ...and it must refuse, not merely log
    i = src.index("if abs(margin) < floor:")
    assert "self._cross_refuse(" in src[i:i + 700]
    assert "return" in src[i:i + 700]


def test_the_gate_is_before_the_entry_is_committed():
    """A gate after `_cross_fired` would spend the round's only ticket."""
    src = (ROOT / "btcpredict" / "main.py").read_text()
    assert src.index("if abs(margin) < floor:") < src.index(
        "self._cross_fired[r.slug] = "), \
        "the margin gate must fire before the slot is taken"


def test_the_revert_is_stated_and_the_measurement_rides_with_it():
    cfg = (ROOT / "btcpredict" / "config.py").read_text()
    assert "CROSS_MIN_MARGIN_BPS=0.50" in cfg, "the .env line to set"
    assert "Revert = 0.25 in both places" in cfg
    assert "MEASURED ON THE FC COHORT" in cfg
    assert "0.0766/-0.0646" in cfg, "the weak half must be quoted, not hidden"


def test_the_env_shadowing_is_called_out_where_the_value_lives():
    """⛔ `.env` SHADOWS this default, and editing config.py alone is a no-op.
    That is how the refuted range gate stayed live for a day; the note must say
    so beside the value, not somewhere else in the file."""
    cfg = (ROOT / "btcpredict" / "config.py").read_text()
    i = cfg.index("cross_min_margin_bps: float")
    block = cfg[max(0, i - 7000):i]
    assert "SHADOWS" in block or "shadows" in block
    assert ".env" in block


def test_the_sweep_that_chose_the_value_is_quoted_in_the_config():
    """A parameter must carry the test that rejected its predecessor."""
    cfg = (ROOT / "btcpredict" / "config.py").read_text()
    assert "ONLY FLOOR POSITIVE IN BOTH HALVES" in cfg
    # the losing predecessor's half-1 number, so nobody re-ships 1.0 by memory
    assert "-0.3451" in cfg
    # ...and the win-rate trap, stated where the knob is
    assert "THE WIN RATE IS NOT THE TARGET" in cfg


def test_the_config_says_what_the_arm_s_look_did_to_that_claim():
    """The floor sweep ran at the tools' stale `--look-s 2.0` while the arm runs
    1.0, and at 1.0 the winning row is 0.50, not 0.25. 0.25 is KEPT because that
    ranking flips between looks, but the record must say so rather than quote the
    2.0 table as if it were the arm's own."""
    cfg = (ROOT / "btcpredict" / "config.py").read_text()
    assert 'THE "ONLY 0.25 SURVIVES THE HALVES" CLAIM DOES' in cfg
    assert "+0.0894" in cfg and "+0.0525" in cfg       # both floors' edges
    assert "the ranking FLIPS between the two looks" in cfg
    assert "the FLOOR is not the lever, the WINDOW is" in cfg
