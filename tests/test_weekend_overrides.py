"""Weekend overrides: the knobs must swap on Sat/Sun UTC and ONLY then.

See config.weekend_overrides for why this ships despite p = 0.42 -- these tests
guard the mechanism, not the hypothesis.
"""
import time
import types

import pytest

from btcpredict import main as m


class _Bot:
    """Minimal stand-in: _wk only touches `settings` and the clock."""
    _wk = m.Bot._wk


@pytest.fixture
def bot():
    return _Bot()


def _freeze(monkeypatch, wday):
    """Pin time.gmtime to a weekday index 0=Mon .. 6=Sun."""
    base = time.struct_time((2026, 9, 14 + wday, 12, 0, 0, wday, 257, 0))
    monkeypatch.setattr(m.time, "gmtime", lambda *a: base)


def _cfg(monkeypatch, **kw):
    s = m.settings
    for k, v in kw.items():
        monkeypatch.setattr(s, k, v, raising=False)


def test_is_weekend_utc(monkeypatch):
    for wday, want in ((0, False), (4, False), (5, True), (6, True)):
        _freeze(monkeypatch, wday)
        assert m.is_weekend_utc() is want


def test_off_by_default_keeps_weekday_knobs(monkeypatch, bot):
    _freeze(monkeypatch, 6)                       # Sunday
    _cfg(monkeypatch, weekend_overrides=False,
         min_edge=0.01, weekend_min_edge=0.06)
    assert bot._wk("min_edge") == 0.01


def test_weekday_ignores_overrides_even_when_on(monkeypatch, bot):
    _freeze(monkeypatch, 2)                       # Wednesday
    _cfg(monkeypatch, weekend_overrides=True,
         min_edge=0.01, weekend_min_edge=0.06)
    assert bot._wk("min_edge") == 0.01


def test_weekend_swaps_every_knob(monkeypatch, bot):
    _freeze(monkeypatch, 5)                       # Saturday
    _cfg(monkeypatch, weekend_overrides=True,
         min_edge=0.01, weekend_min_edge=0.06,
         min_quote=0.55, weekend_min_quote=0.45,
         max_quote=0.80, weekend_max_quote=0.72,
         min_tau_s=60.0, weekend_min_tau_s=60.0,
         max_tau_s=180.0, weekend_max_tau_s=240.0,
         signal_persistence=2, weekend_signal_persistence=2)
    assert bot._wk("min_edge") == 0.06
    assert bot._wk("min_quote") == 0.45
    assert bot._wk("max_quote") == 0.72
    assert bot._wk("max_tau_s") == 240.0
    assert bot._wk("signal_persistence") == 2


def test_missing_weekend_knob_falls_back(monkeypatch, bot):
    """An override that does not exist must not blank the weekday value."""
    _freeze(monkeypatch, 6)
    _cfg(monkeypatch, weekend_overrides=True, edge_haircut=1.0)
    monkeypatch.delattr(m.settings, "weekend_edge_haircut", raising=False)
    assert bot._wk("edge_haircut") == 1.0


def test_sigma_mult_is_a_multiplier_not_a_replacement(monkeypatch, bot):
    _freeze(monkeypatch, 5)
    _cfg(monkeypatch, weekend_overrides=True, weekend_sigma_mult=1.5)
    sigma = 0.0004
    assert sigma * bot._wk("sigma_mult") == pytest.approx(0.0006)
