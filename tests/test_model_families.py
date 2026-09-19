"""`model_families` narrows the MODEL arm without narrowing the tape.

Measured 2026-09-19 with every decision knob pinned to the live host, the
six-family widening was trading two families that lose:

    btc-updown-5m   1,467  WR 72.5%  EV/$ +0.0300  day-t +1.55  +$44.01
    eth-updown-5m     528  WR 69.5%  EV/$ -0.0038  day-t +0.05   -$2.01
    bnb-updown-5m     142  WR 73.2%  EV/$ -0.0093  day-t +0.36   -$1.32

ETH fails its own calibration independently: std(z) = 1.525 over 354,939
observations against BTC 0.999 and BNB 1.017.
"""
import pytest

import btcpredict.config as cfg
from btcpredict.config import model_families, model_traded


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(cfg.settings, "model_families", "")
    yield


class TestTheKnob:
    def test_empty_means_every_family(self, monkeypatch):
        """The widened behaviour must be the default: narrowing is explicit."""
        assert model_families() == frozenset()
        for f in ("btc-updown-5m", "eth-updown-5m", "bnb-updown-15m", "anything"):
            assert model_traded(f) is True, f

    def test_it_narrows_to_exactly_the_named_families(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_families", "btc-updown-5m")
        assert model_traded("btc-updown-5m") is True
        assert model_traded("eth-updown-5m") is False
        assert model_traded("bnb-updown-5m") is False
        assert model_traded("btc-updown-15m") is False, "15m is a DIFFERENT family"

    def test_whitespace_and_trailing_commas_are_tolerated(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_families",
                            " btc-updown-5m , eth-updown-5m ,")
        assert model_families() == {"btc-updown-5m", "eth-updown-5m"}
        assert model_traded("bnb-updown-5m") is False

    def test_an_unknown_prefix_is_refused_not_crashed(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_families", "btc-updown-5m")
        assert model_traded(None) is False
        assert model_traded("") is False


class TestItNarrowsTradingOnly:
    def test_the_gate_sits_after_the_history_tick(self):
        """A switched-off family must keep its price history warm, or
        re-enabling it starts from a cold estimator -- the same reason
        entry_mode "off" returns below `hist.add_tick`."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "btcpredict" / "main.py").read_text()
        tick = src.index("hist.add_tick(spot)")
        gate = src.index("if not model_traded(market_prefix(r.slug)):")
        assert gate > tick, (
            "the model_families gate must come AFTER hist.add_tick, or a "
            "disabled family goes cold and cannot be re-enabled cleanly"
        )

    def test_it_is_not_the_cross_switch(self, monkeypatch):
        """`model_families` and `cross_disabled_families` are separate.

        Collapsing them is how narrowing one arm silently narrows the other --
        the same separation MARKETS and CROSS_TRADED keep.
        """
        from btcpredict.config import cross_traded
        monkeypatch.setattr(cfg.settings, "model_families", "btc-updown-5m")
        monkeypatch.setattr(cfg.settings, "cross_disabled_families", "")
        assert model_traded("eth-updown-5m") is False
        assert cross_traded("eth-updown-5m") is True, \
            "narrowing the model arm must not touch the cross arm"
