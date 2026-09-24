import asyncio

import pytest

from btcpredict.calibration.report import PnLSummary, score
from btcpredict.execution.base import Broker
from btcpredict.execution.live import LiveBroker, LiveBrokerError, from_wei, to_wei
from btcpredict.execution.paper import PaperBroker
from btcpredict.risk.guards import RiskGuard


@pytest.fixture(autouse=True)
def _market_orders(monkeypatch):
    """Pin the order path this file exercises.

    main._place_order branches on `settings.order_type`, which the LIVE .env
    sets -- so without this the same code passes on a laptop and fails on the
    box (observed 2026-09-11: the fourth .env-dependent test failure this
    session). These tests are about the market-FOK path, so they say so
    instead of inheriting whichever arm happens to be live.
    """
    monkeypatch.setattr("btcpredict.config.settings.order_type", "market")


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


def test_a_streak_limit_of_zero_switches_the_streak_rule_off():
    """0 = off, like the caps. This needs its own code path in all three
    places the comparison appears (check, on_settle, seed_daily): a bare
    `consecutive_losses >= max_consecutive_losses` is `0 >= 0` on the first
    call, so a disabled rule would halt the bot on its first settled trade."""
    g = guard(daily_loss_cap=0.0, max_consecutive_losses=0)
    for i in range(10):
        g.on_open(f"r{i}")
        g.on_settle(f"r{i}", -1.0)
    allowed, why = g.check("next")
    assert allowed, why
    assert not g.state.halted
    # the counter still counts -- it is just not a reason to stop
    assert g.state.consecutive_losses == 10
    # ...and a restart must not invent a halt from yesterday's run either
    g2 = guard(daily_loss_cap=0.0, max_consecutive_losses=0)
    g2.seed_daily(-240.0, 12)
    assert not g2.state.halted
    assert g2.check("r")[0]


def test_with_the_shipped_guards_off_nothing_halts_on_pnl():
    """Both PnL rules off. The open-round limits still apply: those are
    position rules, not tolerance for losses."""
    g = guard(daily_loss_cap=0.0, daily_loss_cap_pct=0.0,
              max_consecutive_losses=0)
    for i in range(6):
        g.on_open(f"r{i}")
        g.on_settle(f"r{i}", -40.0)          # -$240, far past any daily cap
    assert g.effective_daily_cap() == float("inf")
    assert g.check("next")[0]
    g.on_open("held")
    assert g.check("held") == (False, "already positioned in this round")


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
        self.limits: list[tuple] = []

    async def buy_limit(self, *, slug, side, token_id, limit_px, stake, wait_s,
                        poll_s=0.75, fill_break=0.999):
        # Mirrors LiveBroker.buy_limit's signature. `fill_break` is the fraction
        # at which the wait loop calls the order done and cancels the remainder;
        # a stub that does not accept it hides a real call-site mismatch.
        self.limits.append((limit_px, wait_s))
        self.fill_breaks = getattr(self, "fill_breaks", [])
        self.fill_breaks.append(fill_break)
        from btcpredict.execution.base import Fill
        return Fill(slug=slug, side=side, requested_px=limit_px,
                    filled_px=limit_px, shares=stake / limit_px, stake=stake,
                    fee_paid=0.0, order_id="lim1", ts_ms=0, raw="{}")

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

    async def positions(self):
        # Empty, and READABLE: the retry loop refuses to re-place unless it can
        # confirm the venue holds no position for the round (an unreadable list
        # is indistinguishable from "we might already be long"). A live broker
        # without a position list is no longer a valid model of one.
        return []


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


def test_the_candle_open_gate_holds_by_default(monkeypatch):
    """The main arm only trades at the candle open, and that gate is deliberate."""
    monkeypatch.setattr("btcpredict.main.settings.trade_on_candle_open", True)
    monkeypatch.setattr("btcpredict.main.sec_into_candle", lambda ms: 285)

    b = _StubBroker(["fill"])
    bot, fill = _place(b)
    assert fill is None
    assert b.buys == 0


def test_a_taker_limit_crosses_at_our_own_valuation(monkeypatch):
    """order_type=limit_take must reach buy_limit (which matches the BOOK) and
    never buy() (which goes through trade/get-quote + place-order-bundle, priced
    off the venue's DISPLAYED price -- measured lagging its own book by up to
    0.40, with the quote above the ask in 5-7% of live samples).

    The ceiling is fair_side - limit_take_margin, and the DOWN side's fair is
    1 - fair_up, so passing P(Up) is what keeps this from pricing a Down order
    at the Up price.
    """
    monkeypatch.setattr("btcpredict.main.settings.order_type", "limit_take")
    monkeypatch.setattr("btcpredict.main.settings.limit_take_margin", 0.01)

    b = _StubBroker(["fill"])
    bot = _bot(b)
    # Signal is Down at 0.57; P(Up) = 0.10 -> the Down fair is 0.90, so the ceiling
    # is fair - margin = 0.89. But the ceiling ALONE is not enough: a live fill on
    # 2026-09-12 13:02 crossed a 0.90 limit against an ask of 0.7326 and landed at
    # 0.8919 (ev_at_fill -0.195), because our fair is the miscalibrated estimator
    # here. The limit is clamped to ask + one tick = 0.58, which still crosses the
    # book and still fills at the ask -- the only price the taker leg ever pays.
    fill = asyncio.run(bot._place_order(_Round(), _Sig(), fair_up=0.10,
                                        token="tok", stake=10.0))
    assert fill is not None
    assert b.buys == 0, "a taker limit must not use the market path"
    assert len(b.limits) == 1
    limit_px, wait = b.limits[0]
    assert limit_px == 0.58
    assert wait <= 20.0


