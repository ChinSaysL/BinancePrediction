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


def test_daily_cap_stays_absolute_when_pct_is_off():
    # no bankroll wired up, and pct 0 -> the old absolute behaviour, unchanged
    assert guard().effective_daily_cap() == pytest.approx(3.0)
    assert guard(bankroll=lambda: 40.0).effective_daily_cap() == pytest.approx(3.0)


def test_percentage_daily_cap_binds_before_the_absolute_one():
    # 5% of $40 is $2.00, i.e. tighter than the $3.00 absolute cap
    g = guard(daily_loss_cap_pct=0.05, bankroll=lambda: 40.0)
    assert g.effective_daily_cap() == pytest.approx(2.0)
    g.on_settle("r1", -2.0)
    assert g.state.halted
    assert "2.00" in g.state.halt_reason


def test_percentage_daily_cap_never_exceeds_the_absolute_cap():
    # 5% of $1000 is $50, but the absolute cap still governs
    g = guard(daily_loss_cap_pct=0.05, bankroll=lambda: 1000.0)
    assert g.effective_daily_cap() == pytest.approx(3.0)


def test_percentage_daily_cap_follows_the_bankroll_down():
    """A percentage cap must tighten as the account shrinks -- that is the
    behaviour fixed dollars cannot provide."""
    b = {"v": 100.0}
    g = guard(daily_loss_cap=100.0, daily_loss_cap_pct=0.10,
              bankroll=lambda: b["v"])
    assert g.effective_daily_cap() == pytest.approx(10.0)
    b["v"] = 50.0
    assert g.effective_daily_cap() == pytest.approx(5.0)


def test_percentage_daily_cap_ignores_a_missing_or_broken_bankroll():
    assert guard(daily_loss_cap_pct=0.05).effective_daily_cap() == pytest.approx(3.0)
    assert guard(daily_loss_cap_pct=0.05,
                 bankroll=lambda: 0.0).effective_daily_cap() == pytest.approx(3.0)


def test_daily_cap_can_be_switched_off_entirely():
    """0 means off for BOTH knobs. A disabled dollar cap must never be read as
    a zero-dollar budget, which would halt on the first loser."""
    g = guard(daily_loss_cap=0.0)
    assert g.effective_daily_cap() == float("inf")
    g.on_settle("r1", -5.0)
    assert not g.state.halted
    assert g.check("r2")[0]


def test_percentage_daily_cap_keeps_its_meaning_as_the_account_grows():
    """The point of the switch: one rule that means the same at every size."""
    b = {"v": 76.0}
    g = guard(daily_loss_cap=0.0, daily_loss_cap_pct=0.45,
              bankroll=lambda: b["v"])
    assert g.effective_daily_cap() == pytest.approx(34.2)    # ~ the old $35
    b["v"] = 1000.0
    assert g.effective_daily_cap() == pytest.approx(450.0)   # same 45%


def test_percentage_daily_cap_is_honoured_when_seeding_after_a_restart():
    g = guard(daily_loss_cap_pct=0.05, bankroll=lambda: 40.0)   # cap $2.00
    g.seed_daily(-2.50)
    assert g.state.halted
    assert "2.00" in g.state.halt_reason


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


def test_a_new_utc_day_clears_a_streak_halt():
    """Regression: the streak halt used to be permanent. roll_day_if_needed
    only cleared a halt whose reason started with "daily loss", and never reset
    `consecutive_losses`, so check() kept re-deriving the halt from a counter
    that could not fall -- 5 losses stopped the bot until a manual --resume,
    even days later."""
    g = guard(daily_loss_cap=999.0)          # isolate the streak rule
    for i in range(3):                       # guard() halts at 3
        g.on_open(f"r{i}"); g.on_settle(f"r{i}", -1.0)
    assert not g.check("next")[0]
    assert g.state.halt_kind == "streak"

    g.state._day = "1999-01-01"              # force the UTC day boundary
    assert g.check("next-day")[0]
    assert g.state.consecutive_losses == 0
    assert not g.state.halted and g.state.halt_kind == ""


