"""The 15m ITM cell: the gates, and the invariants that must not drift.

The point of most of these is not that the arithmetic works -- it is that the
cell stays the PRE-SPECIFIED one. Its evidence (day-clustered t +1.64 on 4
dates) sits below its own bar, and a 288-cell sweep of exactly these knobs
produced a p=0.225 winner that survived every leave-one-date-out fold. So the
defaults are pinned here: if someone edits them to the swept values, a test
fails and says why.
"""
import math

import pytest

from btcpredict.config import settings, shot15_observed
from btcpredict.edge.shot15 import Shot15Signal, evaluate, fair_itm


KW = dict(min_tau_s=300.0, max_tau_s=600.0, min_edge=0.10,
          min_quote=0.05, max_quote=0.99)


def ev(spot=80800.0, strike=80700.0, tau=450.0, sigma=0.00053,
       up=0.65, down=0.38, **kw):
    return evaluate(spot, strike, tau, sigma, up, down, **{**KW, **kw})


# --- the defaults are the pre-specified cell, not the swept one -------------

def test_defaults_are_the_prespecified_cell():
    """⛔ The sweep's winner was tau 180-600, sigma 1800s, edge 0.16,
    ask 0.50-0.80 -- EV +0.2673, day-t +2.85, 4/4 dates, every LODO fold
    holding, and p=0.225 against a best-of-grid null. If these move to those
    values, the arm has been tuned on the sample that cannot support tuning."""
    assert settings.shot15_min_tau_s == 300.0
    assert settings.shot15_max_tau_s == 600.0
    assert settings.shot15_min_edge == 0.10
    assert settings.shot15_min_quote == 0.05
    assert settings.shot15_max_quote == 0.99


def test_mode_cannot_be_configured_to_trade():
    """There is no order path in Bot._shot15; the type must not imply one."""
    assert settings.shot15_mode in ("off", "shadow")
    fields = type(settings).model_fields["shot15_mode"]
    allowed = set(getattr(fields.annotation, "__args__", ()))
    assert allowed == {"off", "shadow"}, allowed


# --- families ---------------------------------------------------------------

def test_only_named_families_observed():
    assert shot15_observed("btc-updown-15m") is True
    assert shot15_observed("btc-updown-5m") is False
    assert shot15_observed(None) is False


def test_empty_family_list_means_none_not_all(monkeypatch):
    """The opposite of `model_families`, deliberately: the 300-600s window is
    longer than a whole 5m round, so "everything" would log only refusals."""
    monkeypatch.setattr(settings, "shot15_families", "")
    assert shot15_observed("btc-updown-15m") is False


# --- the gates --------------------------------------------------------------

def test_fires_when_ask_sits_below_fair():
    s = ev()
    assert s.act and s.side == "Up"
    assert s.edge == pytest.approx(s.pstar - 0.65)
    assert s.edge >= 0.10


def test_buys_the_leading_side_not_the_cheap_one():
    """The cell is a favourite-harvester. Below the strike it must buy DOWN --
    the expensive side -- never the cheap Up. Getting this backwards silently
    turns it into the counter-impulse rule, which was measured NEGATIVE."""
    s = ev(spot=80600.0, up=0.38, down=0.65)
    assert s.side == "Down"
    assert s.ask == 0.65


def test_tau_outside_window_refuses():
    assert not ev(tau=900.0).act
    assert "tau" in ev(tau=900.0).reason
    assert not ev(tau=120.0).act


def test_edge_below_threshold_refuses():
    s = ev(up=0.79, down=0.24)
    assert not s.act and "edge" in s.reason


def test_ask_outside_band_refuses():
    s = ev(up=0.995, down=0.30, min_edge=-1.0)
    assert not s.act and "outside" in s.reason


def test_incoherent_book_refuses_before_edge_is_read():
    """⛔ Two complementary asks summing to <= $1.00 are free arbitrage, i.e.
    corrupt data. A price filter selects exactly those rows -- that is how a
    +0.58/$1 'edge' that passed 23-fold LODO was once manufactured."""
    s = ev(up=0.60, down=0.20)
    assert not s.act and "incoherent" in s.reason


def test_missing_inputs_refuse_rather_than_raise():
    for kwargs in (dict(spot=None), dict(strike=None), dict(tau=None),
                   dict(sigma=None), dict(up=None), dict(down=None)):
        s = ev(**kwargs)
        assert not s.act and s.reason


def test_non_positive_inputs_refuse():
    assert not ev(sigma=0.0).act
    assert not ev(spot=0.0).act


# --- the fair value ---------------------------------------------------------

def test_fair_is_driftless_and_symmetric():
    """Equal displacement either side of the strike must price identically --
    a drift term fitted on the sigma window was rejected as noise."""
    up = fair_itm(80800.0, 80700.0, 450.0, 0.00053)
    dn = fair_itm(80600.0, 80700.0, 450.0, 0.00053)
    assert up == pytest.approx(dn)


