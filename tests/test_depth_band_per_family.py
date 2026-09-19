"""Every family the bot quotes must have a NON-EMPTY depth band.

Two gates bracket the "how far through the strike" measurement (main.py, the
min_depth_ticks block):

    floor = min_depth_ticks * tick_size          <- SPOT dollars
    ceil  = max_depth_bps / 1e4 * spot           <- bps of spot

`tick_size` is the SPOT price tick of the UNDERLYING (BTC 0.01, XRP 0.0001,
DOGE 0.00001), not the 0.01 probability tick on the CLOB. Comparing a
symbol-dependent floor against a symbol-independent ceiling is fine on the
symbols whose tick is small relative to their price and fatal on the ones where
it is not:

    family   spot      floor with a global 0.01      ceiling   band
    btc      76000     0.0013 bps                    5.0       open
    bnb        726     0.14 bps                      5.0       open
    xrp       1.28     78.0 bps                      5.0       ** EMPTY **
    doge      0.08     1250.0 bps                    5.0       ** EMPTY **

An empty band is not a strict filter, it is an OFF SWITCH, and a refusal is not
an error -- so both families were inert with nothing in the log to say so.
Found 2026-09-16 by reading the two gates side by side. The remedy is the
per-family `tick_size` override, which is what these tests pin.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from btcpredict import config as C

#: A representative spot for each underlying. The test is about ORDERS OF
#: MAGNITUDE, so an approximate price is enough and the assertion is not
#: sensitive to it (a 2x error moves XRP's floor by 2x, not by 100x).
SPOT = {"btc": 76000.0, "eth": 2450.0, "bnb": 726.0, "sol": 100.5,
        "xrp": 1.30, "doge": 0.0813}


def _settings():
    return C.Settings()


def _ticks(prefix: str) -> float:
    """The floor in TICKS, honouring the per-market override.

    `min_depth_ticks` is overridable per market like every other `cross_*`
    knob (main.py `_ck` -> `config.cross_knob`), which is how the three
    reconstructed-strike families get two ticks instead of one.
    """
    s = _settings()
    ov = s.cross_overrides.get(prefix, {})
    return ov.get("min_depth_ticks") or s.cross_min_depth_ticks


def _knobs(prefix: str) -> tuple[float, float, float]:
    """(floor_bps, ceil_bps, spot) for a family, from the live settings."""
    s = _settings()
    ov = s.cross_overrides.get(prefix, {})
    tick = ov.get("tick_size") or s.cross_tick_size
    # ⭐ 0 = OFF for this ceiling (shipped 2026-09-19), so the band is
    # unbounded above. Represent that as +inf rather than 0: every assertion
    # below is about the floor not EMPTYING the band, and an off ceiling makes
    # that impossible, not certain. Leaving it as 0 would have turned the
    # guard into "the band is always empty".
    ceil_bps = ov.get("max_depth_bps") or s.cross_max_depth_bps
    if not ceil_bps:
        ceil_bps = float("inf")
    sym = C.SYMBOL_BY_PREFIX[prefix].lower().removesuffix("usdt")
    spot = SPOT[sym]
    floor_bps = 1e4 * _ticks(prefix) * tick / spot
    return floor_bps, ceil_bps, spot


def _families() -> list[str]:
    return [p for p, _s, _tf in C.MARKETS]


def test_the_floor_is_measured_in_spot_not_in_probability_ticks():
    """A guard against the units being swapped back. 0.01 is the CLOB tick and
    it is ALSO BTC's spot tick, which is exactly why the confusion survived."""
    s = _settings()
    assert s.cross_tick_size == 0.01
    # On BTC the two coincide, so BTC can never reveal the bug.
    assert _knobs("btc-updown-5m")[0] < 0.01
    # On XRP they differ by 100x, and that is the whole failure.
    assert _settings().cross_overrides["xrp-updown-5m"]["tick_size"] == 0.0001


@pytest.mark.parametrize("prefix", _families())
def test_every_family_has_a_non_empty_depth_band(prefix):
    """The real assertion: floor < ceil, or the arm never trades that family."""
    floor_bps, ceil_bps, spot = _knobs(prefix)
    assert floor_bps < ceil_bps, (
        f"{prefix}: floor {floor_bps:.3f} bps >= ceiling {ceil_bps:.2f} bps "
        f"-- the band is EMPTY and the arm is inert (spot {spot})")


@pytest.mark.parametrize("prefix", _families())
def test_the_band_is_still_a_band(prefix):
    """And not a hairline: the ceiling must be more than one tick of spot, or
    every crossing is refused for the opposite reason."""
    floor_bps, ceil_bps, spot = _knobs(prefix)
    ceil_usd = ceil_bps / 1e4 * spot
    s = _settings()
    ov = s.cross_overrides.get(prefix, {})
    tick = ov.get("tick_size") or s.cross_tick_size
    assert ceil_usd > tick, f"{prefix}: ceiling ${ceil_usd:.6f} <= tick {tick}"