def test_a_new_utc_day_still_clears_the_daily_loss_halt():
    g = guard(max_consecutive_losses=99)
    g.on_settle("r", -3.5)
    assert not g.check("next")[0]
    g.state._day = "1999-01-01"
    assert g.check("next-day")[0]


def test_a_new_utc_day_does_not_clear_an_unknown_halt():
    """Only the guard's own two halts expire. A halt raised from anywhere else
    (empty halt_kind) stays put until someone resumes it deliberately."""
    g = guard()
    g.state.halted = True
    g.state.halt_reason = "venue position mismatch"
    g.state._day = "1999-01-01"
    assert not g.check("next")[0]
    assert g.state.halted


def test_seed_daily_restores_a_live_streak_halt():
    """A restart mid-streak must not launder it -- and must still expire it
    with the day."""
    g = guard()                              # cap 3.0, streak limit 3
    g.seed_daily(-1.0, 3)                    # today's losses, cap not breached
    assert not g.check("next")[0]
    assert g.state.halt_kind == "streak"
    g.state._day = "1999-01-01"
    assert g.check("next-day")[0]


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


# -- order retries ---------------------------------------------------------
class _StubBroker:
    """Live-shaped broker whose buy() replays a scripted list of outcomes."""

    mode = "live"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.last_failure = None
        self.buys = 0
        self.quotes = 0

    async def get_quote(self, token_id, stake, side="BUY"):
        self.quotes += 1
        return {"quoteId": f"q{self.quotes}", "averagePrice": 0.57,
                "feeRateBps": 60, "expireAt": 0}

    async def buy(self, *, slug, side, token_id, price, stake, quote=None):
        self.buys += 1
        outcome = self.outcomes.pop(0)
        if outcome == "fill":
            self.last_failure = None
            from btcpredict.execution.base import Fill
            return Fill(slug=slug, side=side, requested_px=price, filled_px=0.57,
                        shares=stake / 0.57, stake=stake, fee_paid=0.06,
                        order_id=f"o{self.buys}", ts_ms=0)
        self.last_failure = outcome
        return None


class _Sig:
    """The signal from the live log: Down at 0.570 with the model at P(up)=0.28."""

    act, side, quote, fair, edge = True, "Down", 0.57, 0.72, 0.10


class _Round:
    slug = "btc-updown-5m-1788968400"


def _bot(broker):
    from btcpredict.main import Bot
    bot = Bot.__new__(Bot)
    bot.broker = broker
    bot._order_cooldown_until = 0.0
    return bot


def _place(broker):
    bot = _bot(broker)
    fill = asyncio.run(bot._place_order(_Round(), _Sig(), fair_up=0.28,
                                        token="tok", stake=10.0))
    return bot, fill


def test_a_venue_rejection_is_retried_until_it_fills(monkeypatch):
    """A FOK failure is the book moving mid-flight, not a reason to skip the round.

    This is the bug from the live log: order FAILED on the venue and the bot
    went straight to a 30s pause instead of re-quoting.
    """
    from btcpredict.config import settings
    monkeypatch.setattr(settings, "order_retry_delay_s", 0.0)
    monkeypatch.setattr(settings, "order_max_attempts", 3)
    # These exercise the retry path, not the edge gate: keep the stub's signal
    # qualifying regardless of what the live haircut is set to.
    monkeypatch.setattr(settings, "edge_haircut", 1.0)

    b = _StubBroker(["venue_failed", "fill"])
    bot, fill = _place(b)

    assert fill is not None
    assert b.buys == 2
    # A fresh quote per attempt: the first quoteId is spent and cannot be re-sent.
    assert b.quotes == 2
    assert bot._order_cooldown_until == 0.0      # it filled; no backoff


