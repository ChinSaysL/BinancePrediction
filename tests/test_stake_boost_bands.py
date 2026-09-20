"""`model_stake_boost_bands`: boost TWO bands with a dead zone between them.

The single cut at 0.70 shipped 2026-09-19 and was reverted the same night: it
spent the multiplier on 0.60-0.70, which contributes +$0.21 across 162 entries.
The measured structure is two separated bands, which a cut cannot express.

At constant capital, fit on the first half and scored on the second:
    flat                      test +16.59
    2x <=0.70 (reverted)      test +19.06
    2x 0.50-0.60 + 0.70-0.80  test +24.54   p = 0.036 vs a calibrated null
"""
from types import SimpleNamespace

import pytest

import btcpredict.config as cfg
from btcpredict.config import stake_boost_bands, stake_boosted
from btcpredict.main import Bot


def _sig(fair, reason="ok"):
    return SimpleNamespace(side="Up", fair=fair, quote=0.60, edge=0.05,
                           hurdle=0.02, ev_per_dollar=0.05, act=True,
                           reason=reason, ev_valid=True)


@pytest.fixture(autouse=True)
def _flat(monkeypatch):
    monkeypatch.setattr(cfg.settings, "sizing_mode", "edge")
    monkeypatch.setattr(cfg.settings, "min_stake_usdt", 1.0)
    monkeypatch.setattr(cfg.settings, "max_stake_usdt", 1.0)
    monkeypatch.setattr(cfg.settings, "stake_cap_usdt", 2.0)
    monkeypatch.setattr(cfg.settings, "stake_tilt", "none")
    monkeypatch.setattr(cfg.settings, "model_low_fair_cut", 0.0)
    monkeypatch.setattr(cfg.settings, "model_low_fair_stake_mult", 1.0)
    monkeypatch.setattr(cfg.settings, "model_stake_boost_bands",
                        "0.50-0.60,0.70-0.80")
    monkeypatch.setattr(cfg.settings, "model_stake_boost_mult", 2.0)
    yield


class TestParsing:
    def test_two_bands(self):
        assert stake_boost_bands() == ((0.50, 0.60), (0.70, 0.80))

    def test_empty_is_off(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_stake_boost_bands", "")
        assert stake_boost_bands() == ()
        assert stake_boosted(0.55) is False

    @pytest.mark.parametrize("bad", ["0.6", "abc-def", "0.8-0.5", "-",
                                     "1.2-1.5", "0.0-0.5", ",,"])
    def test_malformed_is_dropped_never_guessed(self, monkeypatch, bad):
        """A typo must cost the boost, not silently widen it onto a band the
        measurement never covered."""
        monkeypatch.setattr(cfg.settings, "model_stake_boost_bands", bad)
        assert stake_boost_bands() == (), bad

    def test_a_good_band_survives_a_bad_neighbour(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_stake_boost_bands",
                            "0.50-0.60,garbage,0.70-0.80")
        assert stake_boost_bands() == ((0.50, 0.60), (0.70, 0.80))


class TestTheDeadZoneIsNotBoosted:
    """0.60-0.70 contributes +$0.21 across 162 entries. Boosting it is the
    mistake the reverted single cut made."""

    @pytest.mark.parametrize("fair", [0.60, 0.62, 0.65, 0.68, 0.699])
    def test_dead_zone_stays_flat(self, fair):
        assert Bot.__new__(Bot)._stake_for(_sig(fair)) == pytest.approx(1.0)

    @pytest.mark.parametrize("fair", [0.50, 0.55, 0.599, 0.70, 0.75, 0.799])
    def test_the_bands_are_boosted(self, fair):
        assert Bot.__new__(Bot)._stake_for(_sig(fair)) == pytest.approx(2.0)

    @pytest.mark.parametrize("fair", [0.45, 0.80, 0.85, 0.95])
    def test_outside_both_bands_stays_flat(self, fair):
        assert Bot.__new__(Bot)._stake_for(_sig(fair)) == pytest.approx(1.0)

    def test_boundaries_are_half_open(self):
        b = Bot.__new__(Bot)
        assert b._stake_for(_sig(0.60)) == pytest.approx(1.0), "hi is exclusive"
        assert b._stake_for(_sig(0.70)) == pytest.approx(2.0), "lo is inclusive"


class TestTheBoundsAndScope:
    def test_the_cap_still_binds_last(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "stake_cap_usdt", 1.0)
        assert Bot.__new__(Bot)._stake_for(_sig(0.55)) == pytest.approx(1.0), (
            "a risk bound a sizing rule can exceed is not a bound"
        )

    @pytest.mark.parametrize("reason", ["cross", "train into x", "impulse +4bps/3s"])
    def test_other_arms_are_untouched(self, reason):
        assert Bot.__new__(Bot)._stake_for(_sig(0.55, reason)) == pytest.approx(1.0)

    def test_bands_supersede_the_legacy_cut(self, monkeypatch):
        """With bands set, the old cut must be inert -- otherwise an upgraded
        deployment would boost the dead zone again."""
        monkeypatch.setattr(cfg.settings, "model_low_fair_cut", 0.70)
        monkeypatch.setattr(cfg.settings, "model_low_fair_stake_mult", 2.0)
        assert Bot.__new__(Bot)._stake_for(_sig(0.65)) == pytest.approx(1.0), (
            "the legacy cut must not fire while bands are configured"
        )

    def test_the_legacy_cut_still_works_when_no_bands_are_set(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_stake_boost_bands", "")
        monkeypatch.setattr(cfg.settings, "model_stake_boost_mult", 1.0)
        monkeypatch.setattr(cfg.settings, "model_low_fair_cut", 0.70)
        monkeypatch.setattr(cfg.settings, "model_low_fair_stake_mult", 2.0)
        assert Bot.__new__(Bot)._stake_for(_sig(0.65)) == pytest.approx(2.0)


def test_defaults_are_off():
    from btcpredict.config import Settings
    assert Settings.model_fields["model_stake_boost_bands"].default == ""
    assert Settings.model_fields["model_stake_boost_mult"].default == 1.0