def test_a_taker_limit_never_prices_an_arm_above_its_own_quote(monkeypatch):
    """An arm with no valuation of its own passes `take_px`, and that floors the
    limit -- the ticket's premise is that the venue's price is wrong, so its limit
    must come out at exactly the ask it read, never below it (which would never
    fill) and never above it (which would pay more than it decided to)."""
    monkeypatch.setattr("btcpredict.main.settings.order_type", "limit_take")
    monkeypatch.setattr("btcpredict.main.settings.limit_take_margin", 0.04)

    b = _StubBroker(["fill"])
    bot = _bot(b)
    # Down at 0.04, so P(Up) = 0.96 and the Down fair is 0.04.
    fill = asyncio.run(bot._place_order(_Round(), _Sig(), fair_up=0.96,
                                        token="tok", stake=1.0,
                                        skip_pretrade_gates=True,
                                        take_px=0.57))
    assert fill is not None
    assert len(b.limits) == 1
    limit_px, wait = b.limits[0]
    assert limit_px == 0.57, "the limit must be the arm's own price, not 1 - it"
    assert wait <= 20.0


def test_a_stale_high_quote_cannot_lift_the_limit_above_fair(monkeypatch):
    """The case the ceiling exists for: the venue repriced BEFORE us, so the
    displayed price is stale HIGH.

    With max(quote, fair - margin) -- what shipped first -- a display of 0.90
    would have beaten a fair minus margin of 0.86 and authorised a fill ABOVE our
    own valuation: a negative-EV entry, taken by the very arm the ceiling exists
    to stop. The limit is now the valuation and nothing else.
    """
    monkeypatch.setattr("btcpredict.main.settings.order_type", "limit_take")
    # Pinned, so this measures the ceiling and not whichever margin ships.
    monkeypatch.setattr("btcpredict.main.settings.limit_take_margin", 0.01)

    class StaleHigh:
        act, side, quote, fair, edge = True, "Down", 0.90, 0.87, 0.0

    b = _StubBroker(["fill"])
    bot = _bot(b)
    # P(Up) = 0.13 -> the Down fair is 0.87, so the ceiling is 0.86, under the
    # 0.90 the venue is displaying.
    fill = asyncio.run(bot._place_order(_Round(), StaleHigh(), fair_up=0.13,
                                        token="tok", stake=10.0))
    assert fill is not None
    limit_px, _ = b.limits[0]
    assert limit_px == 0.86
    assert limit_px < StaleHigh.quote, \
        "a stale-high display must not raise the price we are willing to pay"


def test_the_hurdle_and_the_take_ceiling_are_separate_knobs(monkeypatch):
    """`safety_margin` moves the signal's HURDLE; the take ceiling has its own
    setting. They used to be one number, so sweeping the ceiling -- which is what
    `--sweep-margin` does, 0.00 to 0.10 -- would also have moved the entry gate,
    an unmeasured change smuggled in with a measured one. The ceiling must follow
    `limit_take_margin` and ignore `safety_margin`.
    """
    monkeypatch.setattr("btcpredict.main.settings.order_type", "limit_take")

    class Sig:                     # same shape as the stale-high case below
        act, side, quote, fair, edge = True, "Down", 0.90, 0.87, 0.0

    # A 9% ceiling margin with the hurdle left at 1%: the ceiling is
    # 0.87 - 0.09 = 0.78, NOT 0.87 - 0.01.
    monkeypatch.setattr("btcpredict.main.settings.safety_margin", 0.01)
    monkeypatch.setattr("btcpredict.main.settings.limit_take_margin", 0.09)
    b = _StubBroker(["fill"])
    bot = _bot(b)
    asyncio.run(bot._place_order(_Round(), Sig(), fair_up=0.13, token="tok",
                                 stake=10.0))
    limit_px, _ = b.limits[0]
    assert limit_px == 0.78, "the ceiling must follow limit_take_margin"

    # ...and the reverse: a 9% HURDLE with a 1% ceiling must leave the ceiling
    # where it was. While the two were one setting this came out 0.78.
    monkeypatch.setattr("btcpredict.main.settings.safety_margin", 0.09)
    monkeypatch.setattr("btcpredict.main.settings.limit_take_margin", 0.01)
    b2 = _StubBroker(["fill"])
    bot2 = _bot(b2)
    asyncio.run(bot2._place_order(_Round(), Sig(), fair_up=0.13, token="tok",
                                  stake=10.0))
    limit_px2, _ = b2.limits[0]
    assert limit_px2 == 0.86, "raising safety_margin must NOT move the ceiling"


