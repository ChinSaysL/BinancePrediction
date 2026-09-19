"""`model_low_fair_stake_mult`: concentrate a fixed budget on the cheap bucket.

btc-updown-5m, 886 entries: fair <= 0.684 earns +0.1006/$ against +0.0294
above it. Reweighting at CONSTANT total capital, fit on days 1-5 and scored on
6-9, 2x gives test +29.29 against flat's +23.62.

NOT significant (p = 0.10 against a calibrated null) and it has no interior
optimum -- "low-fair only" is the in-sample best, so the ceiling is a risk
choice, not a measurement. Shipped on the operator's explicit instruction.
"""
from types import SimpleNamespace

import pytest

import btcpredict.config as cfg
from btcpredict.main import Bot


def _bot():
    b = Bot.__new__(Bot)
    b.bankroll = 100.0
    return b


def _sig(fair, reason="ok"):
    return SimpleNamespace(side="Up", fair=fair, quote=0.60, edge=0.05,
                           hurdle=0.02, ev_per_dollar=0.05, act=True,
                           reason=reason, ev_valid=True)


@pytest.fixture(autouse=True)
def _flat(monkeypatch):
    """Flat $1 base, so any change is the multiplier and nothing else."""
    monkeypatch.setattr(cfg.settings, "sizing_mode", "edge")
    monkeypatch.setattr(cfg.settings, "min_stake_usdt", 1.0)
    monkeypatch.setattr(cfg.settings, "max_stake_usdt", 1.0)
    monkeypatch.setattr(cfg.settings, "stake_cap_usdt", 2.0)
    monkeypatch.setattr(cfg.settings, "stake_tilt", "none")
    monkeypatch.setattr(cfg.settings, "model_low_fair_cut", 0.70)
    monkeypatch.setattr(cfg.settings, "model_low_fair_stake_mult", 2.0)
    yield


class TestTheMultiplier:
    def test_below_the_cut_is_doubled(self):
        assert _bot()._stake_for(_sig(0.62)) == pytest.approx(2.0)

    def test_above_the_cut_is_not(self):
        assert _bot()._stake_for(_sig(0.80)) == pytest.approx(1.0)

    def test_exactly_on_the_cut_counts_as_below(self):
        assert _bot()._stake_for(_sig(0.70)) == pytest.approx(2.0)

    def test_it_is_off_by_default(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_low_fair_cut", 0.0)
        monkeypatch.setattr(cfg.settings, "model_low_fair_stake_mult", 1.0)
        assert _bot()._stake_for(_sig(0.62)) == pytest.approx(1.0)


class TestTheBoundsStillBind:
    def test_the_cap_still_binds_last(self, monkeypatch):
        """A risk bound that a sizing rule can exceed is not a bound. The cap
        once let a $1-capped order spend $5.00 -- see _stake_for."""
        monkeypatch.setattr(cfg.settings, "stake_cap_usdt", 1.0)
        assert _bot()._stake_for(_sig(0.62)) == pytest.approx(1.0), (
            "the multiplier must not be able to breach stake_cap_usdt"
        )

    def test_a_missing_fair_does_not_multiply(self):
        s = SimpleNamespace(side="Up", quote=0.6, edge=0.05, hurdle=0.02,
                            ev_per_dollar=0.05, act=True, reason="ok",
                            ev_valid=True)
        assert _bot()._stake_for(s) == pytest.approx(1.0)


class TestItIsTheModelArmOnly:
    """The other arms choose their side by a different rule, so their `fair`
    is not the same quantity the measurement was made on."""

    @pytest.mark.parametrize("reason", ["cross", "train into btc-updown-5m",
                                        "impulse +4.2bps/3s"])
    def test_other_arms_are_untouched(self, reason):
        assert _bot()._stake_for(_sig(0.62, reason)) == pytest.approx(1.0)

    def test_the_model_arm_is(self):
        for reason in ("ok", "", "Up skipped: 1h ret +0.006 > +0.005"):
            assert _bot()._stake_for(_sig(0.62, reason)) == pytest.approx(2.0)
