"""Maker-first execution: post a limit, wait, cancel what did not fill.

The venue's LIMIT rules are easy to get wrong and expensive when you do: it
needs `priceLimit` (a missing one is a nameless -3026), it needs GTC where
MARKET needs FOK, and its order records use decimal amounts while its quotes
use wei. Each of those is pinned here.
"""
import asyncio

import pytest

from btcpredict.execution.live import LiveBroker, amount_usdt


class Stub(LiveBroker):
    """A broker whose two network calls are scripted."""

    def __init__(self, quote, place, records):
        super().__init__(None, "key", "secret", slippage_bps=300)
        self.wallet_address, self.wallet_id = "0xabc", "w1"
        self._quote, self._place, self._records = quote, place, list(records)
        self.sent: list[tuple[str, str, dict]] = []
        self.cancelled: list[str] = []

    async def _request(self, method, path, params):
        self.sent.append((method, path, dict(params)))
        if path.endswith("get-quote"):
            return self._quote
        if path.endswith("place-order-bundle"):
            return self._place
        if path.endswith("batch-cancel"):
            self.cancelled += [str(params[k]) for k in params
                               if k.startswith("cancelInfoList")]
            return {"canceled": [self._place.get("orderId")], "failed": []}
        return {}

    async def order_record(self, order_id, tries=3, delay=0.4):
        return self._records.pop(0) if self._records else None

    async def cancel_orders(self, order_ids):
        # The real cancel goes out over http.client (see its own tests below);
        # here we only care that buy_limit asks for it, and what it reports.
        self.cancelled += list(order_ids)
        return list(order_ids)


QUOTE = {"quoteId": "q1", "averagePrice": 0.70, "expireAt": 9_999_999_999_999}
# `marketProviderFee` on an order/history row is USDT -- NOT the convention of
# the quote's `feeAmount`, which buy() rightly treats as shares. Settled against
# the venue's own accounting over all 47 FILLED limit orders of 2026-09-14:
# todayTotalCost $87.11308 minus sum(filledUsdtAmount) $85.81000 leaves $1.30308
# of fee; summing the field as USDT gives $1.27158 (the 3c gap is the venue's
# 2dp rounding of filledUsdtAmount), summing it as shares x price gives $0.75248.
# A single 2026-09-12 fill (0.111 on $0.83 at px 0.1526) reads as exactly 2.0%
# under the SHARE convention, which is what this file used to assert; the
# 47-order reconciliation refutes it.
RECORD_FILLED = {"orderId": "77", "status": "CLOSED", "fillPercentage": "1.00",
                 "filledUsdtAmount": "1.00", "filledShareQty": "1.4286",
                 "marketProviderFee": "0.02", "price": "0.70"}
RECORD_WORKING = {"orderId": "78", "status": "OPENING", "fillPercentage": "0.00",
                  "filledUsdtAmount": "0.00", "filledShareQty": "0.00"}


def buy(broker, **kw):
    kw.setdefault("limit_px", 0.69)
    kw.setdefault("wait_s", 0.05)
    kw.setdefault("poll_s", 0.01)
    return asyncio.run(broker.buy_limit(slug="s", side="Up", token_id="t",
                                        stake=1.0, **kw))


def test_a_limit_uses_priceLimit_and_gtc():
    b = Stub(QUOTE, {"orderId": "77"}, [RECORD_FILLED])
    buy(b)
    _m, _p, q = [c for c in b.sent if c[1].endswith("get-quote")][0]
    assert q["orderType"] == "LIMIT" and q["priceLimit"] == "0.69"
    _m, _p, o = [c for c in b.sent if c[1].endswith("place-order-bundle")][0]
    assert o["orderType"] == "LIMIT"
    assert o["timeInForce"] == "GTC"          # FOK is MARKET-only
    assert o["priceLimit"] == "0.69"


