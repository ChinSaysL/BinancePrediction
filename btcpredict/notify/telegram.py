"""Telegram notifications with inline-keyboard buttons.

WHY A QUEUE INSTEAD OF JUST AWAITING THE SEND. The poll cadence IS this arm's
edge: the model is evaluated once per tick, and this repo has already measured
what happens when that cadence slips -- widening the websocket book took it
1.00s -> 4.15s and the arm stopped emitting (see `_idle`). An HTTPS POST to
api.telegram.org from inside the tick would put a 50-300 ms round trip, and on
a bad day a multi-second timeout, on the critical path of a real-money
decision. So `notify()` only ever enqueues: it is `put_nowait`, it cannot
block, and it cannot raise into the caller. A single sender task drains the
queue, which also means messages keep their order.

A FULL QUEUE DROPS THE NEWEST MESSAGE AND COUNTS IT. The alternative --
blocking until there is room -- trades a lost notification for a lost trade.
The count is reported on the next successful send so a silent drop is
impossible; the operator sees "N message(s) dropped".

A FAILED SEND RETRIES AND THEN GIVES UP, LOUDLY IN THE LOG. Nothing in the
trading path is waiting on it, so a Telegram outage costs notifications and
nothing else.

BUTTONS. Every entry/exit carries a URL button to the venue's own page for
that round (the "chart"). A FAILED entry additionally carries a callback
button, `RETRY NOW`, which re-places the same side as a MARKET order. Callback
handling needs an inbound channel, so a second task long-polls `getUpdates`.
Two properties matter there and both are implemented below: the backlog is
DRAINED at startup (so a restart does not replay yesterday's taps), and a
retry for a given decision is SINGLE-FLIGHT (so a double tap cannot buy the
same round twice).

A BAD URL BUTTON MUST NOT DESTROY THE MESSAGE. Telegram rejects the whole
`sendMessage` with 400 `BUTTON_URL_INVALID` if a URL button is malformed --
which would silently cost every notification because of a wrong
`telegram_chart_url`. So a 400 on a message that carried buttons is retried
once WITHOUT the keyboard: the text always lands, the button is what
degrades.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

import aiohttp

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"

#: Telegram's hard limit on `callback_data`.
_CB_MAX = 64

#: The server-side window `getUpdates` holds the connection open for.
#:
#: THE CLIENT TIMEOUT MUST EXCEED IT. Set to 25s against a 10s client timeout,
#: every poll was killed client-side and retried: measured on the host at
#: 12:05:21, `telegram getUpdates failed: TimeoutError` ten seconds after start,
#: and then a WARNING every ten seconds forever -- a poller that can never
#: receive a button tap, wrapped in the noise this module exists to remove.
_LONG_POLL_S = 25.0

#: Do not re-announce the same CONDITION more often than this. Discrete events
#: (a fill, a settlement) are never deduped -- only `notify_once` keys are.
_ONCE_DEFAULT_S = 600.0

#: Exit no-fills that are CONDITIONS rather than moments. Everything else
#: (`limit_unfilled`, `sell_no_shares`, `fak_unfilled`) resolves on the next
#: tick and is deliberately not alerted on -- see `exit_stuck`.
_STRUCTURAL_EXIT_BLOCKS = frozenset({"sell_below_venue_minimum"})


def _money(x: float) -> str:
    return f"${x:,.2f}" if abs(x) >= 1.0 else f"${x:,.4f}"


def _human_reason(reason: str | None) -> str:
    """Venue/broker failure code -> something readable in a notification."""
    r = (reason or "").strip()
    if not r:
        return "no reason reported"
    table = {
        "no_quote": "the venue returned no quote",
        "quote_above_ceiling": "the quote left the price band",
        "quote_unfillable": "the venue's quote could not be filled",
        "quote_stale": "the quote was stale",
        "quote_price_missing": "the quote carried no price",
        "px_below_floor": "the price was below the entry floor",
        "size_above_cap": "no size both trades and fits the stake cap",
        "limit_unfilled": "the resting limit was never crossed",
        "fak_unfilled": "the market had nothing at or below the ceiling",
        "sell_below_venue_minimum": ("proceeds under the venue's minimum, "
                                     "so it was not sent"),
        "above_fill_cap": "the book was above the fill cap",
        "insufficient_balance": "not enough USDT in the wallet",
        "rejected": "the venue refused the order",
        "venue_failed": "the venue reported the order FAILED",
        "unconfirmed": "the order could not be confirmed at the venue",
        "unknown": "the outcome could not be determined",
    }
    return table.get(r, r.replace("_", " "))


class TelegramNotifier:
    """Fire-and-forget event notifications for one chat."""

    def __init__(self, *, token: str, chat_id: str, session, chart_url: str = "",
                 enabled: bool = True, on_retry=None, queue_max: int = 200,
                 timeout_s: float = 10.0, dry_run: bool = False) -> None:
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.session = session
        self.chart_url = (chart_url or "").strip()
        self.on_retry = on_retry
        self.timeout_s = float(timeout_s)
        self.dry_run = bool(dry_run)
        self.enabled = bool(enabled and self.token and self.chat_id)
        self._q: asyncio.Queue = asyncio.Queue(maxsize=int(queue_max))
        self._tasks: list[asyncio.Task] = []
        self._offset = 0
        self._dropped = 0
        self._sent = 0
        self._failed = 0
        #: decision ids with a RETRY NOW in flight, so a double tap cannot
        #: place a second order for a round we may already be long in.
        self._retry_inflight: set[int] = set()
        #: key -> last sent, for `notify_once`.
        self._once: dict[str, float] = {}

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        """Start the sender and the poller. NEVER AWAITS THE NETWORK.

        The backlog drain used to run here, which put a Telegram timeout (10s,
        or a DNS stall) inside `Bot.setup()` -- i.e. it made the TRADING bot's
        startup depend on api.telegram.org being up. The drain is one
        `getUpdates` with `timeout=0` and now runs as the poller's first act,
        where nothing is waiting on it.
        """
        if not self.enabled:
            return
        self._tasks = [asyncio.create_task(self._sender(), name="tg-send"),
                       asyncio.create_task(self._poller(), name="tg-poll")]

    async def stop(self) -> None:
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []

    # -- public API ---------------------------------------------------------
    def notify(self, text: str, buttons: list[list[dict]] | None = None) -> None:
        """Enqueue a message. NEVER blocks and NEVER raises."""
        if not self.enabled:
            log.debug("telegram disabled; would send: %s", text.splitlines()[0])
            return
        if self.dry_run:
            log.info("telegram (dry-run): %s", text.replace("\n", " | "))
            return
        try:
            self._q.put_nowait((text, buttons))
        except asyncio.QueueFull:
            self._dropped += 1

    def notify_once(self, key: str, text: str, buttons=None,
                    window_s: float = _ONCE_DEFAULT_S) -> None:
        """Like `notify`, but at most once per `window_s` per key.

        For CONDITIONS rather than events -- a position that cannot be sold
        because the proceeds are under the venue minimum will be retried every
        tick, and one notification per tick is the noise this module exists to
        avoid.
        """
        now = time.time()
        last = self._once.get(key)
        if last is not None and now - last < window_s:
            return
        if len(self._once) > 256:                      # bounded: no leak
            for k in [k for k, t in self._once.items() if now - t > window_s]:
                self._once.pop(k, None)
        self._once[key] = now
        self.notify(text, buttons)

    # -- message builders ---------------------------------------------------
    def _keyboard(self, *rows: list[dict]) -> list[list[dict]] | None:
        # A missing button must remove ITS ROW, not leave a null in it. A row
        # of `[None]` is truthy as a list, so a plain `if r` filter kept it and
        # shipped `{"text": null}` to Telegram, which refuses the whole message
        # -- the failure mode being an entry-failure alert that never arrives.
        out: list[list[dict]] = []
        for r in rows:
            row = [b for b in (r or []) if b]
            if row:
                out.append(row)
        return out or None

    def _chart_button(self, slug: str) -> dict | None:
        if not self.chart_url:
            return None
        url = (self.chart_url.replace("{slug}", slug)
               if "{slug}" in self.chart_url else self.chart_url)
        if not url.startswith(("http://", "https://")):
            return None
        return {"text": "📈 Chart", "url": url}

    def _retry_button(self, did: int | None) -> dict | None:
        if did is None or self.on_retry is None:
            return None
        data = f"retry:{int(did)}"
        if len(data) > _CB_MAX:
            return None
        return {"text": "🔁 RETRY NOW (market)", "callback_data": data}

    def entry(self, *, slug: str, side: str, stake: float, px: float,
              shares: float, edge: float | None, ev: float | None,
              tau: float, spot: float | None, strike: float | None,
              arm: str = "model") -> None:
        lines = [f"🟢 ENTRY {side}  ({arm})",
                 slug,
                 f"stake {_money(stake)} → {shares:g} sh @ {px:.4f}"]
        bits = []
        if edge is not None:
            bits.append(f"edge {edge:+.4f}")
        if ev is not None:
            bits.append(f"ev/$ {ev:+.4f}")
        if bits:
            lines.append("  ".join(bits))
        geo = []
        geo.append(f"tau {tau:.0f}s")
        if spot:
            geo.append(f"spot {spot:,.2f}")
        if strike:
            geo.append(f"strike {strike:,.2f}")
        if strike and spot:
            geo.append(f"{(spot - strike) / strike * 1e4:+.1f}bps")
        lines.append("  ".join(geo))
        self.notify("\n".join(lines),
                    self._keyboard([self._chart_button(slug)]))

    def entry_failed(self, *, slug: str, side: str, stake: float,
                     reason: str | None, attempts: int, did: int | None = None,
                     tau: float | None = None, arm: str = "model") -> None:
        """A failed entry, AT MOST ONCE PER ROUND PER SIDE.

        ⛔ THIS MUST BE THROTTLED, AND THE FIRST VERSION WAS NOT. Read off the
        live log, the `-9000 Please ensure your account has enough USDT` window
        of 2026-09-22 produced a terminal `_place_order` failure (three
        attempts, "pausing orders for 30s") roughly ONCE PER SECOND for 45
        seconds across one 15m round: the cooldown deters the next TICK, not the
        loop already inside `_place_order`, and the arm re-decides every poll.
        Unthrottled, that is ~45 messages for one condition -- the exact failure
        this module exists to prevent, and the same repeating-condition shape as
        the ERROR the log filter now collapses to one line per 5 minutes.

        Keyed on slug+side rather than on the reason, because the operator's
        question is "did this round enter?" -- a round that failed three
        different ways is still one round that did not enter. The RETRY button
        therefore points at the FIRST attempt, which is the one whose stake and
        side the message quotes.
        """
        lines = [f"⚠️ ENTRY FAILED {side}  ({arm})",
                 slug,
                 f"reason: {_human_reason(reason)}"]
        if reason:
            lines.append(f"code: {reason}")
        tail = [f"stake {_money(stake)}", f"{attempts} attempt(s)"]
        if tau is not None:
            tail.append(f"tau {tau:.0f}s")
        lines.append("  ".join(tail))
        if self._retry_button(did) is not None:
            lines.append("↳ RETRY NOW re-places this side as a MARKET order")
        self.notify_once(f"entryfail:{slug}:{side}", "\n".join(lines),
                         self._keyboard([self._retry_button(did)],
                                        [self._chart_button(slug)]))

    def exit(self, *, slug: str, side: str, shares: float, px: float,
             proceeds: float, pnl: float, tau: float, rule: str,
             running: str = "", partial: bool = False,
             frac: float | None = None) -> None:
        head = "🔵 EXIT" if pnl >= 0 else "🔴 EXIT"
        when = "" if not partial else (f"  (partial "
                                       f"{frac * 100:.0f}%)" if frac else "  (partial)")
        lines = [f"{head} {rule}{when}",
                 slug,
                 f"sold {shares:g} sh @ {px:.4f}",
                 f"proceeds {proceeds:+.4f}   pnl {pnl:+.4f}",
                 f"tau {tau:.0f}s"]
        if running:
            lines.append(f"running: {running}")
        self.notify("\n".join(lines),
                    self._keyboard([self._chart_button(slug)]))

    def settled(self, *, slug: str, side: str, stake: float, payout: float,
                pnl: float, won: bool, bankroll: float | None = None) -> None:
        head = "✅ WIN" if won else "❌ LOSS"
        lines = [f"{head}  {side}",
                 slug,
                 f"stake {_money(stake)} → payout {_money(payout)}",
                 f"pnl {pnl:+.4f}"]
        if bankroll is not None:
            lines.append(f"bankroll {_money(bankroll)}")
        self.notify("\n".join(lines),
                    self._keyboard([self._chart_button(slug)]))

    def exit_stuck(self, *, slug: str, side: str, shares: float,
                   reason: str | None) -> None:
        """A position that CANNOT be sold, deduped per slug.

        NARROW ON PURPOSE. The sell is retried every tick, so this is called on
        every no-fill -- and most no-fills are MOMENTS, not conditions:
        `limit_unfilled` and `sell_no_shares` both resolve on the next tick, and
        alerting on them would train the operator to ignore `CANNOT SELL`. The
        one that cannot resolve itself is the venue's minimum NOTIONAL refusing
        to buy a near-worthless position at all -- the `unsellable` column in
        the exit research, and the reason a closed position stays open.
        """
        if (reason or "") not in _STRUCTURAL_EXIT_BLOCKS:
            return
        self.notify_once(
            f"stuck:{slug}",
            "\n".join([f"🚫 CANNOT SELL  {side}", slug,
                       f"holding {shares:g} sh",
                       f"reason: {_human_reason(reason)}",
                       "→ retrying every tick"]),
            self._keyboard([self._chart_button(slug)]))

    def info(self, text: str) -> None:
        self.notify(text)

    # -- transport ----------------------------------------------------------
    async def _post(self, method: str, payload: dict) -> dict | None:
        url = _API.format(token=self.token, method=method)
        try:
            async with self.session.post(
                url, json=payload,
                timeout=self._timeout(method),
            ) as r:
                body = await r.json(content_type=None)
                if r.status == 200:
                    return body
                if r.status == 409 and method == "getUpdates":
                    # EXPECTED AFTER A RESTART, and not an error: the previous
                    # process's long-poll can still be open for its 25s window
                    # and Telegram terminates the new one ("Conflict:
                    # terminated by other getUpdates request"). Measured on the
                    # host during a test run. It is also the reason two
                    # instances must never share one bot token -- they would
                    # steal each other's button taps.
                    log.debug("telegram getUpdates: another poller is active "
                              "(409); retrying")
                    return {"_status": 409, "ok": False}
                log.warning("telegram %s -> HTTP %d: %s", method, r.status,
                            str(body)[:200])
                return {"_status": r.status, **({"ok": False} if isinstance(body, dict) else {})}
        except Exception as e:                                   # noqa: BLE001
            log.warning("telegram %s failed: %s: %s", method,
                        type(e).__name__, e)
            return None

    def _timeout(self, method: str) -> aiohttp.ClientTimeout:
        if method == "getUpdates":
            # Longer than the server-side hold, or the poll is a guaranteed
            # client-side timeout -- see `_LONG_POLL_S`.
            return aiohttp.ClientTimeout(total=_LONG_POLL_S + 15.0,
                                         sock_read=_LONG_POLL_S + 15.0)
        return aiohttp.ClientTimeout(total=self.timeout_s)

    async def _deliver(self, text: str, buttons) -> None:
        """Send one message. Retries with backoff; degrades buttons on a 400."""
        for attempt in range(3):
            payload = {"chat_id": self.chat_id, "text": text[:4000],
                       "disable_web_page_preview": True}
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            r = await self._post("sendMessage", payload)
            if r is None:                       # transport: back off and retry
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            if r.get("ok"):
                self._sent += 1
                return
            # A 400 from a BUTTON must not cost the message: drop the keyboard
            # and send the text alone. Telegram refuses the whole call for a
            # malformed URL button, which would otherwise make one wrong
            # `telegram_chart_url` silently eat every notification.
            if buttons and r.get("_status") == 400:
                log.warning("telegram: sending without buttons (the keyboard "
                            "was refused)")
                buttons = None
                continue
            if r.get("_status") == 429:         # rate limited
                await asyncio.sleep(3.0 * (attempt + 1))
                continue
            self._failed += 1
            log.warning("telegram refused the message: %s", str(r)[:200])
            return
        self._failed += 1

    async def _sender(self) -> None:
        while True:
            text, buttons = await self._q.get()
            try:
                await self._deliver(text, buttons)
            except asyncio.CancelledError:
                raise
            except Exception as e:                               # noqa: BLE001
                log.warning("telegram sender: %s: %s", type(e).__name__, e)
            finally:
                self._q.task_done()
            if self._dropped:
                n, self._dropped = self._dropped, 0
                log.warning("telegram: %d message(s) dropped (queue full)", n)

    # -- inbound ------------------------------------------------------------
    async def _drain_backlog(self) -> None:
        """Skip updates that predate this process.

        A restart must not replay taps from a previous run: the offset is a
        cursor into Telegram's queue, and starting at 0 would re-deliver every
        callback the chat ever sent.
        """
        r = await self._post("getUpdates", {"timeout": 0, "limit": 100,
                                            "allowed_updates": ["callback_query"]})
        if isinstance(r, dict) and r.get("ok"):
            for u in r.get("result") or []:
                self._offset = max(self._offset, int(u.get("update_id", 0)) + 1)

    async def _poller(self) -> None:
        # DRAIN FIRST, in the task rather than in `start()` -- see `start()`.
        # Skipping the updates that predate this process is what stops a
        # restart replaying yesterday's taps.
        with contextlib.suppress(Exception):
            await self._drain_backlog()
        while True:
            try:
                r = await self._post("getUpdates", {
                    "offset": self._offset, "timeout": int(_LONG_POLL_S),
                    "allowed_updates": ["callback_query"]})
                if not isinstance(r, dict) or not r.get("ok"):
                    await asyncio.sleep(5.0)
                    continue
                for u in r.get("result") or []:
                    self._offset = max(self._offset,
                                       int(u.get("update_id", 0)) + 1)
                    cq = u.get("callback_query")
                    if cq:
                        await self._handle_callback(cq)
            except asyncio.CancelledError:
                raise
            except Exception as e:                               # noqa: BLE001
                log.warning("telegram poller: %s: %s", type(e).__name__, e)
                await asyncio.sleep(5.0)

    async def _handle_callback(self, cq: dict) -> None:
        data = str(cq.get("data") or "")
        cq_id = cq.get("id")
        if not data.startswith("retry:"):
            await self._post("answerCallbackQuery",
                             {"callback_query_id": cq_id, "text": "unsupported"})
            return
        try:
            did = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await self._post("answerCallbackQuery",
                             {"callback_query_id": cq_id, "text": "bad button"})
            return
        # SINGLE-FLIGHT. A double tap must not place two market orders for the
        # same round -- `broker.buy` has no way to know this is the same
        # decision, and the venue would happily sell us the round twice.
        if did in self._retry_inflight:
            await self._post("answerCallbackQuery",
                             {"callback_query_id": cq_id,
                              "text": "already retrying…"})
            return
        self._retry_inflight.add(did)
        # Answer FIRST: the button spinner stops even if the retry is slow.
        await self._post("answerCallbackQuery",
                         {"callback_query_id": cq_id, "text": "retrying…"})
        try:
            result = "retry handler is not wired"
            if self.on_retry is not None:
                result = await self.on_retry(did)
            self.notify(f"🔁 RETRY {did}: {result}")
        except Exception as e:                                   # noqa: BLE001
            log.warning("telegram retry %s failed: %s: %s", did,
                        type(e).__name__, e)
            self.notify(f"🔁 RETRY {did}: failed ({type(e).__name__}: {e})")
        finally:
            self._retry_inflight.discard(did)

    # -- diagnostics --------------------------------------------------------
    def stats(self) -> dict:
        return {"enabled": self.enabled, "sent": self._sent,
                "failed": self._failed, "dropped": self._dropped,
                "queued": self._q.qsize()}

    def _debug_json(self, payload: dict) -> str:
        return json.dumps(payload)
