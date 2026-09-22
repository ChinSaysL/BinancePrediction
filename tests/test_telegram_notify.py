"""Telegram notifications: what they say, what the buttons do, and the two
guarantees the trading loop depends on.

The loop guarantees are the reason this file exists rather than a smoke test:

  1. `notify()` CANNOT BLOCK AND CANNOT RAISE. It is called from `_book_fill`,
     `_place_order` and `_settle_due` -- i.e. from inside the tick, on the path
     that decides trades. An exception here, or an `await` that stalls on an
     unreachable api.telegram.org, would cost trades; the tests below pin the
     behaviour that stops that.

  2. A RETRY CANNOT BE PLACED TWICE. The RETRY NOW button places a real MARKET
     order. A double tap, or a redelivered callback, must not buy the same round
     twice, so the handler is single-flight and that is asserted directly.
"""
from __future__ import annotations

import asyncio

import pytest

from btcpredict.notify.telegram import TelegramNotifier, _human_reason

SLUG = "btc-updown-5m-1790076600"
CHART = "https://example.test/round/{slug}"


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = body

    async def json(self, content_type=None):            # noqa: ARG002
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """A stand-in for aiohttp.ClientSession that records what was sent."""

    def __init__(self, respond=None):
        self.calls: list[tuple[str, dict]] = []
        self._respond = respond or (lambda m, p: (200, {"ok": True}))

    def post(self, url, json=None, timeout=None):        # noqa: A002, ARG002
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        out = self._respond(method, json)
        if isinstance(out, Exception):
            raise out
        status, body = out
        return _Resp(status, body)

    def payloads(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


def _notifier(session=None, **kw) -> TelegramNotifier:
    kw.setdefault("chart_url", CHART)
    return TelegramNotifier(token="tok", chat_id="42",
                            session=session or FakeSession(), **kw)


def _take(n: TelegramNotifier) -> tuple[str, list | None]:
    assert not n._q.empty(), "nothing was enqueued"
    text, buttons = n._q.get_nowait()
    return text, buttons


# -- rendering --------------------------------------------------------------

def test_entry_message_carries_the_trade_and_a_chart_button():
    n = _notifier()
    n.entry(slug=SLUG, side="Up", stake=2.0, px=0.613, shares=3.26,
            edge=0.148, ev=0.0836, tau=178.0, spot=86012.5, strike=85975.0)
    text, buttons = _take(n)
    for frag in ("ENTRY Up", SLUG, "$2.00", "3.26 sh", "0.6130",
                 "edge +0.1480", "ev/$ +0.0836", "tau 178s", "strike 85,975.00"):
        assert frag in text, f"{frag!r} missing from:\n{text}"
    assert buttons == [[{"text": "📈 Chart",
                         "url": f"https://example.test/round/{SLUG}"}]]


def test_entry_omits_a_fabricated_edge_when_the_signal_has_no_valuation():
    """The cross arm prices off the ask it is about to pay, so its edge is a
    structural zero. Showing "+0.0000" would be a fabricated number."""
    n = _notifier()
    n.entry(slug=SLUG, side="Down", stake=2.0, px=0.55, shares=3.6,
            edge=None, ev=None, tau=90.0, spot=None, strike=None, arm="cross")
    text, _ = _take(n)
    assert "edge" not in text and "ev/$" not in text
    assert "(cross)" in text


def test_failed_entry_states_the_reason_and_offers_retry():
    n = _notifier(on_retry=lambda did: None)
    n.entry_failed(slug=SLUG, side="Up", stake=2.0,
                   reason="insufficient_balance", attempts=3, did=1234,
                   tau=140.0)
    text, buttons = _take(n)
    assert "ENTRY FAILED Up" in text
    assert "not enough USDT" in text          # the human mapping, not the code
    assert "code: insufficient_balance" in text
    assert "3 attempt(s)" in text
    assert [(b.get("callback_data") or b.get("url")) for row in buttons
            for b in row] == ["retry:1234", f"https://example.test/round/{SLUG}"]
    # The consequence of the button is stated, so a tap is an informed act.
    assert "MARKET order" in text


def test_a_repeating_entry_failure_notifies_once_per_round_per_side():
    """MEASURED: the `-9000 not enough USDT` window on 2026-09-22 produced a
    terminal `_place_order` failure roughly ONCE PER SECOND for 45s across one
    15m round -- the cooldown deters the next tick, not the loop already inside
    `_place_order`. Unthrottled that is ~45 messages for one condition."""
    n = _notifier(on_retry=lambda did: None)
    for _ in range(45):
        n.entry_failed(slug=SLUG, side="Up", stake=1.88,
                       reason="insufficient_balance", attempts=3, did=1234)
    assert n._q.qsize() == 1, f"{n._q.qsize()} messages for ONE condition"
    # The other side of the same round is a different condition...
    n.entry_failed(slug=SLUG, side="Down", stake=1.88,
                   reason="insufficient_balance", attempts=3, did=1235)
    # ...and so is another round.
    n.entry_failed(slug="btc-updown-5m-1790076900", side="Up", stake=1.88,
                   reason="insufficient_balance", attempts=3, did=1236)
    assert n._q.qsize() == 3


def test_retry_button_is_omitted_when_there_is_no_handler():
    n = _notifier()                                   # no on_retry
    n.entry_failed(slug=SLUG, side="Up", stake=2.0, reason="rejected",
                   attempts=2, did=9)
    text, buttons = _take(n)
    assert "RETRY" not in text
    assert all("callback_data" not in b for row in buttons for b in row)


def test_retry_callback_data_fits_the_telegram_limit():
    """Telegram rejects the whole message if callback_data exceeds 64 bytes."""
    n = _notifier(on_retry=lambda did: None)
    btn = n._retry_button(10 ** 18)
    assert btn is not None
    assert len(btn["callback_data"].encode()) <= 64


def test_exit_message_names_the_rule_that_fired():
    n = _notifier()
    n.exit(slug=SLUG, side="Up", shares=3.26, px=0.54, proceeds=1.76,
           pnl=-0.28, tau=92.0, rule="react", running="+$21.30")
    text, buttons = _take(n)
    assert "EXIT" in text and "react" in text
    assert "sold 3.26 sh @ 0.5400" in text
    assert "pnl -0.2800" in text
    assert "+$21.30" in text
    assert buttons[0][0]["text"] == "📈 Chart"


def test_partial_exit_says_so():
    n = _notifier()
    n.exit(slug=SLUG, side="Up", shares=1.0, px=0.5, proceeds=0.5, pnl=-0.1,
           tau=60.0, rule="react", partial=True, frac=0.25)
    text, _ = _take(n)
    assert "partial 25%" in text


@pytest.mark.parametrize("won,head", [(True, "✅ WIN"), (False, "❌ LOSS")])
def test_settlement_says_win_or_loss_and_quotes_equity(won, head):
    n = _notifier()
    n.settled(slug=SLUG, side="Up", stake=2.0, payout=3.34, pnl=1.31,
              won=won, bankroll=12.18)
    text, _ = _take(n)
    assert text.startswith(head)
    assert "$12.18" in text
    assert "pnl +1.3100" in text


def test_stuck_exit_is_deduped_per_slug():
    """The sell is retried EVERY TICK; one message per tick is the noise this
    module exists to avoid."""
    n = _notifier()
    for _ in range(5):
        n.exit_stuck(slug=SLUG, side="Up", shares=0.5,
                     reason="sell_below_venue_minimum")
    assert n._q.qsize() == 1
    text, _ = _take(n)
    assert "CANNOT SELL" in text
    assert "venue's minimum" in text


def test_chart_url_without_a_slug_placeholder_is_used_verbatim():
    n = _notifier(chart_url="https://example.test/fixed")
    assert n._chart_button(SLUG) == {"text": "📈 Chart",
                                     "url": "https://example.test/fixed"}


def test_no_chart_button_when_the_template_is_empty_or_not_a_url():
    assert _notifier(chart_url="")._chart_button(SLUG) is None
    assert _notifier(chart_url="javascript:alert(1)")._chart_button(SLUG) is None


@pytest.mark.parametrize("code,expect", [
    ("insufficient_balance", "not enough USDT"),
    ("sell_below_venue_minimum", "venue's minimum"),
    ("fak_unfilled", "nothing at or below the ceiling"),
    ("", "no reason reported"),
    ("something_new", "something new"),
])
def test_human_reason_is_readable_and_never_empty(code, expect):
    assert expect in _human_reason(code)


# -- the two guarantees -----------------------------------------------------

def test_notify_never_raises_and_enqueues_nothing_when_disabled():
    n = TelegramNotifier(token="", chat_id="", session=FakeSession(),
                         enabled=True)
    assert n.enabled is False
    n.notify("anything")
    n.entry_failed(slug=SLUG, side="Up", stake=2.0, reason="rejected",
                   attempts=1)
    assert n._q.qsize() == 0


def test_a_full_queue_drops_and_counts_instead_of_blocking():
    n = _notifier(queue_max=2)
    for i in range(5):
        n.notify(f"msg {i}")
    assert n._q.qsize() == 2
    assert n.stats()["dropped"] == 3
    # The two that fit are the FIRST two; the drop is reported on the next
    # drain so a silent loss is impossible.
    assert [_take(n)[0], _take(n)[0]] == ["msg 0", "msg 1"]


def test_notify_once_fires_once_per_window_then_again(monkeypatch):
    n = _notifier()
    n.notify_once("k", "first")
    n.notify_once("k", "second")
    assert n._q.qsize() == 1
    assert _take(n)[0] == "first"
    # A different key is a different condition.
    n.notify_once("other", "third")
    assert n._q.qsize() == 1


def test_deliver_degrades_a_refused_keyboard_instead_of_losing_the_message():
    """Telegram rejects the WHOLE sendMessage with 400 for a bad URL button.
    One wrong `telegram_chart_url` must not eat every notification."""
    seen = []

    def respond(method, payload):
        if method == "sendMessage":
            seen.append(payload)
            if "reply_markup" in payload:
                return (400, {"ok": False, "description": "BUTTON_URL_INVALID"})
        return (200, {"ok": True})

    n = _notifier(session=FakeSession(respond))
    asyncio.run(n._deliver("hello", [[{"text": "x", "url": "http://bad"}]]))
    assert len(seen) == 2, "the text was not re-sent without the keyboard"
    assert "reply_markup" in seen[0]
    assert "reply_markup" not in seen[1]
    assert n.stats()["sent"] == 1 and n.stats()["failed"] == 0


def test_deliver_survives_a_transport_error():
    calls = {"n": 0}

    def respond(method, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("network unreachable")
        return (200, {"ok": True})

    n = _notifier(session=FakeSession(respond))
    asyncio.run(n._deliver("hello", None))
    assert n.stats()["sent"] == 1


def test_a_retry_callback_is_single_flight():
    """Two taps on RETRY NOW must place ONE market order."""
    calls = []

    async def main():
        started = asyncio.Event()
        release = asyncio.Event()

        async def on_retry(did):
            calls.append(did)
            started.set()
            await release.wait()
            return "placed 3.2 sh @ 0.6100"

        n = _notifier(on_retry=on_retry)
        t1 = asyncio.create_task(n._handle_callback({"id": "a", "data": "retry:7"}))
        await started.wait()                 # the first is now in flight
        t2 = asyncio.create_task(n._handle_callback({"id": "b", "data": "retry:7"}))
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(t1, t2)

    asyncio.run(main())
    assert calls == [7], f"the retry ran {len(calls)} times"


def test_a_retry_callback_answers_the_button_and_reports_back():
    async def main():
        n = _notifier(on_retry=lambda did: _ok(did))
        await n._handle_callback({"id": "a", "data": "retry:7"})
        return n

    n = asyncio.run(main())
    answered = n.session.payloads("answerCallbackQuery")
    assert answered and answered[0]["callback_query_id"] == "a"
    assert n._q.qsize() == 1
    assert "RETRY 7" in _take(n)[0]


async def _ok(did):
    return f"placed decision {did}"


def test_an_unsupported_callback_is_acknowledged_not_ignored():
    async def main():
        n = _notifier()
        await n._handle_callback({"id": "z", "data": "nonsense"})
        return n

    n = asyncio.run(main())
    assert n.session.payloads("answerCallbackQuery")[0]["text"] == "unsupported"


def test_start_is_a_no_op_when_disabled():
    async def main():
        n = TelegramNotifier(token="", chat_id="", session=FakeSession(),
                             enabled=True)
        await n.start()
        await n.stop()
        return n

    n = asyncio.run(main())
    assert n._tasks == []
    assert n.session.calls == [], "a disabled notifier must not call Telegram"