def test_a_limit_never_rests_past_the_end_of_the_round(monkeypatch):
    """A ticket fires 3-30s before the bell; the configured 20s wait would
    outlive the round, so the wait is capped by the time left."""
    from btcpredict.main import Bot

    monkeypatch.setattr("btcpredict.main.settings.order_type", "limit_take")
    monkeypatch.setattr("btcpredict.main.settings.limit_wait_s", 20.0)

    class ShortRound(_Round):
        def tau_s(self, now_ms):
            return 6.0

    b = _StubBroker(["fill"])
    bot = Bot.__new__(Bot)
    bot.broker = b
    bot._order_cooldown_until = 0.0
    fill = asyncio.run(bot._place_order(ShortRound(), _Sig(), fair_up=0.10,
                                        token="tok", stake=1.0))
    assert fill is not None
    _, wait = b.limits[0]
    assert wait <= 5.0, "the limit must not outlive the round it trades in"


def test_the_ticket_can_skip_the_candle_open_gate(monkeypatch):
    """A ticket fires at the END of a round -- 4-5 minutes into a 5m candle.

    With `trade_on_candle_open` on and no way to skip it, every live ticket
    would be withheld while still writing a `decisions` row, so the arm would
    look like it was trading and never place a single order. The skip must apply
    to those two decision gates only -- the retry loop's venue-position check and
    the booking path are untouched.
    """
    monkeypatch.setattr("btcpredict.main.settings.trade_on_candle_open", True)
    monkeypatch.setattr("btcpredict.main.sec_into_candle", lambda ms: 285)

    b = _StubBroker(["fill"])
    bot = _bot(b)
    fill = asyncio.run(bot._place_order(_Round(), _Sig(), fair_up=0.28,
                                        token="tok", stake=1.0,
                                        skip_pretrade_gates=True))
    assert fill is not None
    assert b.buys == 1


def test_a_down_order_is_rechecked_against_its_own_price(monkeypatch):
    """The pre-trade quote prices the TOKEN being bought, so it is THAT side's.

    The stub signal is Down at 0.57, and `_StubBroker.get_quote` returns 0.57
    for the token -- the Down price. The recheck used to pass `1.0 - 0.57` as
    the Down quote, scoring a 0.57 favourite as if it cost 0.43, so any Down
    order above 0.50 was refused by min_quote = 0.50. That is every Down
    favourite the honest pricing exists to buy: the live book ran 54 Up / 1 Down
    while the replay (no recheck) took 347 Down / 267 Up, and 62 distinct live
    rounds were refused with "real quote 0.670 -> quote 0.33 below min_quote".
    """
    from btcpredict.config import settings
    monkeypatch.setattr(settings, "order_retry_delay_s", 0.0)
    monkeypatch.setattr(settings, "order_max_attempts", 1)
    monkeypatch.setattr(settings, "edge_haircut", 1.0)
    monkeypatch.setattr(settings, "min_quote", 0.50)

    b = _StubBroker(["fill"])
    _bot_, fill = _place(b)
    assert fill is not None, "a 0.57 Down favourite must pass a 0.50 floor"
    assert b.buys == 1

    # ...and the floor still bites on the price actually paid, not on its
    # complement: at a 0.60 floor the same 0.57 order is refused.
    monkeypatch.setattr(settings, "min_quote", 0.60)
    b2 = _StubBroker(["fill"])
    _bot2, fill2 = _place(b2)
    assert fill2 is None
    assert b2.buys == 0


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
    """A FAILED status is retryable ONLY once the venue is confirmed empty.

    This test used to assert `venue_failed` on the strength of the status
    alone -- which is the bug: the order-history endpoint lags a fill, so a
    stale FAILED for an order that had filled sent the caller into the retry
    loop for a round it was already long. Retryability now requires positive
    evidence of absence from the position list.
    """
    from btcpredict.execution import live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")

    async def quote(*a, **kw):
        return {"quoteId": "q1", "amountOut": to_wei(17.5),
                "feeAmount": to_wei(0.1), "averagePrice": 0.57, "expireAt": 0}

    async def request(method, path, params):
        return {"orderId": "26090900001873335541"}

    async def status(order_id, **kw):
        return "FAILED"

    async def positions():
        return []                     # read, and it holds nothing

    b.get_quote = quote
    b._request = request
    b._order_status = status
    b._position_token_ids = positions
    fill = asyncio.run(b.buy(slug="s", side="Down", token_id="t",
                             price=0.57, stake=10.0))
    assert fill is None
    assert b.last_failure == "venue_failed"
    assert b.last_failure in live.RETRYABLE_FAILURES


def test_a_stale_failed_status_never_causes_a_second_buy():
    """Order history says FAILED, the position list says we are long.

    The list is the EFFECT of a fill and cannot be wrong in this direction, so
    the fill is booked and the failure is NOT retryable. Before the fix this
    returned venue_failed, the retry loop placed a second order, and the bot
    held twice the intended position in one round.
    """
    from btcpredict.execution import live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")

    async def quote(*a, **kw):
        return {"quoteId": "q1", "amountOut": to_wei(17.5),
                "feeAmount": to_wei(0.1), "averagePrice": 0.57, "expireAt": 0}

    async def request(method, path, params):
        return {"orderId": "26091100001877448929"}

    async def status(order_id, **kw):
        return "FAILED"

    async def positions():
        return {"t"}                   # the token-id set the venue reports

    b.get_quote = quote
    b._request = request
    b._order_status = status
    b._position_token_ids = positions
    fill = asyncio.run(b.buy(slug="s", side="Up", token_id="t",
                             price=0.57, stake=10.0))
    assert fill is not None, "a held position must be booked, not discarded"
    assert fill.token_id == "t"
    assert fill.unverified is False
    assert b.last_failure is None


