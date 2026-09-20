"""`arm_overlap.load` must keep end_price -- rolling_sigma depends on it.

The column was SELECTed and then dropped from the dict, so `rolling_sigma`
silently returned its 0.0004 fallback for every round: a CONSTANT volatility in
every replay, against a live median near 0.00028. fair_value is a function of
sigma, so this changed which trades fire in every backtest this repo produced.
"""
import sqlite3
import sys

import pytest

sys.path.insert(0, "tools")
import arm_overlap as ao  # noqa: E402


def _db(end_prices):
    con = sqlite3.connect(":memory:")
    con.execute("""create table rounds (slug text, start_ms int, end_ms int,
                   strike real, winner text, end_price real, symbol text)""")
    con.execute("""create table quotes (slug text, ts_ms int, tau_s real,
                   spot real, up_price real, down_price real,
                   up_ask real, down_ask real)""")
    for i, ep in enumerate(end_prices):
        slug = f"btc-updown-5m-{i}"
        con.execute("insert into rounds values (?,?,?,?,?,?,?)",
                    (slug, 1_000_000 + i*300_000, 1_000_000 + (i+1)*300_000,
                     100_000.0, "Up", ep, "BTCUSDT"))
        for j in range(5):
            con.execute("insert into quotes values (?,?,?,?,?,?,?,?)",
                        (slug, 1_000_000 + i*300_000 + j*1000, 300.0 - j,
                         100_000.0 + j, 0.5, 0.5, 0.55, 0.47))
    con.commit()
    return con


def test_end_price_survives_load():
    rs = ao.load(_db([100_050.0, 99_980.0, 100_120.0]))
    assert len(rs) == 3
    assert [r["end_price"] for r in rs] == [100_050.0, 99_980.0, 100_120.0], \
        "end_price must be carried into the round dict"


def test_rolling_sigma_is_not_pinned_to_the_fallback():
    """With enough varied rounds, sigma must MOVE, not sit at 0.0004."""
    import random
    rng = random.Random(0)
    eps = [100_000.0 * (1 + rng.gauss(0, 0.002)) for _ in range(120)]
    rs = ao.load(_db(eps))
    sig = [ao.rolling_sigma(rs, i) for i in range(len(rs))]
    late = sig[40:]
    assert len({round(x, 9) for x in late}) > 5, \
        "rolling_sigma is constant -- end_price is not reaching it"
    assert not all(abs(x - 0.0004) < 1e-12 for x in late), \
        "every sigma is the 0.0004 fallback"


def test_fallback_still_applies_when_end_price_is_genuinely_missing():
    rs = ao.load(_db([None] * 40))
    assert ao.rolling_sigma(rs, 30) == pytest.approx(0.0004)
