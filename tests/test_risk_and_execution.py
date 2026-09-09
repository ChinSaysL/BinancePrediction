import asyncio

import pytest

from btcpredict.calibration.report import PnLSummary, score
from btcpredict.execution.base import Broker
from btcpredict.execution.live import LiveBroker, LiveBrokerError, from_wei, to_wei
from btcpredict.execution.paper import PaperBroker
from btcpredict.risk.guards import RiskGuard


def guard(**kw):
    return RiskGuard(**{"daily_loss_cap": 3.0, "max_consecutive_losses": 3,
                        "max_open_rounds": 1, **kw})


def test_one_position_per_round():
    g = guard()
    assert g.check("r1")[0]
    g.on_open("r1")
    assert not g.check("r1")[0]


def test_max_open_rounds():
    g = guard()
    g.on_open("r1")
    assert not g.check("r2")[0]


def test_consecutive_loss_halt():
    g = guard(daily_loss_cap=999.0)
    for i in range(3):
        g.on_open(f"r{i}")
        g.on_settle(f"r{i}", -1.0)
    allowed, why = g.check("next")
    assert not allowed and "consecutive" in why


def test_a_win_resets_the_streak():
    g = guard(daily_loss_cap=999.0)
    for i in range(2):
        g.on_open(f"r{i}"); g.on_settle(f"r{i}", -1.0)
    g.on_open("w"); g.on_settle("w", +2.0)
    assert g.state.consecutive_losses == 0
    assert g.check("next")[0]


def test_daily_loss_cap_halt():
    g = guard(max_consecutive_losses=99)
    g.on_open("r"); g.on_settle("r", -3.5)
    assert not g.check("next")[0]


def test_seed_daily_persists_halt_across_restart():
    # A fresh process must not forget that today's cap is already breached.
    g = guard()
    g.seed_daily(-3.5, 0)
    assert not g.check("next")[0]
    assert "daily loss cap" in g.state.halt_reason


def test_seed_daily_below_cap_keeps_trading():
    g = guard()
    g.seed_daily(-2.0, 1)
    assert g.check("next")[0]


def test_resume_actually_lets_trading_continue():
    # Regression: clearing `halted` alone left check() re-deriving the halt from
    # daily PnL, so resume reported success and then blocked every trade.
    g = guard()
    for i in range(3):
        g.on_open(f"r{i}"); g.on_settle(f"r{i}", -1.02)
    assert not g.check("next")[0]
    g.resume()
    assert g.check("next")[0]


def test_paper_broker_win_and_loss_arithmetic():
    async def go():
        b = PaperBroker(starting_balance=10.0, fee_rate=0.02, half_spread=0.02)
        f = await b.buy(slug="s", side="Up", token_id="t", price=0.60, stake=1.0)
        assert f.filled_px == pytest.approx(0.62)        # pays the half-spread
        assert f.shares == pytest.approx(1.0 / 0.62)
        assert f.fee_paid == pytest.approx(0.02)
        win = await b.settle(f, "Up")
        assert win.pnl == pytest.approx(1.0 / 0.62 - 1.02)
        f2 = await b.buy(slug="s2", side="Up", token_id="t", price=0.60, stake=1.0)
        loss = await b.settle(f2, "Down")
        assert loss.pnl == pytest.approx(-1.02)          # stake plus fee
    asyncio.run(go())


def test_paper_broker_respects_balance():
    async def go():
        b = PaperBroker(starting_balance=0.5)
        assert await b.buy(slug="s", side="Up", token_id="t", price=0.6, stake=1.0) is None
    asyncio.run(go())


def test_brokers_satisfy_the_protocol():
    assert isinstance(PaperBroker(), Broker)
    assert isinstance(LiveBroker(None, "k", "s"), Broker)


def test_live_broker_refuses_without_a_wallet():
    b = LiveBroker(None, "key", "secret")
    assert not b.enabled                      # no wallet discovered yet
    with pytest.raises(LiveBrokerError):
        asyncio.run(b.buy(slug="s", side="Up", token_id="t", price=0.5, stake=1.0))


def test_live_broker_enabled_once_wallet_is_known():
    b = LiveBroker(None, "key", "secret", wallet_address="0xabc", wallet_id="w1")
    assert b.enabled


def test_amount_is_encoded_as_18_decimal_wei():
    # Sending a plain "1" is rejected by the venue as "order amount too small",
    # which is a units error wearing a misleading message.
    assert to_wei(1.0) == "1000000000000000000"
    assert to_wei(0.5) == "500000000000000000"
    assert from_wei("1342702702702702647") == pytest.approx(1.3427027027, abs=1e-9)
    assert from_wei(None) == 0.0


def test_live_signature_is_deterministic_and_sorted():
    b = LiveBroker(None, "key", "secret")
    qs = b._sign({"symbol": "BTCUSDT", "alpha": "1"})
    assert qs.startswith("alpha=1&")                     # alphabetical ordering
    assert len(qs.split("signature=")[1]) == 64          # hex sha256


def test_pnl_summary_drawdown():
    s = PnLSummary()
    for pnl in (+2.0, -1.0, -1.0, +3.0):
        s.add(pnl, 1.0, 0.02, pnl > 0)
    assert s.n_trades == 4 and s.wins == 2
    assert s.pnl == pytest.approx(3.0)
    assert s.max_drawdown == pytest.approx(-2.0)


def test_scoring_rewards_calibration_over_confidence():
    # Same accuracy, different calibration: the overconfident model must score worse.
    preds = [0.6] * 50 + [0.4] * 50
    outs = [1] * 30 + [0] * 20 + [1] * 20 + [0] * 30
    calibrated = score(preds, outs)
    overconf = score([0.95 if p > 0.5 else 0.05 for p in preds], outs)
    assert overconf.accuracy == calibrated.accuracy
    assert overconf.brier > calibrated.brier
    assert overconf.reliability > calibrated.reliability


def test_live_order_uses_the_verified_parameter_values():
    """Regression lock on hard-won venue behaviour.

    FOK is the only accepted timeInForce (IOC/GTC -> -1102) and accountType must
    be SPOT (CeDeFi -> -3026, even though payment-options advertises it). These
    were found by probing a live endpoint; a "tidying" change that switches them
    back to the documented-looking values would silently stop all trading.
    """
    import inspect
    from btcpredict.execution import live
    src = inspect.getsource(live.LiveBroker.buy)
    assert '"timeInForce": "FOK"' in src
    assert live.LiveBroker(None, "k", "s").account_type == "SPOT"