def test_a_failed_status_with_unreadable_positions_is_not_retryable():
    """Neither source can be trusted, so the round is abandoned.

    A retry here is a coin flip between "nothing was placed" and "we are
    already long", and the losing side of that coin is a doubled position.
    """
    from btcpredict.execution import live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")

    async def quote(*a, **kw):
        return {"quoteId": "q1", "amountOut": to_wei(17.5),
                "feeAmount": to_wei(0.1), "averagePrice": 0.57, "expireAt": 0}

    async def request(method, path, params):
        return {"orderId": "26091100001877449048"}

    async def status(order_id, **kw):
        return "FAILED"

    async def positions():
        return None                   # could not ask

    b.get_quote = quote
    b._request = request
    b._order_status = status
    b._position_token_ids = positions
    fill = asyncio.run(b.buy(slug="s", side="Up", token_id="t",
                             price=0.57, stake=10.0))
    assert fill is None
    assert b.last_failure == "unconfirmed"
    assert b.last_failure not in live.RETRYABLE_FAILURES


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


# --- placement: an unreadable status must not become a booked fill ----------
#
# History lags place-order by a second or two, so "status unreadable" is the
# COMMON case, not the rare one: every one of the four orders on 2026-09-10
# 19:26-20:23 logged "proceeding unverified". Booking on assumption is how the
# trade record and the venue drift apart.

_UNSET = object()


def _buy_broker(*, status=None, held=None, positions_readable=True,
                order_record_resp=None, active_orders_resp=_UNSET):
    """LiveBroker whose place-order succeeds but whose status read fails.

    `active_orders_resp` is whatever order/list returns: `{"orders": []}` is
    READABLE AND EMPTY, `None` is UNREADABLE, and an order of ours inside it
    means the venue still holds it as working. Those three must not collapse.
    """
    from btcpredict.execution import live

    if active_orders_resp is _UNSET:
        active_orders_resp = {"orders": []}
    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")

    async def quote(*a, **kw):
        return {"quoteId": "q1", "amountOut": to_wei(17.5),
                "feeAmount": to_wei(0.1), "averagePrice": 0.57, "expireAt": 0}

    async def request(method, path, params=None):
        if path == live.ACTIVE_ORDERS:
            return active_orders_resp
        return {"orderId": "o1"}

    async def _status(order_id, **kw):
        return status

    async def _positions():
        return held

    b.cancel_calls = []

    async def _cancel(ids):
        # Real `cancel_orders` posts to the venue over http.client; a test must
        # not, and the cut-off path calls it before every retry decision.
        b.cancel_calls.append(list(ids))
        return []

    b.get_quote = quote
    b._request = request
    b._order_status = _status
    b._position_token_ids = _positions
    b.cancel_orders = _cancel
    if order_record_resp is not None:
        async def _record(order_id, **kw):
            return order_record_resp
        b.order_record = _record
    return b


def _buy(b, token="tok"):
    return asyncio.run(b.buy(slug="s", side="Up", token_id=token,
                             price=0.57, stake=10.0))


def test_no_verdict_but_the_POSITION_LIST_holds_the_token_books_it():
    """⛔ THE FILL SIDE OF THE CUT-OFF, and the reason a 2s budget does not turn
    fills into retries: order/history has not stamped FILLED yet, but the
    position exists -- which is the EFFECT of a fill and can only be there if
    the order went through. It must be BOOKED, never re-placed on top of."""
    b = _buy_broker(status=None, held={"tok"})
    fill = _buy(b)
    assert fill is not None, "a position that exists IS the fill"
    assert b.cancel_calls == [["o1"]]
    assert b.last_failure is None


@pytest.fixture(autouse=True)
def _fast_verdict_wait(monkeypatch):
    """The real ceiling is seconds; these stubs answer instantly or never."""
    from btcpredict.execution import live
    monkeypatch.setattr(live, "BUY_TERMINAL_WAIT_S", 0.3)
    monkeypatch.setattr(live, "BUY_TERMINAL_POLL_S", 0.02)


def test_no_verdict_with_BOTH_lists_clear_does_NOT_retry():
    """⛔ INVERTED 2026-09-24. This used to RETRY when order/list and the
    position list were both clear -- but a MARKET bundle never appears in
    order/list, so "clear" was always true. Live 04:52: declared dead at 6.8 s,
    the venue FILLED it at 11.1 s, and a second order was attempted. With no
    terminal verdict the order is UNKNOWN: nothing booked, nothing re-placed.
    """
    from btcpredict.execution import live
    b = _buy_broker(status=None, held=set())            # both lists readable
    assert _buy(b) is None
    assert b.last_failure == "unconfirmed"
    assert b.last_failure not in live.RETRYABLE_FAILURES, "must NOT retry"
    assert b.cancel_calls == [["o1"]], "the order is still pulled"


