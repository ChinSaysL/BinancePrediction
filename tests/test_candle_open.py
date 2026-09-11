"""Tests for the candle-open execution gate."""
from btcpredict.main import sec_into_candle


def test_sec_into_candle_boundaries():
    # 60_000 ms = one minute exactly.
    assert sec_into_candle(60_000) == 0
    assert sec_into_candle(60_001) == 0
    assert sec_into_candle(60_999) == 0
    assert sec_into_candle(61_000) == 1
    assert sec_into_candle(119_999) == 59
    assert sec_into_candle(120_000) == 0


def test_sec_into_candle_rolls_across_hours():
    # Any multiple of 60_000 ms is a candle open, at any offset into the day.
    t = 1_234_567_890_000
    assert t % 60_000 == 0 or sec_into_candle(t) == (t // 1000) % 60
    assert sec_into_candle(1_234_567_830_000) == 30


def test_gate_window_setting_shape():
    from btcpredict.config import settings
    # The winning variant runs UNGATED (price-catcher): trades at any second,
    # gated by a big edge floor instead of the candle open.
    assert settings.trade_on_candle_open is False
    assert settings.candle_open_window_s > 0
    # Persistence must fit inside the open window when the gate is re-enabled.
    assert settings.signal_persistence * settings.poll_interval_s <= settings.candle_open_window_s
    # Candle indexes are minutes remaining at the open: 1..4.
    assert settings.trade_candle_opens
    assert set(settings.trade_candle_opens) <= {1, 2, 3, 4}
    # The edge floor and the quote floor are TUNED VALUES, not invariants, and
    # asserting one era's numbers made this suite fail on whichever machine had
    # a different .env -- the VPS ran 0.01/0.50 against a test that demanded
    # 0.05-0.15 / 0.20-0.30. Assert the shape instead; the history lives in
    # FINDINGS.md. (Successive eras: 0.12 with min_quote 0.25, then the honest
    # pricing at 0.01 with min_quote 0.50, haircut 1.0.)
    assert 0.0 < settings.min_edge < 1.0
    assert 0.0 <= settings.min_quote < settings.max_quote <= 1.0
    assert 0.0 < settings.max_tau_s <= 300.0
    assert settings.stake_fraction > 0.0 or settings.sizing_mode != "fraction"