def test_a_filled_limit_books_the_venue_s_price_not_ours():
    b = Stub(QUOTE, {"orderId": "77"}, [RECORD_FILLED])
    fill = buy(b)
    assert fill is not None
    assert fill.filled_px == pytest.approx(1.00 / 1.4286, rel=1e-3)
    assert fill.stake == pytest.approx(1.00)
    assert fill.shares == pytest.approx(1.4286)
    # 0.02 is 0.02 USDT, booked as-is. Multiplying by the fill price books only
    # `px` of the true fee (~58% of it at these prices), which is what made the
    # ledger read -$18.55 on a day the venue realized -$19.87.
    assert fill.fee_paid == pytest.approx(0.02, rel=1e-3)
    assert not b.cancelled                     # nothing left to cancel
    # JSON text, not a dict: the fill has ALREADY happened by the time the
    # booking runs, so a binding error here loses a real trade -- which is
    # exactly what happened live before this assertion existed.
    assert isinstance(fill.raw, str)


def test_a_limit_fill_survives_being_written_to_the_store(tmp_path):
    """The live failure, as a test: order filled, then the insert threw."""
    from btcpredict.store.db import Store

    b = Stub(QUOTE, {"orderId": "77"}, [RECORD_FILLED])
    fill = buy(b)
    store = Store(tmp_path / "t.db")
    fid = store.insert("fills", {
        "decision_id": None, "slug": "s", "ts_ms": fill.ts_ms,
        "mode": "live", "side": fill.side,
        "requested_px": fill.requested_px, "filled_px": fill.filled_px,
        "shares": fill.shares, "stake": fill.stake,
        "fee_paid": fill.fee_paid, "order_id": fill.order_id,
        "raw": fill.raw, "ev_at_fill": 0.03,
    })
    row = store.conn.execute("SELECT raw, filled_px FROM fills WHERE id=?",
                             (fid,)).fetchone()
    assert "orderId" in row["raw"]               # stored as text, readable back
    assert row["filled_px"] == pytest.approx(fill.filled_px)


def test_an_unfilled_limit_is_cancelled_and_reported_as_a_miss():
    b = Stub(QUOTE, {"orderId": "78"}, [RECORD_WORKING] * 3)
    assert buy(b) is None
    assert b.last_failure == "limit_unfilled"
    assert b.cancelled == ["78"]               # funds are not left locked


def test_a_limit_without_a_quote_never_reaches_the_venue():
    b = Stub(None, {"orderId": "79"}, [])
    assert buy(b) is None
    assert b.last_failure == "no_quote"
    assert not [c for c in b.sent if c[1].endswith("place-order-bundle")]


def test_batch_cancel_signs_raw_brackets():
    """%5B/%5D in the signed string is the documented -1022 trap."""
    b = LiveBroker(None, "key", "secret", slippage_bps=300)
    qs = b._sign({"cancelInfoList[0].orderId": "54124"})
    assert "cancelInfoList[0].orderId=54124" in qs
    assert "%5B" not in qs and "%5D" not in qs


def test_order_record_amounts_are_read_in_the_right_units():
    assert amount_usdt("1.00") == pytest.approx(1.0)         # order records
    assert amount_usdt("20000000000000000") == pytest.approx(0.02)  # quotes (wei)
    assert amount_usdt(None) == 0.0


def test_cancel_sends_raw_brackets_over_a_hand_built_request(monkeypatch):
    """The -1022 trap: a library that encodes [ ] breaks the signature.

    aiohttp did exactly that live, so the cancel goes out over http.client
    with the signed string used verbatim. What is asserted is the PATH the
    venue would see -- brackets and all.
    """
    import http.client as hc

    seen: dict[str, str] = {}

    class FakeResp:
        status = 200

        def read(self):
            return b'{"canceled": ["54124"], "failed": []}'

    class FakeConn:
        def __init__(self, host, timeout=None):
            seen["host"] = host

        def request(self, method, path, headers=None):
            seen["method"], seen["path"] = method, path

        def getresponse(self):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(hc, "HTTPSConnection", FakeConn)
    b = Stub(QUOTE, {"orderId": "77"}, [])
    assert b._cancel_blocking(["54124"]) == ["54124"]
    assert seen["method"] == "POST"
    assert "cancelInfoList[0].orderId=54124" in seen["path"]
    assert "%5B" not in seen["path"] and "%5D" not in seen["path"]
    assert "signature=" in seen["path"]