def test_a_LATE_fill_is_booked_not_retried():
    """The 04:52 order, replayed: SUBMITTED on the first reads, FILLED later.
    The wait must keep reading until the verdict and then BOOK it."""
    b = _buy_broker(status=None, held=set())
    reads = {"n": 0}

    async def _record(order_id, **kw):
        reads["n"] += 1
        if reads["n"] < 4:
            return {"orderId": "o1", "status": "SUBMITTED"}
        return {"orderId": "o1", "status": "FILLED", "filledShareQty": "17.5",
                "filledUsdtAmount": "10"}
    b.order_record = _record
    fill = _buy(b)
    assert fill is not None, "a late FILLED verdict is a fill"
    assert b.last_failure is None
    assert reads["n"] >= 4


def test_a_LATE_failure_is_retryable_as_soon_as_it_lands():
    """The ceiling is a ceiling: a FAILED verdict ends the wait at once."""
    import time as _t
    from btcpredict.execution import live
    b = _buy_broker(status=None, held=set())
    reads = {"n": 0}

    async def _record(order_id, **kw):
        reads["n"] += 1
        return {"orderId": "o1",
                "status": "SUBMITTED" if reads["n"] < 3 else "FAILED",
                "errorMessage": "Failed to execute the market order"}
    b.order_record = _record
    live.BUY_TERMINAL_WAIT_S = 30.0          # a long ceiling must not be spent
    t0 = _t.monotonic()
    assert _buy(b) is None
    assert b.last_failure == "venue_failed"
    assert b.last_failure in live.RETRYABLE_FAILURES
    assert _t.monotonic() - t0 < 10.0, "waited out the ceiling after FAILED"


def test_no_verdict_with_the_order_still_WORKING_does_not_retry():
    """The double-buy guard. An order the venue still holds as working can
    still execute, so placing another is a SECOND position, not a retry."""
    from btcpredict.execution import live
    b = _buy_broker(status=None, held=set(),
                    active_orders_resp={"orders": [{"orderId": "o1"}]})
    assert _buy(b) is None
    assert b.last_failure == "unconfirmed"
    assert b.last_failure not in live.RETRYABLE_FAILURES


def test_no_verdict_with_an_UNREADABLE_working_list_does_not_retry():
    """`[]` and `None` are different answers: only a list we actually READ can
    say nothing of ours is open."""
    from btcpredict.execution import live
    b = _buy_broker(status=None, held=set(), active_orders_resp=None)
    assert _buy(b) is None
    assert b.last_failure == "unconfirmed"
    assert b.last_failure not in live.RETRYABLE_FAILURES


def test_the_status_budget_is_honoured_as_written(monkeypatch):
    """The shortest configurable wait used to be 3.6s (`max(6, ...)`), so a 2s
    budget silently behaved like a 3.6s one."""
    from btcpredict.config import settings
    seen = []
    for budget, want in ((2.0, 4), (15.0, 26)):
        monkeypatch.setattr(settings, "order_status_wait_s", budget, raising=False)
        b = _buy_broker(status="FILLED", held=set())

        async def _status(order_id, **kw):
            seen.append((budget, kw["tries"]))
            return "FILLED"

        b._order_status = _status
        _buy(b)
    assert seen == [(2.0, 4), (15.0, 26)], (
        f"2s must mean 4 reads out to ~1.8s; 15s must keep the old 26: {seen}")


def test_late_history_check_catches_failed_order():
    """When _order_status times out but position check took 4s, a late history
    check catching FAILED converts unconfirmed into retryable venue_failed."""
    from btcpredict.execution import live
    b = _buy_broker(status=None, held=set(),
                    order_record_resp={"orderId": "o1", "status": "FAILED",
                                       "errorMessage": "Failed to execute the market order"})
    assert _buy(b) is None
    assert b.last_failure == "venue_failed"
    assert b.last_order_error == "Failed to execute the market order"
    assert b.last_failure in live.RETRYABLE_FAILURES


def test_late_history_check_catches_filled_order():
    """When _order_status times out and position check lagged, late history
    confirming FILLED books the verified trade."""
    b = _buy_broker(status=None, held=set(),
                    order_record_resp={"orderId": "o1", "status": "FILLED",
                                       "fillPercentage": "1"})
    fill = _buy(b)
    assert fill is not None
    assert not fill.unverified
    assert fill.order_id == "o1"


def test_unreadable_status_with_a_position_is_booked_as_verified():
    """The effect proves the cause: a position can only exist if it filled."""
    b = _buy_broker(status=None, held={"tok"})
    fill = _buy(b)
    assert fill is not None
    assert not fill.unverified
    assert fill.order_id == "o1"


