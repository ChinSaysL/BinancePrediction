"""Tests for the ported strategy models and the model plumbing."""
import pytest

from btcpredict.models import (MODEL_NAMES, ModelContext, PriceHistory,
                               build_model)
from btcpredict.models.base import prob_from_signal, StrategySignal
from btcpredict.pricing.digital import fair_value


def ctx(closes, volumes=None, takers=None, price=None, strike=None,
        tau_s=None, sigma=None):
    return ModelContext(closes, volumes, takers,
                        price=price, strike=strike, tau_s=tau_s, sigma_1m=sigma)


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

def test_factory_builds_every_model():
    for name in MODEL_NAMES:
        assert build_model(name).name == name


def test_factory_rejects_unknown():
    with pytest.raises(ValueError):
        build_model("not-a-model")


def test_prob_from_signal_mapping():
    assert prob_from_signal(StrategySignal(0, 0, "hold")).emitted is False
    p = prob_from_signal(StrategySignal(1, 0.6, "up"))
    assert p.emitted and p.p_up == pytest.approx(0.5 + 0.5 * 0.6)
    p = prob_from_signal(StrategySignal(-1, 0.9, "down"))
    assert p.emitted and p.p_up < 0.5


def test_predict_never_crashes_on_bad_input():
    for name in MODEL_NAMES:
        m = build_model(name)
        pred = m.predict(ctx([], []))
        assert 0.0 < pred.p_up < 1.0


# ---------------------------------------------------------------------------
# momentum
# ---------------------------------------------------------------------------

def test_momentum_rising_leans_up():
    m = build_model("momentum")
    closes = [100.0 + 0.1 * i for i in range(10)]
    pred = m.predict(ctx(closes))
    assert pred.emitted and pred.p_up > 0.5


def test_momentum_falling_leans_down():
    m = build_model("momentum")
    closes = [100.0 - 0.1 * i for i in range(10)]
    pred = m.predict(ctx(closes))
    assert pred.emitted and pred.p_up < 0.5


def test_momentum_flat_holds():
    m = build_model("momentum")
    assert m.predict(ctx([100.0] * 10)).emitted is False


# ---------------------------------------------------------------------------
# mean reversion
# ---------------------------------------------------------------------------

def test_mean_rev_fades_a_spike():
    m = build_model("mean_rev")
    closes = [100.0] * 40 + [100.0 + i for i in range(1, 11)]  # spike up
    pred = m.predict(ctx(closes))
    assert pred.emitted and pred.p_up < 0.5


def test_mean_rev_holds_in_range():
    m = build_model("mean_rev")
    assert m.predict(ctx([100.0] * 40)).emitted is False


# ---------------------------------------------------------------------------
# sentiment (taker order-flow proxy)
# ---------------------------------------------------------------------------

def test_sentiment_all_buys_bullish():
    m = build_model("sentiment")
    vols = [10.0] * 30
    pred = m.predict(ctx([100.0] * 30, volumes=vols, takers=list(vols)))
    assert pred.emitted and pred.p_up > 0.5


def test_sentiment_all_sells_bearish():
    m = build_model("sentiment")
    vols = [10.0] * 30
    pred = m.predict(ctx([100.0] * 30, volumes=vols, takers=[0.0] * 30))
    assert pred.emitted and pred.p_up < 0.5


def test_sentiment_thin_volume_is_noise():
    m = build_model("sentiment", params={"min_volume_per_bar": 1.0})
    pred = m.predict(ctx([100.0] * 30, volumes=[0.0] * 30, takers=[0.0] * 30))
    assert pred.emitted is False


# ---------------------------------------------------------------------------
# phantom
# ---------------------------------------------------------------------------

def test_phantom_breakout_long():
    m = build_model("phantom")
    closes = [100.0 + 0.5 * i for i in range(80)]  # steady uptrend
    pred = m.predict(ctx(closes))
    assert pred.emitted and pred.p_up > 0.5


def test_phantom_flat_is_vol_filtered():
    m = build_model("phantom")
    assert m.predict(ctx([100.0] * 80)).emitted is False


# ---------------------------------------------------------------------------
# hybrid
# ---------------------------------------------------------------------------

def test_hybrid_combines_subsignals():
    m = build_model("hybrid")
    closes = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0,
              100.0, 100.0, 101.0, 100.0, 101.0, 102.0]
    vols = [10.0] * len(closes)
    pred = m.predict(ctx(closes, volumes=vols, takers=list(vols)))
    assert pred.emitted and pred.p_up > 0.5


def test_hybrid_holds_without_data():
    m = build_model("hybrid")
    assert m.predict(ctx([100.0] * 3)).emitted is False


# ---------------------------------------------------------------------------
# sniper
# ---------------------------------------------------------------------------

def test_sniper_emits_on_strong_trend():
    m = build_model("sniper")
    closes = [100.0 - 0.2 * i for i in range(20)]
    pred = m.predict(ctx(closes))
    assert pred.emitted and pred.p_up < 0.5


def test_sniper_holds_on_flat():
    m = build_model("sniper")
    assert m.predict(ctx([100.0] * 20)).emitted is False


# ---------------------------------------------------------------------------
# btc maker lean
# ---------------------------------------------------------------------------

def test_maker_lean_follows_momentum():
    m = build_model("btc_maker")
    up = m.predict(ctx([100.0 + i for i in range(5)]))
    down = m.predict(ctx([105.0 - i for i in range(5)]))
    assert up.emitted and down.emitted
    assert 0.5 < up.p_up and down.p_up < 0.5


# ---------------------------------------------------------------------------
# digital wrapper
# ---------------------------------------------------------------------------

def test_digital_matches_fair_value():
    m = build_model("digital")
    pred = m.predict(ctx([100.0], price=78450.0, strike=78400.0,
                         tau_s=120.0, sigma=0.0004))
    assert pred.emitted
    assert pred.p_up == pytest.approx(fair_value(78450.0, 78400.0, 120.0, 0.0004))


def test_digital_requires_full_context():
    m = build_model("digital")
    assert m.predict(ctx([100.0])).emitted is False


def test_digital_expiry_never_reaches_certainty():
    m = build_model("digital")
    pred = m.predict(ctx([100.0], price=101.0, strike=100.0, tau_s=0.0, sigma=0.0004))
    assert 0.94 < pred.p_up < 1.0


# ---------------------------------------------------------------------------
# price history
# ---------------------------------------------------------------------------

def _kline(open_ms, close, vol=5.0, taker=3.0):
    return [open_ms, close, close, close, close, vol,
            0, 0, 0, taker, 0, 0]


def test_price_history_seed_merge_tick():
    h = PriceHistory()
    h.seed([_kline(60_000, 100.0), _kline(120_000, 101.0)], drop_forming=False)
    assert len(h) == 2
    h.merge([_kline(120_000, 101.0), _kline(180_000, 102.0)], drop_forming=False)
    assert len(h) == 3                  # dedup on open time
    h.add_tick(102.5)
    c = h.ctx()
    assert c.closes == [100.0, 101.0, 102.0, 102.5]
    assert c.taker_buys == [3.0, 3.0, 3.0]


def test_price_history_seed_drops_forming_bar():
    h = PriceHistory()
    h.seed([_kline(60_000, 100.0), _kline(120_000, 101.0)])
    assert len(h) == 1
