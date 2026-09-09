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
    # Replay-tested edge floor for the price-catcher: 0.08 is the winner.
    assert 0.05 < settings.min_edge < 0.12
    # Replay-tested quote floor: cheap longshots carry the highest observed
    # fees (180 bps below px 0.50) and the <0.25 band lost money on the recording.
    assert 0.20 <= settings.min_quote <= 0.30
