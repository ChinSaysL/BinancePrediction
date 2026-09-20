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


def _freeze(monkeypatch, wday, hour=12):
    """Pin time.gmtime to a weekday index 0=Mon .. 6=Sun, at `hour` UTC."""
    base = time.struct_time((2026, 9, 14 + wday, hour, 0, 0, wday, 257, 0))
    monkeypatch.setattr(m.time, "gmtime", lambda *a: base)


def _window(monkeypatch, sd=4, sh=22, ed=6, eh=21):
    for k, v in (("weekend_start_dow", sd), ("weekend_start_hour_utc", sh),
                 ("weekend_end_dow", ed), ("weekend_end_hour_utc", eh)):
        monkeypatch.setattr(m.settings, k, v, raising=False)


def _cfg(monkeypatch, **kw):
    s = m.settings
    for k, v in kw.items():
        monkeypatch.setattr(s, k, v, raising=False)


def test_the_window_is_the_FX_week_not_midnight(monkeypatch):
    """Fri 22:00 UTC -> Sun 21:00 UTC, measured. See config.weekend_start_dow."""
    _window(monkeypatch)
    cases = [
        (4, 20, False, "Fri 20:00 still weekday (median 9.4bps, 75% in band)"),
        (4, 21, False, "Fri 21:00 still weekday"),
        (4, 22, True,  "Fri 22:00 NY close -> weekend"),
        (5, 3,  True,  "Sat is weekend"),
        (6, 20, True,  "Sun 20:00 still dead (1.6bps, 0% in band)"),
        (6, 21, False, "Sun 21:00 Sydney open -> weekday"),
        (6, 23, False, "Sun 23:00 weekday-like (10.7bps, 75% in band)"),
        (0, 12, False, "Mon is weekday"),
    ]
    for wd, h, want, why in cases:
        _freeze(monkeypatch, wd, h)
        assert m.is_weekend_utc() is want, why


def test_midnight_boundary_is_NOT_used(monkeypatch):
    """The old rule called Sat/Sun weekend wholesale; Sun 22:00 must not be."""
    _window(monkeypatch)
    _freeze(monkeypatch, 6, 22)
    assert m.is_weekend_utc() is False


def test_a_wrapping_window_is_not_inverted(monkeypatch):
    """start > end must wrap the week end rather than select the complement."""
    _window(monkeypatch, sd=6, sh=22, ed=1, eh=3)   # Sun 22:00 -> Tue 03:00
    for wd, h, want in ((6, 23, True), (0, 12, True), (1, 2, True),
                        (1, 4, False), (4, 12, False)):
        _freeze(monkeypatch, wd, h)
        assert m.is_weekend_utc() is want


def test_empty_window_is_off(monkeypatch):
    _window(monkeypatch, sd=5, sh=0, ed=5, eh=0)
    _freeze(monkeypatch, 5, 12)
    assert m.is_weekend_utc() is False


def test_off_by_default_keeps_weekday_knobs(monkeypatch, bot):
    _window(monkeypatch)
    _freeze(monkeypatch, 6, 3)           # Sunday 03:00, inside the window
    _cfg(monkeypatch, weekend_overrides=False,
         min_edge=0.01, weekend_min_edge=0.06)
    assert bot._wk("min_edge") == 0.01


def test_weekday_ignores_overrides_even_when_on(monkeypatch, bot):
    _window(monkeypatch)
    _freeze(monkeypatch, 2, 12)          # Wednesday
    _cfg(monkeypatch, weekend_overrides=True,
         min_edge=0.01, weekend_min_edge=0.06)
    assert bot._wk("min_edge") == 0.01


def test_weekend_swaps_every_knob(monkeypatch, bot):
    _window(monkeypatch)
    _freeze(monkeypatch, 5, 12)          # Saturday
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
    _window(monkeypatch)
    _freeze(monkeypatch, 6, 3)
    _cfg(monkeypatch, weekend_overrides=True, edge_haircut=1.0)
    monkeypatch.delattr(m.settings, "weekend_edge_haircut", raising=False)
    assert bot._wk("edge_haircut") == 1.0


def test_sigma_mult_is_a_multiplier_not_a_replacement(monkeypatch, bot):
    _window(monkeypatch)
    _freeze(monkeypatch, 5, 12)
    _cfg(monkeypatch, weekend_overrides=True, weekend_sigma_mult=1.5)
    sigma = 0.0004
    assert sigma * bot._wk("sigma_mult") == pytest.approx(0.0006)
