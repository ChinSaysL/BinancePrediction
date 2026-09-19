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


def test_the_tau_floor_is_a_strategy_gate_now():
    """⭐ 5.0 -> 30.0 (2026-09-18). Below tau=30 more than HALF the settling
    window is already printed and unchangeable, so a crossing there needs an
    enormous remaining move to drag the 60 s mean over. Live: 7 fills under 30 s
    returned -4.679 at a 14.3% win rate, ~2x the average loss per share, while
    tau[30,60) BEATS the average -- so it is not merely "trade less".
    See config.cross_min_tau_s."""
    from btcpredict.config import Settings
    assert Settings().cross_min_tau_s == 30.0


def test_the_tau_floor_carries_its_mechanical_reason():
    """It is arithmetic, not a fitted cell: the reason must ride with the value
    so nobody re-lowers it on a sweep that cannot see late entries."""
    cfg = (ROOT / "btcpredict" / "config.py").read_text()
    i = cfg.index("cross_min_tau_s: float")
    block = cfg[max(0, i - 3000):i]
    assert "ALREADY PRINTED" in block
    assert "required_remaining" in block
    assert "CROSS_MIN_TAU_S=5.0" in block, "the revert must be stated"


def test_the_price_floor_refuses_the_cheap_cohort():
    """⭐ 0.10 -> 0.45 (2026-09-18), RE-JUSTIFIED 2026-09-19.

    It first shipped on eighteen live fills below 0.45 costing -$10.38. That
    evidence does not stand: the 231 settled PM-era fills span seven live
    configs and none of the 139 in-band ones ran under these gates, so a
    price bucket of live fills measures the configs as much as the price.

    The floor survives anyway, on a mechanism that needs no live outcomes.
    THE CHEAP BOOK IS NOT FILLABLE: at the replay's own entry instants the
    quote is 2.4 s old in book [0.02,0.20) and that side's ask is +0.5407
    two seconds later, against 0.1 s old and +0.0180 in [0.55,0.65). A
    crossing IS the moment a stale cheap quote reprices, so the replay buys
    0.06 and the side is at 0.60 before the order could land. Priced at the
    quoted ask the sub-0.45 cohort is +$58.42; at the ask 2 s later it is
    -$17.13.  See config.cross_min_price."""
    from btcpredict.config import Settings
    assert Settings().cross_min_price == 0.45


def test_the_floor_is_not_an_argument_for_a_HIGHER_one():
    """The fillability finding explains why cheap is bad; it does not imply
    dearer is better, and on unseen days it is not: px_lo 0.50 makes
    +2.29/+10.86 and px [0.55,0.75) makes -3.33/-3.06, against 0.45's
    +14.34/+19.16. The band is fillable at 0.45 and the edge is inside it."""
    from btcpredict.config import Settings
    assert Settings().cross_min_price == 0.45
    assert Settings().cross_max_fill_px == 0.65


def test_the_fill_ceiling_was_reverted_to_065():
    """⛔ 0.65 -> 0.70 shipped and was REVERTED the same day. The joint search
    is not wrong about the tape -- 0.70 is the interior optimum there -- but the
    tape prices entries at the QUOTED ASK and the live FAK sweeps up to whatever
    ceiling it is given. 59% of that day's 63 fills landed above the decided
    price, 25% by 5c+, and eth-1789811400 filled 0.58 against a 0.47 decision
    for an ev_at_fill of -0.2041. A knob that widens what the arm MAY pay
    cannot be chosen on a tape that assumes what it DOES pay.
    See config.cross_max_fill_px."""
    from btcpredict.config import Settings
    assert Settings().cross_max_fill_px == 0.65




def test_the_thin_break_even_at_the_new_ceiling_is_recorded():
    """A fill AT 0.70 needs 71.5% to break even against 66.6% at 0.65, and the
    measured win rate for asks in [0.65,1.00) inside the arm's margin band is
    72.5%. About one point of cushion, so the number has to ride with the knob
    or the next person will widen it further on the win-rate column alone --
    which rises monotonically to 0.90 while the MONEY falls."""
    cfg = (pathlib.Path(__file__).resolve().parents[1]
           / "btcpredict" / "config.py").read_text()
    j = cfg.index("cross_max_fill_px: float =")
    block = cfg[max(0, j - 3000):j]
    assert "71.5%" in block and "72.5%" in block
    assert "CROSS_MAX_FILL_PX=0.65" in block, "the revert must be stated"
    assert "does not model" in block.lower() or "DOES NOT LICENSE" in block


def test_the_unfillability_of_the_cheap_book_is_recorded():
    """The joint search WANTS the floor opened to 0.10 -- it is the best cell
    on every price sweep. The reason that cell is a mirage has to ride beside
    the knob or it will be 'fixed' back on the next sweep."""
    cfg = (ROOT / "btcpredict" / "config.py").read_text()
    i = cfg.index("cross_min_price: float")
    block = cfg[max(0, i - 6000):i + 3000]
    assert "NOT FILLABLE" in block
    assert "+0.5407" in block, "the 2s repricing move must be stated"
    assert "seven configs" in block, "the contamination must be stated"