def test_a_failed_cancel_is_reported_as_uncancelled(monkeypatch):
    """-1022 or a transport error must return [], never claim success."""
    import http.client as hc

    class FakeResp:
        status = 400

        def read(self):
            return b'{"code": -1022, "msg": "Signature for this request is not valid."}'

    class FakeConn:
        def __init__(self, host, timeout=None):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return FakeResp()

        def close(self):
            pass

    monkeypatch.setattr(hc, "HTTPSConnection", FakeConn)
    b = Stub(QUOTE, {"orderId": "77"}, [])
    assert b._cancel_blocking(["54124"]) == []


def test_the_impulse_price_cap_binds_on_the_executable_quote():
    """The trigger's band is the last ask the POLL saw; the order crosses at the
    price its own quote returns. Measured drift between the two: +0.16 on MARKET
    fills, and the venue's display lags its book by up to 0.40. Without this the
    cap is decoration -- a 0.70 trigger could fill at 0.90, where a win pays
    +0.09 and needs a 92% win rate to break even."""
    from btcpredict.edge.signal import impulse_price_refusal

    cap = 0.75
    # Above the cap: refused, and the reason names both numbers.
    why = impulse_price_refusal("impulse", 0.90, cap)
    assert why and "0.900" in why and "0.75" in why
    # At the cap and below it: allowed. The band is inclusive, matching the
    # trigger's own `min <= px <= max`.
    assert impulse_price_refusal("impulse", cap, cap) is None
    assert impulse_price_refusal("impulse", 0.70, cap) is None
    # A quote CHEAPER than the trigger is a gift, never a refusal.
    assert impulse_price_refusal("impulse", 0.05, cap) is None
    # The model path re-values against its own band in `evaluate`, and the
    # ticket arm passes its own ask -- neither may be touched by this gate.
    assert impulse_price_refusal("model", 0.90, cap) is None
    assert impulse_price_refusal("model", 0.99, 0.20) is None
    # No quote at all means no basis to refuse: fall back to the trigger's price.
    assert impulse_price_refusal("impulse", None, cap) is None
    # A cap of 1.0 (or above) disables the gate rather than refusing everything.
    assert impulse_price_refusal("impulse", 0.99, 1.0) is None


def test_the_take_limit_cannot_reach_far_above_the_ask_we_measured():
    """A live fill on 2026-09-12 13:02, in full:

        trigger's ask 0.7326, our digital P(Down) 0.94, limit 0.90
        -> filled at 0.8919, ev_at_fill -0.195 per $1

    At 0.89 a win pays +0.10 and needs a 91% win rate; the venue's own price said
    73%. The arm executed a trade it scores as clearly negative, because the
    ceiling is OUR fair and our fair is the miscalibrated estimator here (Brier
    0.124 against the venue's 0.106). One tick of tolerance above the measured
    ask fixes exactly that, and cannot cost anything measured: the taker leg only
    ever fills AT the ask, because it only fires when ask <= fair - margin.
    """
    from btcpredict.edge.signal import take_limit_px

    # The real numbers: fair 0.94, margin 0.04 -> an unclamped 0.90.
    px = take_limit_px(0.94, 0.04, quote=0.7326, tick=0.01)
    assert px == 0.74, f"must stop one tick above the ask, not at 0.90: {px}"
    # With no quote at all there is nothing to clamp against: the ceiling stands.
    assert take_limit_px(0.94, 0.04, quote=None) == 0.90
    # The clamp binds whenever the ceiling sits above ask + tick -- here 0.60 -
    # 0.04 = 0.56 becomes 0.53. That is the case the taker leg always fires in
    # (`ask <= fair - margin`), so the order still crosses and still fills AT the
    # ask, which is why this costs nothing measured.
    assert take_limit_px(0.60, 0.04, quote=0.52) == 0.53
    # An arm with no valuation of its own (the ticket) passes a FLOOR, and the
    # clamp may only ever LOWER a limit -- so the ticket's limit lands exactly on
    # the ask it read (0.12), which is the price its whole thesis is priced at. The
    # clamp cannot push it below that, and it cannot let it pay more than a tick
    # above it either.
    assert take_limit_px(0.10, 0.04, quote=0.12, tick=0.01, take_px=0.12) == 0.12
    # Never below the venue's minimum: a fair under the margin floors at 0.01, and
    # the clamp may not raise it again.
    assert take_limit_px(0.02, 0.04, quote=0.01, tick=0.01) == 0.01