def test_a_limit_fill_carries_the_token_it_bought():
    """`settle()` verifies an unverified fill by asking the venue's position list
    whether it holds that TOKEN. bind() needs the same id to persist the fill, so
    a restored fill without one can never be verified -- the check answers "not
    held" and voids a real position after 20 attempts. The market path always set
    it; buy_limit did not."""
    from btcpredict.execution import live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")

    async def quote(*a, **kw):
        return {"quoteId": "q1", "averagePrice": 0.70, "expireAt": 0}

    async def request(method, path, params=None):
        return {"orderId": "o1"}

    async def record(order_id, **kw):
        return {"orderId": "o1", "status": "CLOSED", "fillPercentage": "1",
                "filledUsdtAmount": "1.00", "filledShareQty": "1.4286",
                "marketProviderFee": "0.02"}

    b.get_quote = quote
    b._request = request
    b.order_record = record
    fill = asyncio.run(b.buy_limit(slug="s", side="Up", token_id="tok-42",
                                   limit_px=0.69, stake=1.0, wait_s=0.1))
    assert fill is not None
    assert fill.token_id == "tok-42"


def test_an_unverified_fill_survives_a_restart_with_its_token(tmp_path):
    """The whole point of persisting token_id: after a restart the fill must still
    be verifiable, or settle() voids a position we actually hold."""
    from btcpredict.execution.base import Fill
    from btcpredict.store.db import Store

    store = Store(tmp_path / "t.db")
    store.insert("fills", {
        "decision_id": 1, "slug": "s", "ts_ms": 1, "mode": "live", "side": "Up",
        "requested_px": 0.6, "filled_px": 0.6, "shares": 1.6, "stake": 1.0,
        "fee_paid": 0.0, "order_id": "o9", "raw": "{}", "token_id": "tok-42",
        "ev_at_fill": None,
    })
    bot = _bot(_StubBroker([]))
    bot.store = store
    bot.open_fills = {}
    from btcpredict.risk.guards import RiskGuard
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                          max_open_rounds=1, daily_loss_cap_pct=0.2,
                          bankroll=lambda: 10.0)
    bot._restore_open_positions()

    _fid, (slug, restored) = next(iter(bot.open_fills.items()))
    assert slug == "s"
    assert isinstance(restored, Fill)
    assert restored.token_id == "tok-42", \
        "a restored fill must keep the token, or its verification is blind"
    store.close()


def test_a_limit_fill_books_its_fee_in_usdt():
    """`marketProviderFee` on an order/history row is USDT, booked as-is.

    Settled against the venue's own accounting over all 47 FILLED limit orders
    of 2026-09-14: todayTotalCost $87.11308 minus sum(filledUsdtAmount)
    $85.81000 leaves $1.30308 of fee. Summing the field as USDT gives $1.27158
    -- the 3c gap is the venue's 2dp rounding of filledUsdtAmount. Summing it as
    shares x price gives $0.75248, off by 42%.

    This test used to assert the share convention, on one 2026-09-12 fill (0.111
    on $0.83 at px 0.1526) that reads as exactly 2.0% that way. Under-booking the
    fee makes every PnL figure optimistic: the ledger read -$18.55 on a day the
    venue realized -$19.87.
    """
    from btcpredict.execution import live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")

    async def quote(*a, **kw):
        return {"quoteId": "q1", "averagePrice": 0.15, "expireAt": 0}

    async def request(method, path, params=None):
        return {"orderId": "o1"}

    async def record(order_id, **kw):
        return {"orderId": "o1", "status": "CLOSED", "fillPercentage": "1",
                "filledUsdtAmount": "0.83", "filledShareQty": "5.44",
                "marketProviderFee": "0.111"}

    b.get_quote = quote
    b._request = request
    b.order_record = record
    fill = asyncio.run(b.buy_limit(slug="s", side="Up", token_id="t",
                                   limit_px=0.18, stake=1.0, wait_s=0.1))
    assert fill is not None
    assert fill.filled_px == pytest.approx(0.83 / 5.44, abs=1e-4)
    assert fill.fee_paid == pytest.approx(0.111, abs=1e-4), \
        "the field is USDT; multiplying by px books only ~px of the real fee"


def test_unreadable_status_and_unreadable_positions_books_provisionally():
    """Neither endpoint answered: hold the fill as provisional rather than
    dropping it, because dropping a real position strands its winnings. settle()
    gets minutes to resolve it instead of the order path's second."""
    b = _buy_broker(status=None, held=None)
    fill = _buy(b)
    assert fill is not None and fill.unverified


# --- the venue's reason, and how long we wait for it -------------------------
#
# 2026-09-12: the last two live orders were logged as "status=FAILED with no
# position held" and then "unverifiable ... NOT booking", with no cause shown.
# The venue had written the cause into the order-history record the whole time
# (`errorMessage: "Failed to execute the market order"`), and it had FILLED the
# second order at +0.75s -- order/history did not show it within the old 2s
# budget and the position list was still empty at 3.5s, so the patience was the
# other half of the problem.

def test_the_venues_own_reason_is_read_and_kept():
    """A FAILED record carries `errorMessage`; it must not be thrown away."""
    from btcpredict.execution import live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")
    payload = {"orders": [{"orderId": "o9", "status": "FAILED",
                           "errorMessage": "Failed to execute the market order"}]}

    async def request(method, path, params):
        return payload

    b._request = request
    assert asyncio.run(b._order_status("o9")) == "FAILED"
    assert b.last_order_error == "Failed to execute the market order"