def test_the_global_tick_WOULD_have_left_xrp_and_doge_inert():
    """Why the override is not cosmetic. Re-derive the old floor with the
    global 0.01 and show it swamps the ceiling -- the measured state of both
    families before this fix."""
    s = _settings()
    for prefix, sym in (("xrp-updown-5m", "xrp"), ("doge-updown-5m", "doge")):
        old_floor_bps = 1e4 * s.cross_min_depth_ticks * 0.01 / SPOT[sym]
        # Against the ceiling THAT WAS IN FORCE, not today's. This records a
        # historical fact -- the floor swamped a 5 bps ceiling and left both
        # families inert -- and it has to keep recording it now that the
        # ceiling ships disabled (cross_max_depth_bps 5 -> 99, 2026-09-19).
        HISTORIC_CEILING_BPS = 5.0
        assert old_floor_bps > HISTORIC_CEILING_BPS, (prefix, old_floor_bps)
        new_floor_bps, ceil_bps, _ = _knobs(prefix)
        assert new_floor_bps < ceil_bps


def test_no_family_overrides_the_spot_tick_unnecessarily():
    """No `tick_size` override should exist where the global 0.01 already
    worked: that number is a claim about the family's SPOT TICK, and a wrong
    one is silent (see the two empty-band tests above).

    This pins `tick_size` only. `min_depth_ticks` is a separate, deliberate
    override on sol/xrp/doge -- see the reconstructed-strike tests below.
    """
    ov = _settings().cross_overrides
    for prefix in ("btc-updown-5m", "eth-updown-5m", "bnb-updown-5m",
                   "sol-updown-5m", "btc-updown-15m", "eth-updown-15m",
                   "bnb-updown-15m"):
        assert "tick_size" not in ov.get(prefix, {}), prefix


#: The three families whose strike is RECONSTRUCTED from the spot tape rather
#: than taken from the wrapper's exact `variantData.startPrice`: Binance's
#: prediction feed carries btc/eth/bnb up/down only, so nothing else supplies
#: one (see main.py `_pm_strike_backfill`).
RECONSTRUCTED = ("sol-updown-5m", "xrp-updown-5m", "doge-updown-5m")


@pytest.mark.parametrize("prefix", RECONSTRUCTED)
def test_a_reconstructed_strike_requires_more_than_one_tick(prefix):
    """ONE tick on these three is 0.99 / 0.77 / 1.23 bps of spot, and the only
    accuracy bound that exists for the reconstructed strike is p99 < 1 bp.
    So a one-tick crossing sits entirely inside one error bar of the level it
    is measured against and the side is not resolvable. Two ticks clears it."""
    ticks = _ticks(prefix)
    floor_bps, _ceil, spot = _knobs(prefix)
    assert ticks >= 2.0, (
        f"{prefix}: floor is {ticks:.0f} tick(s) = {floor_bps:.2f} bps at spot "
        f"{spot} -- a one-tick crossing is inside the strike's error bar")


@pytest.mark.parametrize("prefix", RECONSTRUCTED)
def test_the_raised_floor_does_not_empty_the_band(prefix):
    """Raising a floor is exactly how xrp and doge became inert for their whole
    life (a refusal is not an error, so it was silent). Keep the two gates
    apart."""
    floor_bps, ceil_bps, spot = _knobs(prefix)
    assert 0 < floor_bps < ceil_bps, (prefix, floor_bps, ceil_bps, spot)


def test_the_exact_strike_families_keep_a_one_tick_floor():
    """btc/eth/bnb take the wrapper's exact startPrice -- measured to 0.042 /
    0.091 / 0.114 bps -- so a one-tick floor is correct there and the
    reconstructed-strike rule must not leak onto them."""
    for prefix in ("btc-updown-5m", "eth-updown-5m", "bnb-updown-5m",
                   "btc-updown-15m", "eth-updown-15m", "bnb-updown-15m"):
        assert _ticks(prefix) == 1.0, prefix
        assert _knobs(prefix)[0] < 1.0, prefix


# --- ONE definition of the floor, shared by the arm and its replays ---------
# The gates above are only as good as the code that APPLIES them. Three tools
# used to read the GLOBAL `S.cross_min_depth_ticks * S.cross_tick_size` and a
# fixed `- 1e-12` epsilon, so on sol/xrp/doge they replayed a floor the live arm
# does not have (with the global 0.01 tick, xrp and doge are an EMPTY band) and
# refused crossings sitting exactly on the floor. A replay that prices a
# different gate than the bot runs is a measurement of nothing.