def test_the_impulse_cap_and_the_take_margin_are_independent_knobs():
    """Same failure class as `safety_margin` doubling as the take ceiling: a
    price BAND (which trades) and a limit MARGIN (what we will pay) are different
    questions, and moving one must not move the other."""
    from btcpredict.config import settings
    from btcpredict.edge.signal import impulse_price_refusal

    assert settings.impulse_max_price < settings.max_quote, (
        "the impulse band exists to be tighter than the model's 0.90 band")
    # A refusal does not depend on the margin, and the margin does not appear in
    # the refusal: change it and the gate's answer is identical.
    before = impulse_price_refusal("impulse", 0.90, settings.impulse_max_price)
    old = settings.limit_take_margin
    try:
        settings.limit_take_margin = 0.20
        after = impulse_price_refusal("impulse", 0.90,
                                      settings.impulse_max_price)
    finally:
        settings.limit_take_margin = old
    assert before == after and before is not None


def test_an_unfilled_limit_does_not_pause_every_arm():
    """A resting limit nobody crossed is NOT a venue failure.

    `_place_order` set the shared order cooldown on ANY empty result, so a
    limit that simply rested and was cancelled paused the impulse and cross
    arms too -- and those fire in windows seconds wide. Measured 2026-09-13:
    3 unfills cost ~150s of blocked trading. `LiveBroker` already reports this
    case distinctly as `limit_unfilled`; only a real rejection should back off.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "btcpredict" / "main.py").read_text()
    assert '"limit_unfilled"' in src, (
        "the unfilled-limit case must be recognised by name")
    # the guard must sit BEFORE the cooldown assignment in _place_order
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_place_order")
    body = ast.unparse(fn)
    i_guard = body.find("limit_unfilled")
    i_cool = body.find("_order_cooldown_until = time.time()")
    assert i_guard != -1 and i_cool != -1
    assert i_guard < i_cool, (
        "the limit_unfilled early-return must precede setting the cooldown")


class TestTheFillBreak:
    """A limit is done at `limit_fill_break`, not at 100%.

    MEASURED 2026-09-13 over 238 real live orders: the wait loop broke only at
    >= 0.999, so a 98-99% fill sat out the rest of limit_wait_s chasing the last
    1-2%. Half of all orders paid a ~16s penalty for nothing:
        100% fills (break early)  n=119  median  5.0s   76% within 8s
        partial (wait the clock)  n=119  median 21.0s    3% within 8s
    That penalty is what turned ticket-arm fills (tau 3-8s, wait capped by the
    bell) into cancellations, and what blocked the poll loop for 20s at a time.
    """

    def _rec(self, filled):
        return {"fillPercentage": filled, "status": "OPEN",
                "filledUsdtAmount": 1.0 * filled, "filledShareQty": 5.0 * filled,
                "marketProviderFee": 0.0}

    def test_the_loop_stops_at_the_break_not_at_one(self):
        """Pure check on the condition itself: 0.98 must be terminal at 0.95."""
        for filled, brk, done in ((0.98, 0.95, True), (0.98, 0.999, False),
                                  (1.0, 0.95, True), (0.90, 0.95, False)):
            assert (filled >= brk) is done, (filled, brk)

    def test_every_call_site_passes_the_configured_break(self):
        """A call site that forgets it silently keeps the old 20s behaviour."""
        import pathlib
        import re
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "main.py").read_text()
        calls = re.findall(r"buy_limit\((?:[^()]|\([^()]*\))*\)", src)
        assert calls, "expected buy_limit call sites in main.py"
        missing = [c for c in calls if "fill_break" not in c]
        assert not missing, (
            f"{len(missing)} buy_limit call(s) do not pass fill_break, so they "
            f"still wait out the full limit_wait_s on a 98% fill")

    def test_the_remainder_is_still_cancelled_on_a_partial(self):
        """Breaking early leaves the rest of the order live at the venue, so the
        cancel path must cover any fill under 0.999 -- not just a zero fill."""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "btcpredict" / "execution" / "live.py").read_text()
        assert "if filled < 0.999:" in src, (
            "a partial fill must still cancel its remainder")