def test_a_slow_history_read_still_finds_the_fill():
    """The 2026-09-12 discard: the fill existed but history lagged.

    Four empty reads used to be the whole budget, so an order the venue had
    already FILLED was reported unreadable, the position list was asked too
    early, and the fill was dropped. History that answers on the fifth read must
    now be found and booked.
    """
    from btcpredict.execution import live

    b = live.LiveBroker(None, "k", "s", wallet_address="w", wallet_id="1")
    calls = {"n": 0}

    async def quote(*a, **kw):
        return {"quoteId": "q1", "amountOut": to_wei(17.5),
                "feeAmount": to_wei(0.1), "averagePrice": 0.57, "expireAt": 0}

    async def request(method, path, params):
        if path == live.ORDER_HISTORY:
            calls["n"] += 1
            if calls["n"] < 5:
                return {"orders": []}          # lagging, as the live venue is
            return {"orders": [{"orderId": "o1", "status": "FILLED"}]}
        return {"orderId": "o1"}

    async def positions():
        return set()                           # would have said "not held"

    b.get_quote = quote
    b._request = request
    b._position_token_ids = positions
    fill = asyncio.run(b.buy(slug="s", side="Up", token_id="tok",
                             price=0.57, stake=10.0))
    assert fill is not None, "a lagging history read must not discard a real fill"
    assert fill.order_id == "o1"
    assert b.last_failure is None


def test_a_confirmed_status_needs_no_position_lookup():
    b = _buy_broker(status="FILLED", held=set())
    calls = []
    b._position_token_ids = lambda: calls.append(1)     # must not be reached
    fill = _buy(b)
    assert fill is not None and not fill.unverified
    assert calls == []


def test_a_position_in_another_token_does_not_confirm_this_order():
    b = _buy_broker(status=None, held={"someone-elses-token"})
    assert _buy(b) is None


class TestTheReservedSlot:
    """A reserved arm (the cross) holds a slot of its own, alongside one main position.

    ADDED 2026-09-13. `check()` refused the cross arm on any round the main arm
    held, and since the main arm enters at tau 60-210 and holds to settlement
    while the cross fires at tau 3-60, that was most rounds the ticket wanted.
    The two are different signals at different prices and frequently opposite
    sides, so the collision was crowding-out rather than risk control.

    What must NOT change: every halt, and the daily loss cap, still stop it.
    """

    def _guard(self):
        from btcpredict.risk.guards import RiskGuard
        return RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=5,
                         max_open_rounds=1, daily_loss_cap_pct=0.20,
                         bankroll=lambda: 50.0)

    def test_a_reserved_arm_may_enter_a_round_the_main_arm_holds(self):
        g = self._guard()
        g.on_open("r1")
        assert g.check("r1")[0] is False, "the main arm still dedups itself"
        assert g.check_reserved("r1")[0] is True, "the reserved arm has its own slot"

    def test_the_main_arm_is_not_given_the_reserved_slot(self):
        """max_open_rounds stays 1 for the main arm: the second slot is the
        reserved arm's, not a second model position."""
        g = self._guard()
        g.on_open("r1")
        assert g.check("r2")[0] is False, "one main position at a time"

    def test_the_reserved_slot_is_single(self):
        g = self._guard()
        g.on_open("r1", reserved=True)
        assert g.check_reserved("r2")[0] is False
        assert g.check_reserved("r1")[0] is False

    def test_settling_releases_the_right_slot(self):
        g = self._guard()
        g.on_open("r1")                       # main
        g.on_open("r1", reserved=True)          # ticket, same round
        g.on_settle("r1", 0.5, reserved=True)
        assert g.state.reserved_rounds == set(), "the ticket slot is freed"
        assert g.state.open_rounds == {"r1"}, "the main position still stands"
        g.on_settle("r1", 0.5)
        assert g.state.open_rounds == set()

    def test_a_halt_still_stops_the_reserved_arm(self):
        g = self._guard()
        g._halt("daily", "daily loss cap hit")
        assert g.check_reserved("r1")[0] is False

    def test_the_daily_cap_still_stops_the_reserved_arm(self):
        g = self._guard()
        g.state.daily_pnl = -10_000.0
        assert g.check_reserved("r1")[0] is False

    def test_the_loss_streak_still_stops_the_reserved_arm(self):
        g = self._guard()
        g.state.consecutive_losses = 5
        assert g.check_reserved("r1")[0] is False


# --- voids must give the slot back (regression, 2026-09-13) -----------------
#
# The settle loop's void branch `continue`d past on_settle, but _book_fill had
# already claimed the slot via on_open. The claim then outlived the process.
# One void was enough to silence the MAIN arm, whose pool is a set bounded by
# max_open_rounds=1; the cross arm needed cross_max_per_round of them. It
# self-healed on restart -- a voided fill has a settlement row, so
# _restore_open_positions does not replay it -- which is why a bot restarting
# hundreds of times a day never surfaced it.

def _guard(**kw):
    kw.setdefault("daily_loss_cap", 0.0)
    kw.setdefault("max_consecutive_losses", 5)
    kw.setdefault("max_open_rounds", 1)
    return RiskGuard(**kw)