def test_the_floor_conversion_is_per_market():
    """`depth_in_ticks` must go through `cross_knob`, not the global setting."""
    from btcpredict.main import depth_in_ticks
    # One XRP tick is 0.0001, so 0.0001 of spot is ONE tick on xrp...
    assert depth_in_ticks(1.3001, 1.3000, "xrp-updown-5m") == pytest.approx(1.0)
    # ...and 1/100 of a tick on btc, whose tick is the global 0.01.
    assert depth_in_ticks(76000.01, 76000.00, "btc-updown-5m") == pytest.approx(1.0)


@pytest.mark.parametrize("prefix,spot,tick", [
    ("btc-updown-5m", 76000.0, 0.01),
    ("eth-updown-5m", 2450.0, 0.01),
    ("sol-updown-5m", 100.5, 0.01),
    ("xrp-updown-5m", 1.30, 0.0001),
    ("doge-updown-5m", 0.0813, 0.00001),
])
def test_a_crossing_exactly_on_the_floor_passes_and_a_hair_under_fails(
        prefix, spot, tick):
    """THE FLOAT BUG, pinned across five orders of magnitude of price.

    `abs(spot - strike)` is a difference of two doubles, so its error scales
    with the SPOT: 1.46e-11 at BTC's ~7.6e4, which is 14x the fixed 1e-12 the
    comparison used to subtract. Measured live, that refused 3 crossings that
    sat exactly ON the floor (logs read "only 1 tick(s) ... floor is 1").
    """
    from btcpredict.main import passes_depth_floor
    floor = _ticks(prefix)
    at = spot + floor * tick          # exactly ON the floor
    under = spot + (floor - 0.5) * tick   # half a tick shallower
    assert passes_depth_floor(at, spot, prefix), (
        f"{prefix}: a crossing exactly on the floor must pass")
    assert not passes_depth_floor(under, spot, prefix), (
        f"{prefix}: half a tick under the floor must still be refused")


def test_the_floor_zero_and_off_switch_is_honoured():
    """0 = OFF, the repo convention. It must not read `depth >= -eps`."""
    from btcpredict.main import passes_depth_floor
    assert passes_depth_floor(76000.0, 76000.0, "btc-updown-5m", floor_ticks=0.0)
    # and the shipped value does NOT admit spot == strike on any family


@pytest.mark.parametrize("prefix", [p for p, _s, _tf in C.MARKETS])
def test_a_strike_touching_spot_is_refused_on_every_family(prefix):
    """Not just btc: the floor's whole job is to refuse the touch."""
    from btcpredict.main import passes_depth_floor
    assert not passes_depth_floor(spot_of(prefix), spot_of(prefix), prefix)


def spot_of(prefix: str) -> float:
    return SPOT[C.SYMBOL_BY_PREFIX[prefix].lower().removesuffix("usdt")]


def test_the_replay_tools_use_the_shared_floor():
    """A tool that computes the floor itself WILL drift from the arm.

    Pinned textually because the failure is silent: the tools still run and
    still print a table, it is just a table about a different strategy.
    Comments are stripped first -- the tools' own comments quote the
    expression they were fixed away from.
    """
    root = Path(C.__file__).resolve().parents[1]
    for name in ("cross_whipsaw_guard", "cross_wait_sweep", "cross_15m_calibrate"):
        raw = (root / "tools" / f"{name}.py").read_text()
        code = "\n".join(ln for ln in raw.splitlines()
                         if not ln.strip().startswith("#"))
        assert "passes_depth_floor" in code, (
            f"tools/{name}.py must apply the floor via main.passes_depth_floor")
        assert "cross_min_depth_ticks" not in code, (
            f"tools/{name}.py reads the GLOBAL floor instead of cross_knob's "
            f"-- that is an empty band on xrp/doge")
        assert "cross_tick_size" not in code, (
            f"tools/{name}.py reads the GLOBAL tick_size; the tick is per-market")


def test_a_missing_book_age_is_not_a_stale_book():
    """MY OWN BUG, 2026-09-17, and it produced a wrong verdict in FINDINGS.

    `book_age_ms` is NULL for every quote before 2026-09-16 (269k of 1.23M rows
    in the 09-15 snapshot). A filter written `age is None -> drop` therefore
    deleted the ENTIRE earlier era -- which is exactly where the 5-tick floor's
    refusals live, i.e. the cohort under measurement. The tool then reported
    "the md's +0.2311 does not reproduce" from a population that no longer
    contained those rows, and the era-restored run shows +0.155 at the
    reachable price.

    Dropping a row for MISSING metadata is only conservative if you know the
    metadata would have failed.
    """
    from tools.pm_tick_floor_check import _age_ok
    assert _age_ok(None, 2000), "a missing age must not delete the row"
    assert _age_ok(1999, 2000)
    assert not _age_ok(2001, 2000)
    assert _age_ok(None, 0), "0 disables the filter entirely"