def test_fair_at_the_strike_is_a_coin_flip():
    assert fair_itm(80700.0, 80700.0, 450.0, 0.00053) == pytest.approx(0.5)


def test_fair_rises_as_expiry_approaches():
    """Less time left, same displacement -> the leader is safer."""
    far = fair_itm(80800.0, 80700.0, 600.0, 0.00053)
    near = fair_itm(80800.0, 80700.0, 300.0, 0.00053)
    assert near > far


def test_fair_matches_the_replayed_formula():
    """Phi(m / (sigma_1m * sqrt(tau/60))) -- the exact expression measured on
    391 rounds. Drift here silently invalidates the recorded evidence."""
    spot, strike, tau, sigma = 80900.0, 80700.0, 500.0, 0.00047
    m = abs(spot - strike) / strike
    d = m / (sigma * math.sqrt(tau / 60.0))
    want = 0.5 * (1.0 + math.erf(d / math.sqrt(2.0)))
    assert fair_itm(spot, strike, tau, sigma) == pytest.approx(want)


def test_degenerate_tau_or_sigma_is_a_coin_flip_not_a_certainty():
    assert fair_itm(80800.0, 80700.0, 0.0, 0.00053) == pytest.approx(0.5)
    assert fair_itm(80800.0, 80700.0, 450.0, 0.0) == pytest.approx(0.5)


def test_signal_is_frozen():
    s = ev()
    with pytest.raises(Exception):
        s.act = False  # type: ignore[misc]
    assert isinstance(s, Shot15Signal)


# --- the sigma estimator: the part that nearly shipped wrong -----------------

def ticks(n=900, now_ms=1_700_000_000_000, base=80000.0, wobble=3.0):
    return [(now_ms - (n - i) * 1000, base + (i % 7) - wobble) for i in range(n)]


def test_sigma_comes_from_1s_returns_over_900s():
    from btcpredict.edge.shot15 import SIGMA_WINDOW_S, sigma_from_ticks
    assert SIGMA_WINDOW_S == 900.0
    s = sigma_from_ticks(ticks(), 1_700_000_000_000)
    assert s is not None and s > 0


def test_sigma_matches_a_hand_computed_std():
    """Pct returns, sample std (ddof=1), scaled by sqrt(60) to one minute --
    the exact expression the 391-round evidence was measured with."""
    import statistics
    from btcpredict.edge.shot15 import sigma_from_ticks
    now = 1_700_000_000_000
    tk = ticks(now_ms=now)
    per = {t // 1000: p for t, p in tk}
    secs = sorted(per)
    rets = [per[b] / per[a] - 1.0 for a, b in zip(secs, secs[1:])]
    want = statistics.stdev(rets) * math.sqrt(60.0)
    assert sigma_from_ticks(tk, now) == pytest.approx(want)


def test_thin_buffer_returns_none_rather_than_falling_back():
    """⛔ None, never another estimator. A fallback would silently mix two
    cells in one decision series with nothing in the rows saying which."""
    from btcpredict.edge.shot15 import sigma_from_ticks
    assert sigma_from_ticks([], 1_700_000_000_000) is None
    assert sigma_from_ticks(ticks(n=50), 1_700_000_000_000) is None


def test_flat_buffer_has_no_opinion():
    from btcpredict.edge.shot15 import sigma_from_ticks
    now = 1_700_000_000_000
    flat = [(now - (900 - i) * 1000, 80000.0) for i in range(900)]
    assert sigma_from_ticks(flat, now) is None


def test_only_the_trailing_window_counts():
    """Ticks older than 900s must not widen the estimate."""
    from btcpredict.edge.shot15 import sigma_from_ticks
    now = 1_700_000_000_000
    recent = ticks(now_ms=now)
    ancient = [(now - 5_000_000 - i * 1000, 10.0 + i) for i in range(900)]
    assert sigma_from_ticks(ancient + recent, now) == pytest.approx(
        sigma_from_ticks(recent, now))


def test_future_ticks_are_excluded():
    from btcpredict.edge.shot15 import sigma_from_ticks
    now = 1_700_000_000_000
    tk = ticks(now_ms=now) + [(now + 60_000, 99999.0)]
    assert sigma_from_ticks(tk, now) == pytest.approx(
        sigma_from_ticks(ticks(now_ms=now), now))


def test_thin_sigma_is_its_own_reason_not_missing_input():
    """A cold buffer is a startup state that ends, not a broken input, and the
    recorded reason has to say so or warmup looks like a data fault."""
    s = evaluate(80800.0, 80700.0, 450.0, None, 0.65, 0.38, **KW)
    assert not s.act and s.reason == "sigma buffer thin"


def test_tau_is_checked_before_the_sigma_buffer():
    """⛔ Ordering matters in production: the caller drops `tau ...` refusals
    unlogged. If a cold buffer refused first, every out-of-window tick would be
    relabelled and written -- ~600 rows per round during warmup."""
    s = evaluate(80800.0, 80700.0, 800.0, None, 0.65, 0.38, **KW)
    assert s.reason.startswith("tau ")