def test_a_void_releases_the_main_slot():
    g = _guard()
    g.on_open("r1")
    assert g.check("r2")[0] is False, "the main pool is full while r1 is held"
    g.release("r1")
    assert g.check("r2")[0] is True, "a void must hand the slot back"


def test_a_void_releases_the_reserved_slot():
    g = _guard()
    g.on_open("r1", reserved=True)
    assert g.check_reserved("r1", max_open=1)[0] is False
    g.release("r1", reserved=True)
    assert g.check_reserved("r1", max_open=1)[0] is True


def test_a_void_releases_a_named_pool():
    g = _guard()
    g.on_open("r1", pool="impulse")
    assert g.check_pool("r2", pool="impulse", max_open=1)[0] is False
    g.release("r1", pool="impulse")
    assert g.check_pool("r2", pool="impulse", max_open=1)[0] is True


def test_a_void_records_no_pnl_and_no_loss_streak():
    """The reason this is `release` and not `on_settle(pnl=0.0)`.

    A void deployed no stake, so it is neither a win nor a loss. Settling it at
    zero would count as a non-loss and RESET consecutive_losses, laundering a
    real streak the guard is meant to halt on.
    """
    g = _guard()
    g.on_open("r1", reserved=True)
    g.on_settle("prev", -1.0)              # a real loss on an earlier round
    g.on_settle("prev2", -1.0)
    before_pnl = g.state.daily_pnl
    before_streak = g.state.consecutive_losses
    assert before_streak == 2
    g.release("r1", reserved=True)
    assert g.state.daily_pnl == before_pnl, "a void moves no money"
    assert g.state.consecutive_losses == before_streak, (
        "a void must not launder a live loss streak")


def test_repeated_voids_cannot_drive_the_reserved_count_negative():
    g = _guard()
    g.on_open("r1", reserved=True)
    for _ in range(5):
        g.release("r1", reserved=True)
    assert g.state.reserved_open == 0


# --- the daily seed must not be truncated by a row limit (2026-09-13) -------

@pytest.fixture
def store():
    import pathlib as _pl
    import tempfile as _tf
    from btcpredict.store.db import Store
    with _tf.TemporaryDirectory() as d:
        st = Store(_pl.Path(d) / "seed.db")
        yield st
        st.close()



def _seed_bot(store, pnls, mode="live"):
    """A bot whose only job is to run _seed_guard_state over `pnls`."""
    import time as _t
    from types import SimpleNamespace
    from btcpredict.main import Bot
    bot = Bot.__new__(Bot)
    bot.store = store
    bot.broker = SimpleNamespace(mode="live")
    bot.guard = RiskGuard(daily_loss_cap=0.0, max_consecutive_losses=0,
                          max_open_rounds=1, daily_loss_cap_pct=0.20,
                          bankroll=lambda: 50.0)
    now = _t.time()
    midnight = now - (now % 86400.0)
    for i, p in enumerate(pnls):
        fid = store.insert("fills", {
            "decision_id": None, "slug": f"btc-updown-5m-{i}",
            "ts_ms": int((midnight + i) * 1000), "mode": mode, "side": "Up",
            "requested_px": 0.5, "filled_px": 0.5, "shares": 2.0, "stake": 1.0,
            "fee_paid": 0.0, "order_id": str(i), "raw": "{}", "token_id": "1"})
        store.insert("settlements", {
            "fill_id": fid, "slug": f"btc-updown-5m-{i}",
            "ts_ms": int((midnight + i) * 1000), "won": int(p > 0),
            "payout": max(p, 0.0), "pnl": p, "mode": mode, "void": 0})
    return bot


def test_the_daily_seed_sums_the_whole_day_not_the_last_50(store):
    """REGRESSION 2026-09-13: the bot halted itself out of a WINNING day.

    The seed shared one `ORDER BY ts_ms DESC LIMIT 50` with the streak walk.
    The day held 145 settlements worth +$2.49; its newest 50 summed to -$7.85.
    Every restart re-seeded that phantom loss, two real losses tripped the
    $9.49 cap, and it re-halted on every restart because the seed is re-read
    each time.

    Shaped like the real day: 100 winners first, then 50 losers.
    """
    pnls = [1.0] * 100 + [-0.5] * 50            # true total +$75, last 50 = -$25
    bot = _seed_bot(store, pnls)
    bot._seed_guard_state()
    assert bot.guard.state.daily_pnl == pytest.approx(75.0), (
        "the daily total must cover the whole day, not a row window")
    assert not bot.guard.state.halted


def test_the_streak_still_reads_only_the_newest_settlements(store):
    """The limit is correct for the streak: it only needs the newest few."""
    bot = _seed_bot(store, [1.0] * 100 + [-0.5] * 3)
    bot.guard.max_consecutive_losses = 5
    bot._seed_guard_state()
    assert bot.guard.state.consecutive_losses == 3


def test_a_genuinely_losing_day_still_halts(store):
    """The fix must not disarm the cap -- only stop it firing on a good day."""
    bot = _seed_bot(store, [-1.0] * 60)          # -$60 against a $10 cap
    bot._seed_guard_state()
    assert bot.guard.state.halted
    assert bot.guard.state.halt_kind == "daily"