def test_the_ladder_artifact_is_recorded_beside_the_floor():
    """The floor contradicts every tape sweep, so the REASON the tape is wrong
    must ride with it or someone will 'fix' the floor back."""
    cfg = (ROOT / "btcpredict" / "config.py").read_text()
    i = cfg.index("cross_min_price: float")
    # A GENEROUS window. `cross_ladder_band_pad` was later inserted directly
    # above this knob and pushed the note past a tighter offset, failing this
    # test although nothing had been lost. The window is the whole note block,
    # not a guess at its length.
    block = cfg[max(0, i - 12000):i]
    assert "WEBSOCKET ladder" in block
    assert "CROSS_MIN_PRICE=0.10" in block, "the revert must be stated"


def test_the_margin_ceiling_ships_OFF_because_its_evidence_did_not_survive():
    """⛔ RETIRED 2026-09-19, the same day it shipped.

    It was chosen by bucketing live fills on their realised margin. The 231
    settled PM-era fills span SEVEN live configs and NONE of the 139 in the
    [0.45,0.65] band ran under the gates those buckets were used to justify, so
    a bucket only contains a fill because the config of the moment admitted it.
    On the config-independent tape, charged the live-measured ladder, every
    finite ceiling in a 768-cell joint search loses to no ceiling at both
    look_s 1.0 and 2.0 -- ceiling off makes +19.00 (t+1.68) against 1.5's
    +12.53 (t+1.27), and +10.00 against +5.92 on days the choice never saw.
    The MECHANISM stays in main.py and is exercised below; only the default
    changes, so re-enabling is one env var.  See config.cross_max_margin_bps."""
    from btcpredict.config import Settings
    assert Settings().cross_max_margin_bps == 0.0


def test_the_ceiling_mechanism_survives_its_retirement():
    """0 = off is the repo's convention, so the gate must treat it as off
    rather than as a ceiling of zero that refuses everything."""
    src = (ROOT / "btcpredict" / "main.py").read_text()
    i = src.index('cap = self._ck("max_margin_bps", r)')
    assert "cap > 0" in src[i:i + 300], "0 must mean OFF, not 'refuse all'"


def test_the_ceiling_is_enforced_and_refuses_not_just_logs():
    src = (ROOT / "btcpredict" / "main.py").read_text()
    assert 'cap = self._ck("max_margin_bps", r)' in src
    i = src.index("if abs(margin) > cap:")
    blk = src[i:i + 700]
    assert "self._cross_refuse(" in blk and "return" in blk


def test_the_ceiling_fires_after_the_floor_and_before_the_slot_is_taken():
    """Both margin gates must sit before `_cross_fired` is stamped, or a
    refused crossing still spends the round's only ticket."""
    src = (ROOT / "btcpredict" / "main.py").read_text()
    assert src.index("if abs(margin) < floor:") < src.index("if abs(margin) > cap:")
    assert src.index("if abs(margin) > cap:") < src.index("self._cross_fired[r.slug] = ")


def test_two_independent_sweeps_have_asked_to_lower_the_price_floor():
    """Both were artifacts, and the guard has to outlive the memory of them.

    `pm_joint_chain` wanted 0.10 (it prices at the quoted ask, which the cheap
    book does not honour). `pm_two_book` wants 0.35 (it draws one gap
    distribution at every price, so it cannot see the cheap book being stale
    either). A 0.35 ladder floor adds 171 entries of which 27% PAY under 0.45,
    where the two-second reprice is +0.2174 to +0.5407.

    If a third sweep asks, check whether it models PRICE-DEPENDENT staleness
    before believing it.  See config.cross_min_price."""
    from btcpredict.config import Settings
    assert Settings().cross_min_price == 0.45
    cfg = (pathlib.Path(__file__).resolve().parents[1]
           / "btcpredict" / "config.py").read_text()
    j = cfg.index("cross_min_price: float =")
    block = cfg[max(0, j - 7000):j]
    assert "pm_two_book" in block, "the second refuted sweep must be named"
    assert "27%" in block, "and why it was refused"


def test_the_limit_buffer_is_recorded_as_inert():
    """It reads like a live risk knob and is not one while the venue re-base is
    on -- `venue + buffer` always covers `venue`, so the fill test is just
    `venue <= cross_max_fill_px`. Without this note someone tunes it for a day
    and measures nothing, which is exactly what happened on 2026-09-19."""
    cfg = (pathlib.Path(__file__).resolve().parents[1]
           / "btcpredict" / "config.py").read_text()
    j = cfg.index("cross_limit_buffer_inv: float =")
    block = cfg[max(0, j - 2000):j]
    assert "INERT" in block and "cross_venue_take" in block
    assert "DID NOTHING" in block, "the mis-credited fix must be recorded"