def test_retries_are_bounded_and_then_cool_down(monkeypatch):
    from btcpredict.config import settings
    monkeypatch.setattr(settings, "order_retry_delay_s", 0.0)
    monkeypatch.setattr(settings, "order_max_attempts", 3)
    # These exercise the retry path, not the edge gate: keep the stub's signal
    # qualifying regardless of what the live haircut is set to.
    monkeypatch.setattr(settings, "edge_haircut", 1.0)

    b = _StubBroker(["venue_failed"] * 3)
    bot, fill = _place(b)

    assert fill is None
    assert b.buys == 3
    assert bot._order_cooldown_until > 0.0


def test_an_unknown_outcome_is_never_retried(monkeypatch):
    """The order may be live on the venue; a second one would double the stake."""
    from btcpredict.config import settings
    monkeypatch.setattr(settings, "order_retry_delay_s", 0.0)
    monkeypatch.setattr(settings, "order_max_attempts", 3)
    # These exercise the retry path, not the edge gate: keep the stub's signal
    # qualifying regardless of what the live haircut is set to.
    monkeypatch.setattr(settings, "edge_haircut", 1.0)

    b = _StubBroker(["unknown", "fill"])
    bot, fill = _place(b)

    assert fill is None
    assert b.buys == 1
    assert bot._order_cooldown_until > 0.0


def test_live_broker_labels_a_terminal_venue_status_as_retryable():
    from btcpredict.execution import live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")

    async def quote(*a, **kw):
        return {"quoteId": "q1", "amountOut": to_wei(17.5),
                "feeAmount": to_wei(0.1), "averagePrice": 0.57, "expireAt": 0}

    async def request(method, path, params):
        return {"orderId": "26090900001873335541"}

    async def status(order_id, **kw):
        return "FAILED"

    b.get_quote = quote
    b._request = request
    b._order_status = status
    fill = asyncio.run(b.buy(slug="s", side="Down", token_id="t",
                             price=0.57, stake=10.0))
    assert fill is None
    assert b.last_failure == "venue_failed"
    assert b.last_failure in live.RETRYABLE_FAILURES


# --- unverified fills: a void is only allowed on positive evidence -----------

def _unverified_broker(*, status, held):
    """LiveBroker with the two verification calls stubbed out."""
    from btcpredict.execution import live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")

    async def _status(order_id, **kw):
        return status

    async def _positions():
        return held                       # None models "could not ask"

    b._order_status = _status
    b._position_token_ids = _positions
    return b


def _unverified_fill():
    from btcpredict.execution.base import Fill

    return Fill(slug="r1", side="Up", requested_px=0.5, filled_px=0.5,
                shares=20.0, stake=10.0, fee_paid=0.1, order_id="o1", ts_ms=1,
                token_id="tok", unverified=True)


def test_unverified_fill_voids_only_on_confirmed_absence():
    b = _unverified_broker(status=None, held=set())     # readable, token absent
    f = _unverified_fill()
    f.unverified_checks = 19                            # one check short
    s = asyncio.run(b.settle(f, "Down"))
    assert s is not None and s.void and s.pnl == 0.0


def test_unverified_fill_held_by_the_venue_settles_for_real():
    b = _unverified_broker(status=None, held={"tok"})
    s = asyncio.run(b.settle(_unverified_fill(), "Up"))
    assert s is not None and not s.void and s.won
    assert s.pnl == pytest.approx(10.0)


def test_unverified_fill_never_voids_while_the_venue_is_unreadable():
    # An unreadable position list is NOT evidence of absence: voiding here
    # would silently drop a real position whose winnings are never redeemed.
    b = _unverified_broker(status=None, held=None)
    f = _unverified_fill()
    f.unverified_checks = 100
    for _ in range(3):
        assert asyncio.run(b.settle(f, "Down")) is None
    assert f.unverified_checks > 100


def test_unverified_fill_with_terminal_status_is_voided():
    b = _unverified_broker(status="FAILED", held=None)
    s = asyncio.run(b.settle(_unverified_fill(), "Up"))
    assert s is not None and s.void


def test_verified_fill_skips_verification_entirely():
    b = _unverified_broker(status="FAILED", held=set())
    f = _unverified_fill()
    f.unverified = False                                # status was confirmed
    s = asyncio.run(b.settle(f, "Up"))
    assert s is not None and not s.void and s.won
