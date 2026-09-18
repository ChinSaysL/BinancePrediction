"""Polymarket order placement, fills and settlement.

Implements `btcpredict.execution.base.Broker`, so an arm can be pointed at
either venue without a second code path. What is DIFFERENT here is not the
interface, it is the cost model, and every constant below was measured rather
than assumed.

THE MEASURED LATENCY BREAKDOWN (2026-09-16, Contabo Tokyo -> Cloudflare NRT)
--------------------------------------------------------------------------
    edge-only request (never reaches the origin)          22-29 ms
    ANY clob.polymarket.com API call                     260-283 ms
    ...identical from Marseille (205-304), so it is not our box
    ...identical on IPv4 (262) and IPv6 (255)
    ...identical warm or cold (258 -> 237 steady)
    create_limit_order  (local signing)    504 ms cold -> 4.8 ms warm
    post_order                             275 ms
    place_limit_order (sign+post in one)   797 ms   <- it re-signs internally

So the order API has an irreducible ~250 ms cost per call, and the number of
calls is the only thing under our control. Four rules follow, and this module
exists mostly to enforce them:

  RULE 1 -- PRIME THE SIGNER. The first `create_limit_order` in a process pays
  ~500 ms for lazy initialisation. That must be spent at boot (see `prime()`),
  never on the hot path.

  RULE 2 -- NO PER-ORDER DISCOVERY. Token ids, tick and min size are fetched
  once per round and cached by slug (`round_for`). Reusing the round is also
  what keeps RULE 1 spent.

  RULE 3 -- NO BALANCE READ ON THE HOT PATH. `balance()` is a 260 ms call.
  Cache it, refresh it on demand or out of band.

  RULE 4 -- SIGN AND POST SEPARATELY. `place_limit_order` costs 797 ms because
  it signs internally; `create_limit_order` + `post_order` costs 4.8 + 275 =
  ~280 ms for the same order. Use the two-step form.

WHAT A FILL ACTUALLY IS (measured on the live account, 5 shares @ 0.38)
----------------------------------------------------------------------
* `post_order` returns 200 and an order id BEFORE the outcome is known, so an
  ack is NOT a fill. This module never reports a fill from the ack alone.
* **A cancel ack is not proof of no fill either.** A post-only bid 5 ticks
  under the touch filled anyway, in the same second: `cancel_order` came back
  `"order can't be found - already canceled or matched"` while collateral had
  already dropped by exactly `5 x 0.38`. `post_only` stops us CROSSING; it does
  not stop a taker crossing into us. Reconciliation is by trade/position read,
  never by cancel result.
* The only trustworthy post-trade evidence is the account: a position appeared,
  or a trade record with `tx_hash` and a real matched amount.
* **AN UNREADABLE READBACK IS NOT AN UNFILLED ORDER.** Both list endpoints
  return LAZY PAGINATORS, so the HTTP call happens on ITERATION -- a guard
  around `list_account_trades()` protects nothing, and a malformed response
  raises out of `_trades_since` instead of returning its documented None. Live
  2026-09-17 19:09, `UnexpectedResponseError: account trades response items
  malformed` escaped all the way to the arm's order path, which logged
  `cross order failed ... not retrying` and wrote the order off -- while the
  venue had matched it, settled it our way and paid $3.13. Nothing was booked:
  no fill, no settlement, a released round slot, and the exposure outside every
  cap the ledger enforces. Reads must therefore be exception-proof, and
  `settle()` confirms an unverified fill from the POSITION LIST when the trade
  file will not answer.

FEES. The market metadata says `feeType crypto_fees_v2`,
`feeSchedule {rate 0.07, exponent 1, takerOnly true}` -- i.e. `0.07*p*(1-p)`
per share, taker pays, maker pays zero. The ONE live trade measured paid
`$0.00` and was a MAKER fill, so it does not confirm or refute the schedule.
`fee_rate_bps=0` on that record is the taker's rate, not the maker's. This
module therefore CHARGES the schedule analytically and keeps the venue's own
number when it supplies one, so the two can be compared on real fills.

SETTLEMENT IS ~58 SECONDS AFTER THE BELL, and that is a venue property, not a
bug. Measured on round btc-updown-5m-1789577400: bell at 16:55:00, position
`redeemable` at 16:55:57.6 (+57.6 s), redeem at 16:56:08 (+68 s). The terminal
variable is a 60 s Chainlink TWAP that does not exist until the round ends.
`settle()` is therefore bookkeeping against a winner read from
`markets.settled_winner_strict` (which requires `umaResolutionStatus ==
"resolved"`), not an on-chain action: Polymarket's AutoRedeemer sweeps winning
positions by itself, gasless, about 10 s after they become redeemable.
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from btcpredict.execution.base import Fill, Settlement
from btcpredict.venues.polymarket import markets as M
from btcpredict.venues.polymarket.book import PmBook, Touch

#: Taker fee per share, from the market's own `feeSchedule`: `rate * p * (1-p)`,
#: `feeType crypto_fees_v2, rate 0.07`. MAKERS PAY 0 (`takerOnly true`) -- which
#: is why "avoid the fee" is NOT an argument for taking liquidity with FAK. The
#: argument for FAK is adverse selection, and the fee is what it gives up.
TAKER_FEE_RATE = 0.07

#: The venue's own defaults, used before the market's metadata arrives.
DEFAULT_TICK = 0.01
DEFAULT_MIN_SHARES = 5.0
#: `invalid amount for a marketable BUY order ($0.05), min size: $1` -- measured.
MIN_NOTIONAL = 1.0

#: The lowest price an ENTRY may be quoted at, on the ASK or on the LIMIT.
#:
#: MEASURED FAILURE, 2026-09-16 17:29:42 UTC, `btc-updown-5m-1789579500`. A
#: degenerate fair with no usable quote made `take_limit_px` return its
#: `max(0.01, ...)` floor. `shares = ceil(stake / limit)` then turned the $5.00
#: stake into a **500-share order at 0.01**, and the venue filled it:
#: `size 500 @ 0.01`, `entry_cost_usdc 5.0`, `entry_fees_usdc 0.3465` -- and
#: 0.07*0.01*0.99*500 = 0.3465 exactly, so the order is identified beyond doubt.
#: The round resolved Up and the whole stake was lost.
#:
#: TWO things are wrong with a price this low and they compound:
#: 1. It is not a valuation. It says the other side is worth 95c+, a region no
#:    measured cell covers (cross 0.45-0.90, train 0.66-0.89).
#: 2. The SIZE is derived from it (`ceil(stake / limit)`), so the error is
#:    multiplied by the stake rather than bounded by it: at 0.01 the share count
#:    is 100x what it is at 1.00. `shares * limit >= MIN_NOTIONAL` cannot catch
#:    it, because that product is `stake` by construction at any price.
#:
#: The arms' own band (`cross_min_price`, 0.10) does NOT protect this: it is
#: checked against the polled ASK, so it never inspected the price we POSTED.
#: This floor is the last gate before the venue and it checks both.
DEFAULT_MIN_ENTRY_PX = 0.05


def _as_logger(log: Any) -> Callable[[str], None]:
    """Normalise the `log` argument to something definitely CALLABLE.

    Found live 2026-09-17: `main.py` passes `log` = `logging.getLogger(
    "btcpredict")`, and a `logging.Logger` is NOT callable -- `self._log("msg")`
    raises `TypeError: 'Logger' object is not callable`. Every one of this
    module's ~12 error lines was therefore dead, which is why 90 failed warms
    inside a minute left nothing in the log at all. Inside a task
    (`asyncio.to_thread`) the TypeError vanished without a trace.

    LOGGING MUST NEVER BE THE THING THAT BREAKS: a lost log line is bad, a
    raise from a diagnostics call on the order path is worse. So accept a
    Logger, a plain callable, or None -- and never fail.
    """
    if log is None:
        return lambda _m: None
    if callable(log):
        return log
    info = getattr(log, "info", None)                # logging.Logger
    if callable(info):
        return info
    return lambda _m: None


def _below_entry_floor(px: float, floor: float) -> bool:
    """True when `px` is below the lowest price an entry may be quoted at.

    Kept as one predicate so the taker plan and the resting path cannot drift
    apart; a price floor enforced in one of two order paths is not a floor.
    """
    return not (px >= floor)      # NaN-safe: a NaN price is not >= anything


def _size_above_cap(shares: float, limit: float, cap: float) -> bool:
    """True when the SMALLEST order the venue accepts costs more than `cap`.

    MEASURED, 2026-09-16. `shares = max(r.min_order or 5, ceil(stake/limit))`
    takes the MAX, so the venue's 5-SHARE minimum beats the stake cap and the
    notional is `5 x limit`, not `cap`:

        stake_cap_usdt = 1.00      limit 0.90 -> 5 shares -> **$4.50**
        stake_cap_usdt = 1.00      limit 0.99 -> 5 shares -> **$4.95**

    The cap IS applied to `stake` (main.py `min(stake, stake_cap_usdt)`), and
    every ledger row records that $1.00 -- so the money actually at risk was
    understated by 4.5-5x with nothing in the record to show it. `MIN_NOTIONAL`
    cannot catch it: `shares * limit >= stake` by construction, so the floor
    passes at any price. This is the same family as the 500-share order of
    2026-09-16 17:29 (a price error multiplied into the SIZE) and it is silent
    for the same reason.

    THE CONVENTION IS ALREADY IN THE REPO. `stake_floor_to_minimum` handles the
    mirror case for the DOLLAR minimum -- "False skips the trade and True
    stakes the minimum instead. Off by default because rounding up raises the
    real fraction." The venue's SHARE minimum had no such guard, so it rounded
    up unconditionally. `cap <= 0` means NO CAP (the repo's convention for every
    other limit) and this whole check is off.

    Below $0.20 the 5-share minimum costs less than $1.00 and this is inert;
    the cross arm's band is 0.10-0.90 and its measured fills cluster 0.50-0.90,
    so in practice it refuses the arm's whole population at a $1 cap.
    """
    return cap > 0 and shares * limit > cap + 1e-9


def _shares_within_cap(shares: float, limit: float, cap: float,
                       min_shares: float) -> float | None:
    """The largest whole share count that fits the cap, or None if none does.

    WHY THIS IS NOT `_size_above_cap`. That predicate answers "does the size we
    chose exceed the cap"; refusing on True throws away orders that a LEGAL
    size existed for. The size is `max(min_order, ceil(stake/limit))` and
    `ceil` overshoots the stake by up to one share -- the overshoot is what the
    cap sees:

        cap 5.00, limit 0.24 -> ceil(5/0.24)=21 -> $5.04 > cap
                               but 20 shares cost $4.80, which fits
        cap 2.50, limit 0.90 -> the venue's 5 shares cost $4.50 > cap, and 2
                               shares cost $1.80 but 2 is below its minimum,
                               so THIS one really is impossible and must refuse

    MEASURED 2026-09-17: 25 of 25 order attempts over 5 hours were refused by
    the old predicate, every one of them `NO FILL in 0.00s` -- the order never
    reached the venue. A refusal is only honest when NO count satisfies all
    three of: at least the venue's minimum shares, at least `MIN_NOTIONAL`,
    and at most the cap. Otherwise the order should SHRINK to fit -- which is
    what a cap is for, and strictly less risk than the size it replaces.

    ⚠️ AT `cap == MIN_NOTIONAL` (both 1.00 in this repo) THERE IS NOTHING TO
    FIT AT ANY PRICE ABOVE $0.20. The notional must be at least $1.00 and at
    most $1.00, so it must be EXACTLY $1.00, which only prices that divide
    $1.00 can express. The live effect was an arm that ordered 27 times in 5
    hours and filled none, with `size_above_cap` as the whole reason. Raising
    the cap is the only lever; this helper makes the cap behave like a cap
    rather than an on/off switch once it exceeds the floor.

    `cap <= 0` is NO CAP (the repo's convention), so there is nothing to fit.
    """
    if cap <= 0 or limit <= 0:
        return shares
    fit = int(math.floor(cap / limit + 1e-9))
    if fit >= min_shares and fit * limit >= MIN_NOTIONAL:
        return float(fit)
    return None


def _records(res: Any) -> list[Any]:
    """Flatten whatever a list endpoint returned into a list of RECORDS.

    ⛔ THE SDK RETURNS A **PAGINATOR**, AND ITERATING ONE YIELDS **PAGES**, NOT
    RECORDS. This cost real money on 2026-09-16 and it cost it twice:

    * `_trades_since` iterated the object that `list_account_trades` returned,
      directly. Every element was a `Page`, so `_a(tr, "asset_id")` resolved to
      `""`, no trade ever matched the token, `seen` stayed False and it returned
      **None**. `_confirm` reads None as "the venue will not say", so **six real
      fills were never booked** (the DB still showed 861 fills / max id 865, all
      Binance-era, while the wallet had traded 5 Up @0.38, 500 Down @0.01 and
      4 x 5 Down @0.01).
    * `active_orders` wrapped the same value and emitted **one all-blank row**,
      so resting exposure was unreadable and `cancel_orders([""])` could not
      cancel anything.

    Both were silent: a Page is a valid object, every attribute access on it is
    simply absent, and the default hides it. `_a`-style probing cannot detect
    this -- the shape has to be unwrapped.
    """
    if res is None:
        return []
    if isinstance(res, (list, tuple)):
        return list(res)
    items = getattr(res, "items", None)
    if items is not None and not callable(items):
        return list(items)                    # a Page
    # A Paginator: flatten EVERY page -- returning only the first would
    # silently truncate a multi-page list. Bounded so a bad cursor cannot spin.
    out: list[Any] = []
    try:
        for i, page in enumerate(res):
            if i >= 20:
                break
            out.extend(_records(page))
        return out
    except TypeError:
        pass
    first = getattr(res, "first_page", None)
    if callable(first):
        try:
            return _records(first())
        except Exception:                                     # noqa: BLE001
            return []
    return out


def ev_ceiling(p_win: float, margin: float = 0.0,
               fee_rate: float = TAKER_FEE_RATE) -> float:
    """The most we may pay: `P(win | signal) - fee - target margin`.

    WHY NOT `ask + offset`. A static offset spends whatever the ask happens to
    be, so it pays a different edge at every price. This spends a fixed EDGE
    and lets the price vary, which is the same bound stated in the unit the arm
    actually earns in -- and it is the number the sweep produces directly
    (`be-ask = win_rate - fee`; at the live train setting, thr 15 bps, that is
    0.898).

    THE FEE IS INSIDE THE UNKNOWN, so this is solved rather than approximated:
    the fee is `k*x*(1-x)` at the very price `x` we are solving for, which
    makes the identity `x = w - k*x*(1-x) - m` a quadratic in x:

        k*x^2 - (1+k)*x + (w - m) = 0
        x = [ (1+k) - sqrt((1+k)^2 - 4k(w-m)) ] / 2k     (the lower root)

    The lower root is the conservative one: the upper root is the far side of
    the fee parabola, where the same fee is charged on a price we would never
    pay. A fee-free call (`fee_rate=0`) degenerates to `w - m`, which is the
    naive form, so the two agree where they should.

    Returns 0.0 when the edge cannot cover the fee and the margin at any
    price -- a signal not worth trading at all, not a price of zero.
    """
    if fee_rate <= 0:
        return max(0.0, p_win - margin)
    k = float(fee_rate)
    b = p_win - margin
    disc = (1.0 + k) ** 2 - 4.0 * k * max(0.0, b)
    if disc <= 0 or b <= 0:
        return 0.0
    x = ((1.0 + k) - math.sqrt(disc)) / (2.0 * k)
    return max(0.0, x)


def entry_ceiling(static_max: float, p_win: float, margin: float,
                  fee_rate: float = TAKER_FEE_RATE) -> float:
    """A static price gate TIGHTENED by the EV ceiling for a MEASURED win rate.

    WHY THIS AND NOT JUST A NUMBER. `cross_train_max_ask` is hand-picked, and a
    hand-picked price cannot follow the thing it is really bounding. `p_win` is
    measured (`tools/pm_train_sweep.py`), the fee is charged AT the price being
    solved for, and the gate becomes the arithmetic of the pair instead of a
    remembered constant. At the live measurement (p_win 0.914, margin 0.03) the
    ceiling is **0.876** against a static 0.890 -- 1.4c tighter -- and it falls
    on its own if the win rate falls.

    IT CAN ONLY TIGHTEN. `min(static, ceiling)`, so enabling this can never
    loosen a gate that was already refusing; the static value stays as a
    backstop if the measurement is ever withdrawn.

    FAILS TO THE STATIC VALUE when `p_win` is not a probability (<=0 or >=1) or
    when the pair cannot cover the fee at any price (`ev_ceiling` -> 0.0). It
    deliberately does NOT return 0.0 in that case: 0 is this repo's convention
    for "gate OFF", so returning it would OPEN the gate exactly when the
    measurement says there is no edge. An unprofitable p_win is a decision to
    turn the arm off, not something to infer from a default.
    """
    if not (0.0 < p_win < 1.0):
        return static_max
    ceil_ev = ev_ceiling(p_win, margin, fee_rate)
    if ceil_ev <= 0.0:
        return static_max
    return min(static_max, ceil_ev) if static_max > 0 else ceil_ev


def _round_to_tick(px: float, tick: float, up: bool) -> float:
    """Snap to the venue's grid. BUY ceilings go UP, floors go DOWN.

    Ordering matters: rounding a ceiling DOWN can put the limit below the ask
    we are trying to cross, which turns a fillable order into a resting one.
    """
    if tick <= 0:
        return px
    n = px / tick
    k = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(k * tick, 10)


def taker_fee(px: float, shares: float, rate: float = TAKER_FEE_RATE) -> float:
    """`rate * p * (1-p)` per share, in dollars."""
    return max(0.0, rate) * px * (1.0 - px) * shares


@dataclass(slots=True)
class PmBrokerConfig:
    """Knobs. Every default is either the venue's own or a measured bound."""

    #: The only family we trade. The slug builder can produce nothing else.
    series: str = M.SERIES
    #: How stale a book may be before we refuse to price off it. Fails CLOSED:
    #: a frozen feed that prices every order off a dead book is worse than no
    #: price at all.
    max_book_age_s: float = 2.0
    #: How far above the ask we will reach. PM's tick is 0.01, so this is
    #: really "one or two ticks of slack"; the ceiling (`max_px`) is what
    #: actually bounds us.
    slippage: float = 0.02
    #: Hard cap on the number of sequential venue calls per order. 2 = sign +
    #: post. Anything more means discovery has leaked onto the hot path.
    max_venue_calls: int = 2
    #: Charge the analytic taker fee when the venue does not report one.
    charge_fee_when_unreported: bool = True
    taker_fee_rate: float = TAKER_FEE_RATE
    #: Seconds to wait for the venue's own record of the fill before marking
    #: the Fill `unverified`. The ack alone is never enough (see module docs).
    confirm_budget_s: float = 2.5
    #: How the MARKET path takes liquidity.
    #:   "limit" - sign locally, post, let the remainder REST (261 ms median)
    #:   "fak"   - Fill-And-Kill == IOC, atomic, remainder KILLED (414 ms)
    #: Defaults to "limit" because the measured cost of FAK is +153 ms against
    #: a horizon that is already the binding constraint (250 ms profitable at
    #: every cap, 500 ms negative at every cap). FAK removes the adverse
    #: selection of a resting fill; it is a trade, not a free upgrade, so it is
    #: opt-in and must be A/B'd on its own fills.
    order_type: str = "limit"
    #: Lowest price an ENTRY may be quoted at, on the ask or on the limit. See
    #: `DEFAULT_MIN_ENTRY_PX` for the measured fill that put this here.
    #: The most the SMALLEST order the venue accepts may cost. 0 = off (the
    #: repo's convention for every other limit). See `_size_above_cap`: the
    #: venue's 5-share minimum turns a $1.00 stake into a $4.50 position at
    #: 0.90, and nothing else in the stack sees that number.
    stake_cap_usdt: float = 0.0
    min_entry_px: float = DEFAULT_MIN_ENTRY_PX


@dataclass
class _RoundCache:
    """Token ids and venue constants, resolved once per round (RULE 2)."""

    by_slug: dict[str, M.PmRound] = field(default_factory=dict)

    def get(self, slug: str, fetch: Callable[[float], M.PmRound | None]
            ) -> M.PmRound | None:
        r = self.by_slug.get(slug)
        if r is not None:
            return r
        try:
            ts = float(slug.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return None
        r = fetch(ts)
        if r is not None:
            self.by_slug[slug] = r
            # The family is fixed at construction; a fetched round that is a
            # different series means the slug was not ours.
        return r


def _a(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute among `names`. The SDK renames fields between
    minor versions and a missing side-field must not raise mid-order."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _ts_epoch(x: Any) -> float | None:
    """A venue timestamp as epoch seconds, or None.

    The SDK mixes epoch-seconds, epoch-milliseconds and ISO strings across
    models, and comparing them wrongly SILENTLY drops real fills.
    """
    if x is None:
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v / 1000.0 if v > 1e11 else v
    s = str(x).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


class PmBroker:
    """Order placement against the Polymarket CLOB.

    `client` is injected so the whole module is testable without the SDK and
    without a key. Pass `client=None` and the real `SecureClient` is built
    lazily from the environment (which also keeps import cheap for the paper
    broker and for tests).
    """

    mode = "live"

    def __init__(self, *, book: PmBook, client: Any = None,
                 cfg: PmBrokerConfig | None = None,
                 log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] | None = None,
                 creds: dict | None = None) -> None:
        self.cfg = cfg or PmBrokerConfig()
        self.book = book
        self._client = client
        # Credentials are INJECTED, not read from `os.environ` here. Under
        # systemd the `.env` names are never exported, so an environ read
        # reports "missing" for a correctly configured bot (that produced a
        # crash-restart loop). One source of truth: `registry.pm_creds`.
        self.creds = creds or {}
        self._log = _as_logger(log)
        self._now = clock or time.time
        self._rounds = _RoundCache()
        self._balance_cache: float | None = None
        #: token -> `self._now()` of the last successful warm (see `warm`). A
        #: token IS a round side, so without this every order is a cold token
        #: and pays two GETs before its POST; the timestamps expire the memo
        #: because the SDK's market cache only lives 600 s.
        self._warm: dict[str, float] = {}
        self._warm_warned = False
        self.warm_calls = 0
        #: Serialises EVERY use of the SDK client across threads. The order
        #: path now runs on a worker thread (`buy_fak` -> `_buy_fak_sync`) and
        #: the background warm queue already runs on one, so two threads can
        #: otherwise mutate/read the SDK's `order_metadata` cache at once.
        #: RLock, not Lock: `_buy_fak_body` re-enters through `warm_now`.
        self._client_lock = threading.RLock()
        self._primed = False
        self.venue_calls = 0          # per-order hot-path call counter
        self.unverified = 0
        # Why the last `buy` returned None, so the caller can tell a
        # safe-to-retry rejection from an unknown outcome. Mirrors
        # `LiveBroker.last_failure` / `.last_order_error`.
        self.last_failure: str | None = None
        self.last_order_error: str | None = None
        self.wallet_address = str(self.creds.get("wallet") or "")

    @property
    def enabled(self) -> bool:
        """False only when we KNOW the credentials are missing.

        Unlike the Binance broker this cannot require a wallet id: the SDK
        resolves the Deposit Wallet from the signer, so a blank funder is a
        valid configuration (it is a CREATE2 derivation, not a lookup).
        """
        if self._client is not None:
            return True
        c = self.creds
        return bool(c.get("private_key") and c.get("key"))

    # -- client ------------------------------------------------------------
    def _ensure_client(self) -> Any:
        if self._client is None:
            from polymarket import (ApiKeyCreds, RelayerApiKey,  # noqa: PLC0415
                                    SecureClient)
            import os
            c = self.creds

            def need(name: str, var: str) -> str:
                v = str(c.get(name) or os.environ.get(var, "") or "").strip()
                if not v:
                    raise RuntimeError(f"missing {var}")
                return v

            self._client = SecureClient.create(
                private_key=need("private_key", "POLYMARKET_PRIVATE_KEY"),
                wallet=(c.get("wallet")
                        or os.environ.get("POLYMARKET_FUNDER_ADDRESS") or None),
                credentials=ApiKeyCreds(
                    key=need("key", "POLYMARKET_API_KEY"),
                    secret=need("secret", "POLYMARKET_API_SECRET"),
                    passphrase=need("passphrase", "POLYMARKET_PASSPHRASE"),
                ),
                api_key=RelayerApiKey(
                    key=str(c.get("relayer_key")
                            or os.environ.get("POLYMARKET_RELAYER_API", "")),
                    address=str(c.get("relayer_address")
                                or os.environ.get(
                                    "POLYMARKET_RELAYER_ADDRESS", "")),
                ),
            )
        return self._client

    def prime(self) -> float:
        """Pay the ~500 ms cold signing cost now, at boot. RULE 1.

        Returns the milliseconds spent so the caller can log it. Idempotent:
        the second call is the warm ~5 ms.
        """
        c = self._ensure_client()
        t0 = time.perf_counter()
        try:
            c.create_limit_order(token_id="0" * 32, price=0.01, size=5.0,
                                 side="BUY", post_only=False)
        except Exception:                                    # noqa: BLE001
            # A rejection still proves the signer ran. The cost is the point.
            pass
        self._primed = True
        return (time.perf_counter() - t0) * 1000.0

    # -- discovery (cold path) --------------------------------------------
    def round_for(self, slug: str) -> M.PmRound | None:
        return self._rounds.get(slug, M.fetch_round)

    def remember(self, r: M.PmRound) -> None:
        """Cache a round the caller already discovered.

        The bot discovers the current AND next round once per roll and hands
        them here, so the order path never pays for discovery (RULE 2).

        IT MUST BE A `markets.PmRound`. There are TWO classes called `Round` in
        this codebase -- `markets.PmRound` (the venue's view: token ids, tick,
        min order) and `feeds.market_feed.Round` (the arms' view: outcomes,
        strike, tau). They share a name and nothing else, and `plan()` calls
        `.token(side)` on whatever is in this cache. Storing the wrong one used
        to surface much later as `'Round' object has no attribute 'token'`,
        inside the order path, on a live round.
        """
        if not hasattr(r, "token"):
            raise TypeError(
                f"remember() needs a markets.PmRound, got {type(r).__name__}. "
                "feeds.market_feed.Round is a DIFFERENT type with the same "
                "name; the arms consume that one, this cache holds the "
                "venue's.")
        self._rounds.by_slug[r.slug] = r

    # -- balance (cold path) ----------------------------------------------
    def _balance(self, *, refresh: bool = False) -> float:
        """Collateral in dollars, cached. A 260 ms call, so RULE 3."""
        if self._balance_cache is not None and not refresh:
            return self._balance_cache
        c = self._ensure_client()
        bal = 0.0
        try:
            b = c.get_balance_allowance(asset_type="COLLATERAL")
            raw = getattr(b, "balance", b)
            bal = float(raw) / 1e6      # pUSD has 6 decimals
        except Exception as exc:                             # noqa: BLE001
            self._log(f"pm balance read failed: {type(exc).__name__} {exc}")
            if self._balance_cache is not None:
                return self._balance_cache
        self._balance_cache = bal
        return bal

    async def balance(self, *, refresh: bool = False) -> float:
        """`Broker` protocol shape. Served from the cache unless `refresh`."""
        return self._balance(refresh=refresh)

    async def equity(self, *, refresh: bool = False) -> float:
        """Total account value.

        On Polymarket the collateral IS the equity: a position is worth
        nothing until the round settles, and the venue does not mark open
        positions into a separate cash balance the way a perpetual does. So
        this deliberately equals `balance()` and does NOT add open exposure --
        adding it would double-count money already spent.

        ⛔ `refresh=True` IS WHAT MAKES A DEPOSIT VISIBLE. `_balance_cache` is
        invalidated only by SPENDING money -- `buy`, `buy_fak`, `buy_limit`,
        `settle` and `redeem` each set it to None -- so an IDLE bot re-reads
        nothing, and the cached figure stands until something trades or
        settles. MEASURED 2026-09-17: the collateral went $5.5243 -> $10.5243 (a
        real $5.00 deposit, confirmed on-chain) and the bot kept sizing off
        **$5.5243** until a restart, because no order and no settlement had
        happened to invalidate the cache. That is not cosmetic: the exposure cap
        is a FRACTION of this number, so at 0.30 * 5.52 = $1.66 against a $0.98
        open position + a $1.00 stake, every crossing was refused and the arm
        was locked out of its own deposit. `Bot.refresh_bankroll_if_stale` is
        the clock that now forces the read.

        The DEFAULT stays cached: RULE 3 keeps a 260 ms venue call off the hot
        path. Only the periodic reconciler asks for a fresh one.
        """
        return self._balance(refresh=refresh)

    # -- pricing (no venue call) ------------------------------------------
    #: Re-warm after this many seconds. The SDK's own market cache TTL is 600 s
    #: (`_METADATA_TTL_S` in `polymarket/.../orders/cache.py`) while a 15m round
    #: outlives 10 minutes, so a memo that never expired would let a late order
    #: pay the lookups again.
    WARM_TTL_S = 240.0

    def warm(self, token_ids) -> int:
        """Pre-fetch a round's order metadata BEFORE an order needs it.

        MEASURED 2026-09-17, the httpx log beside a 918-947 ms FAK:
            decision
            GET /markets-by-token/<tok>      253 ms
            GET /clob-markets/<cond>         238 ms
            POST /order                      440 ms

        Those two GETs are `SecureClient`'s order-metadata cache resolving the
        market for a token. The cache is real -- `_conditions` never expires and
        `_markets` lives 600 s -- but it is keyed by TOKEN/CONDITION, and a
        token IS a round side, so every order is a cold lookup and ~490 of the
        930 ms was never the venue.

        That matters because the book moves 1-4c in that window: five of the
        first six live FAKs came back `no orders found to match`, one missing by
        a single cent. Warm off the critical path and the order is one POST.

        `resolve_market` is EXACTLY what `place_market_order` calls
        (`_internal/actions/orders/market.py:173`), so this fills the same cache
        entry the order will read -- and loading it back-fills the condition id
        for BOTH tokens of the round.

        Never raises: a warm failure must leave the order path exactly as it is
        (worst case the order pays the lookup, as it does today). Returns the
        number of tokens actually warmed; 0 means "already warm" (the common
        case, since the poll calls this continuously).

        THE LOCK: this runs on the poll's background worker while `buy_fak`
        runs on its own, and both touch the SDK's metadata cache.
        """
        with self._client_lock:
            return self._warm_body(token_ids)

    def _warm_body(self, token_ids) -> int:
        """Body of `warm`. Call with `_client_lock` HELD (see `warm`)."""
        try:
            c = self._client if self._client is not None else self._ensure_client()
        except Exception as exc:                              # noqa: BLE001
            self._log(f"pm warm skipped: {type(exc).__name__} {exc}")
            return 0
        ctx = getattr(c, "_ctx_inner", None)
        cache = getattr(ctx, "order_metadata", None)
        if ctx is None or cache is None:                      # pragma: no cover
            # A different SDK shape: say so ONCE per process, then stay quiet --
            # this runs every poll and a log line per poll is noise.
            if not getattr(self, "_warm_warned", False):
                self._warm_warned = True
                self._log("pm warm unavailable: client has no order_metadata "
                          "cache; orders will pay their own lookups")
            return 0
        now = self._now()
        n = 0
        for tok in token_ids or ():
            tok = str(tok or "")
            if not tok:
                continue
            # A PLACEHOLDER token (all zeros) is what a round carries before
            # its book resolves the side. MEASURED live: those 404 (90 in a
            # single minute after a restart), so each costs a ~150 ms GET that
            # can never help. Mark it warmed -- never retry a known 404 -- and
            # move on. Not logged: the poll would print it every second.
            if not tok.strip("0"):
                self._warm[tok] = now
                continue
            at = self._warm.get(tok)
            if at is not None and (now - at) < self.WARM_TTL_S:
                continue
            try:
                cache.resolve_market(ctx, token_id=tok)
            except Exception as exc:                          # noqa: BLE001
                # Record the ATTEMPT: retrying a broken lookup every poll would
                # put the very latency this removes back on the decision loop.
                self._warm[tok] = now
                self._log(f"pm warm failed for {tok[:12]}...: "
                          f"{type(exc).__name__} {exc}")
                continue
            self._warm[tok] = now
            n += 1
        self.warm_calls += n
        return n

    def warm_now(self, token_id: str) -> float:
        """Force the SDK's order-metadata resolve for ONE token. Never raises.

        ⛔ WHY THIS EXISTS BESIDE `warm()`. `warm()` is TTL-gated on its OWN
        `_warm` dict, so it reports "already warm" and returns without touching
        the SDK whenever it has seen a token inside `WARM_TTL_S` -- even when the
        SDK's own entry is missing. That is how 80% of live orders still paid
        489 ms of metadata GETs (20 of 25 attempts, measured 2026-09-17).

        This is the un-gated version, called on the order path for the exact
        token the order is about to use, so it primes the same cache entry
        `place_market_order` reads (`_internal/actions/orders/market.py:173`).
        Returns the milliseconds it cost -- a HIT is ~0-5 ms, a miss is the same
        ~489 ms the order would otherwise have paid itself.

        THE LOCK is an RLock and this is called from INSIDE `_buy_fak_body`,
        which already holds it on this thread -- re-entry is the point.
        """
        with self._client_lock:
            return self._warm_now_body(token_id)

    def _warm_now_body(self, token_id: str) -> float:
        """Body of `warm_now`. Call with `_client_lock` HELD."""
        try:
            c = self._client if self._client is not None else self._ensure_client()
            ctx = getattr(c, "_ctx_inner", None)
            cache = getattr(ctx, "order_metadata", None)
            tok = str(token_id or "")
            if ctx is None or cache is None or not tok or not tok.strip("0"):
                return 0.0
            t0 = time.perf_counter()
            cache.resolve_market(ctx, token_id=tok)
            self._warm[tok] = self._now()
            return (time.perf_counter() - t0) * 1000.0
        except Exception as exc:                              # noqa: BLE001
            self._log(f"pm warm_now failed for {str(token_id)[:12]}...: "
                      f"{type(exc).__name__} {exc}")
            return 0.0

    def plan(self, *, slug: str, side: str, stake: float,
             max_px: float | None = None,
             size_by_amount: bool = False) -> dict | None:
        """What we would pay, from the live book. Pure, no venue call.

        Returns None when the book is stale, the touch is missing, or the
        order cannot meet the venue's own minimums. The SWEEP price is used,
        not the touch: gating on the touch prices the order against liquidity
        that may be one tick wide.

        `size_by_amount=True` sizes in DOLLARS for the FAK/market path. See the
        two-minimums note below -- it is not a preference, it is the only way a
        $1 stake is sendable at a high price.
        """
        r = self.round_for(slug)
        if r is None:
            return None
        tok = r.token(side)
        t: Touch | None = self.book.best(tok, self.cfg.max_book_age_s)
        if t is None or t.ask <= 0:
            return None

        tick = r.tick or DEFAULT_TICK
        ceiling = max_px if max_px is not None else 1.0
        # Reach at most `slippage` past the ask, and never past the ceiling.
        want = min(t.ask + self.cfg.slippage, ceiling)
        limit = _round_to_tick(want, tick, up=True)
        if limit > ceiling:
            # Rounding up crossed our own ceiling; the next tick down is the
            # best legal price, and if that is below the ask we will rest
            # rather than fill.
            limit = _round_to_tick(ceiling, tick, up=False)
        if limit <= 0:
            return None
        if _below_entry_floor(min(limit, t.ask), self.cfg.min_entry_px):
            # BOTH prices. `limit` is what we would send; `t.ask` is what the
            # book shows. Either one at the floor means the fair behind this
            # plan is not a valuation, and the SIZE below is derived from
            # `limit` -- so a clamped limit is a 100x position, not just a bad
            # price. The log prints the share count it would have bought.
            self.last_failure = "px_below_floor"
            self._log(
                f"pm no plan for {slug} {side}: ask {t.ask:.4f} / limit "
                f"{limit:.4f} below the entry floor "
                f"{self.cfg.min_entry_px:.2f} (stake {stake:.2f} would buy "
                f"{math.ceil(stake / max(limit, 1e-9)):g} shares)")
            return None

        # ⭐ TWO MINIMUMS, MEASURED, AND THEY ARE NOT THE SAME NUMBER (2026-09-17)
        #   limit orders : the venue answers `Size (1) lower than the minimum:
        #                  5` and the same for 2 -- **5 SHARES**. A 5-share order
        #                  at 0.11 ($0.55 notional) was ACCEPTED, so there is no
        #                  $ floor on this path either. That share floor is why
        #                  a $1 stake is unsendable above ~$0.20 (5 x 0.20 =
        #                  $1.00), and why `stake_cap_usdt` had to be 4.5 for an
        #                  arm that trades 0.75-0.90.
        #   amount orders: `place_market_order(amount=1.0, ...)` came back
        #                  "no orders found to match with FAK" -- a MATCHING
        #                  rejection, i.e. the size passed -- where $1 at a 0.37
        #                  ask is 2.70 shares, BELOW that 5-share floor. The
        #                  FAK/market path's minimum is in DOLLARS, which is what
        #                  the venue's own UI `$1` box has always done.
        # ⇒ $1 SIZING IS AVAILABLE, and only on this path. Size it in dollars:
        #   the share count is the venue's business, and forcing 5 of them would
        #   silently multiply the stake by up to 5x.
        spend = 0.0
        if size_by_amount:
            spend = min(stake, self.cfg.stake_cap_usdt) if self.cfg.stake_cap_usdt > 0 else stake
            spend = round(spend, 4)
            if spend < MIN_NOTIONAL:
                self.last_failure = "below_min_notional"
                self._log(
                    f"pm no plan for {slug} {side}: ${spend:.2f} is below the "
                    f"venue's ${MIN_NOTIONAL:.2f} floor for an amount order "
                    f"(stake {stake:.2f}, cap {self.cfg.stake_cap_usdt:.2f})")
                return None
            shares = max(1.0, math.ceil(spend / max(limit, 1e-9)))
        else:
            shares = max(r.min_order or DEFAULT_MIN_SHARES,
                         math.ceil(stake / limit))
            if shares * limit < MIN_NOTIONAL:
                return None
        if not size_by_amount and _size_above_cap(shares, limit, self.cfg.stake_cap_usdt):
            # The cap is a cap: SHRINK to fit before giving up. Refusing was
            # throwing away orders that a legal size existed for; only refuse
            # when no count is both tradeable and within it (see
            # `_shares_within_cap`).
            shrunk = _shares_within_cap(shares, limit,
                                        self.cfg.stake_cap_usdt,
                                        r.min_order or DEFAULT_MIN_SHARES)
            if shrunk is None:
                self.last_failure = "size_above_cap"
                self._log(
                    f"pm no plan for {slug} {side}: the venue minimum of "
                    f"{shares:g} shares at {limit:.2f} costs "
                    f"${shares * limit:.2f}, above the stake cap "
                    f"${self.cfg.stake_cap_usdt:.2f} (stake {stake:.2f})")
                return None
            self._log(
                f"pm size fitted to the cap {slug} {side}: {shares:g} -> "
                f"{shrunk:g} shares at {limit:.2f} "
                f"(${shrunk * limit:.2f} <= cap "
                f"${self.cfg.stake_cap_usdt:.2f})")
            shares = shrunk
        sweep_px, fillable = t.sweep(spend or shares * limit)
        return {
            "slug": slug, "side": side, "token_id": tok, "ask": t.ask,
            "sweep_px": sweep_px, "fillable": fillable, "limit": limit,
            "shares": shares, "ceiling": ceiling, "tick": tick,
            "book_age_s": t.age_s(), "bid": t.bid,
            "spend": spend or shares * limit,
        }

    # -- the wrapper's quote shape (no venue call) -------------------------
    async def get_quote(self, token_id: str, stake: float, side: str = "BUY",
                        *, order_type: str = "MARKET",
                        price_limit: float | None = None) -> dict | None:
        """The reference price for `stake`, in the wrapper's shape.

        `averagePrice` is the BOOK SWEEP for that stake -- the volume-weighted
        price of the shares a taker would actually take. That is a strictly
        better number than a displayed price, and it needs no complementing:
        the caller passes the token being bought, so this IS that side's price.
        (`main.py` carries a long note about a `1.0 - real_px` bug that cost it
        every Down FAVOURITE; this return value cannot reproduce it.)

        `feeRateBps` is the taker schedule expressed against NOTIONAL. The fee
        per SHARE is `0.07*p*(1-p)`, so against notional it is `0.07*(1-p)`:
        **350 bps at 0.50, 70 bps at 0.90** -- the opposite shape to the
        wrapper's flat ~96 bps, and cheap exactly where the favourite lives.
        Feeding the wrapper's constant in here would understate cost by 5x at
        mid prices and overstate it at the extremes.

        DELIBERATELY NOT routed through `live.quote_is_unusable`. That gate
        encodes the wrapper's `slippageBps` multiplier (`bound <= quote * 2`,
        so `quote*2 < ceiling` is structurally unfillable). This venue takes an
        EXPLICIT limit price instead, so that structural condition does not
        exist here and copying the gate would refuse healthy quotes.

        `order_type` and `price_limit` are accepted and ignored, so a caller
        written for the wrapper runs unchanged.
        """
        t = self.book.best(token_id, self.cfg.max_book_age_s)
        if t is None or t.ask <= 0:
            return None
        px, fillable = t.sweep(stake)
        if px <= 0:
            px = t.ask
        return {
            "venue": "polymarket",
            "tokenId": token_id,
            "side": side,
            "averagePrice": px,
            "feeRateBps": TAKER_FEE_RATE * (1.0 - px) * 1e4,
            "amountIn": stake,
            "fillable": fillable,
            "ask": t.ask,
            "bid": t.bid,
            "askSize": t.ask_size,
            "bookAgeS": t.age_s(),
        }

    # -- the hot path (2 venue calls) -------------------------------------
    async def buy(self, *, slug: str, side: str, token_id: str,
                  price: float, stake: float,
                  quote: dict | None = None,
                  max_px: float | None = None) -> Fill | None:
        """Place a marketable BUY. Two venue calls, one of them the POST.

        `price` is the caller's idea of the price; the limit actually sent is
        derived from the live book inside `plan()`, because a limit built from
        a caller's stale number is how a ceiling stops meaning anything.

        `quote` is accepted and IGNORED, for `LiveBroker.buy` parameter parity.
        The names are the contract, not a suggestion -- `main.py` sends `quote=`
        whenever `broker.mode == "live"` (this broker IS "live", by design), so
        a broker that mirrors the MODE but not the SIGNATURE passes every guard
        and then raises TypeError on the first order. MEASURED 2026-09-17
        04:44:17, the first cross order to take the FAK path --
            cross order failed for sol-updown-5m-1789620000 Down:
            TypeError: PmBroker.buy() got an unexpected keyword argument 'quote'
        LiveBroker uses it to skip a second round trip and to bound a MARKET
        order by the venue's DISPLAYED price. Here the book is the websocket
        one and `plan()` derives the limit from it, so a caller-supplied quote
        has nothing to add -- accepting it is the whole of the fix.

        `order_type` on the config switches this to the FAK (IOC) path -- see
        `buy_fak` for what that costs and why it is not the default.
        """
        if str(self.cfg.order_type).lower() == "fak":
            return await self.buy_fak(slug=slug, side=side, token_id=token_id,
                                      price=price, stake=stake, quote=quote,
                                      max_px=max_px)
        p = self.plan(slug=slug, side=side, stake=stake, max_px=max_px)
        if p is None:
            self.last_failure = "no_fresh_book"
            self._log(f"pm plan refused {slug} {side}: no fresh book")
            return None
        if p["limit"] > p["ceiling"] + 1e-9:
            self.last_failure = "above_ceiling"
            self._log(f"pm plan refused {slug}: limit {p['limit']} > ceiling")
            return None
        if token_id and token_id != p["token_id"]:
            # The caller's id disagrees with the round's. Trust the round: it
            # comes from the venue, and a wrong token id buys the wrong side.
            self._log(f"pm token mismatch {slug}: caller {token_id[:12]} vs "
                      f"round {p['token_id'][:12]}; using the round's")

        c = self._ensure_client()
        calls = 0
        t0 = time.perf_counter()
        try:
            signed = c.create_limit_order(
                token_id=p["token_id"], price=p["limit"],
                size=p["shares"], side="BUY", post_only=False)
            calls += 1
            resp = c.post_order(signed)
            calls += 1
        except Exception as exc:                             # noqa: BLE001
            self.venue_calls = calls
            self.last_failure = "post_failed"
            self.last_order_error = f"{type(exc).__name__}: {exc}"
            self._log(f"pm order failed after {calls} calls: "
                      f"{type(exc).__name__} {exc}")
            return None
        dt_ms = (time.perf_counter() - t0) * 1000.0
        self.venue_calls = calls
        if calls > self.cfg.max_venue_calls:
            self._log(f"pm WARNING: {calls} venue calls on the hot path "
                      f"(budget {self.cfg.max_venue_calls})")

        oid = (_a(resp, "order_id", "orderId", default="") or "")
        status = _a(resp, "status", default=None)

        # REJECTION IS NOT A FILL, AND NOT AN UNVERIFIED ONE EITHER.
        # `post_order` returns `AcceptedOrder | RejectedOrder`; a rejection has
        # `ok=False`, a `code` and a `message` and NO order id. Treating it as
        # accepted would report a Fill that never existed and then send the
        # operator hunting for a phantom position.
        if _a(resp, "ok", default=True) is False:
            self.last_failure = "rejected"
            self.last_order_error = (f"{_a(resp, 'code', default='')} "
                                     f"{_a(resp, 'message', default='')}").strip()
            self._log(f"pm order REJECTED {slug} {side} @ {p['limit']}: "
                      f"{self.last_order_error} (ack {dt_ms:.0f} ms)")
            return None

        # The ack is NOT the fill. Reconcile against the account.
        got = self._confirm(p["token_id"], t0, p["shares"])
        if got is None:
            self.unverified += 1
            self.last_failure = "unverified"
            self._log(f"pm order {oid} status {status} ack {dt_ms:.0f} ms "
                      f"-- UNVERIFIED, no trade record yet")
            return Fill(
                slug=slug, side=side, requested_px=p["limit"],
                filled_px=p["limit"], shares=p["shares"],
                stake=p["shares"] * p["limit"],
                fee_paid=0.0, order_id=str(oid),
                ts_ms=int(self._now() * 1000), token_id=p["token_id"],
                raw=f"ack {dt_ms:.0f}ms status={status}",
                unverified=True)

        shares, px, fee = got
        stake_paid = shares * px
        self._balance_cache = None      # we just spent money
        self._log(f"pm FILL {shares:g} {side} @ {px} ({slug}) "
                  f"limit {p['limit']} ask {p['ask']} fee {fee:.4f} "
                  f"ack {dt_ms:.0f} ms")
        return Fill(
            slug=slug, side=side, requested_px=p["limit"], filled_px=px,
            shares=shares, stake=stake_paid, fee_paid=fee,
            order_id=str(oid), ts_ms=int(self._now() * 1000),
            token_id=p["token_id"], raw=f"ack {dt_ms:.0f}ms status={status}")

    def _trades_since(self, token_id: str, since_epoch: float
                      ) -> tuple[float, float, float] | None:
        """(shares, vwap price, fee) from the venue's OWN trade records.

        This is the primary confirmation because it is the only source that is
        correct in all three of the ways a position read is not:

        * **It is a fill, not a total.** A position's size is cumulative, so
          buying the same token twice in a round (a retry, a second entry)
          would report the running total and overstate the second stake.
        * **It carries the venue's real fee** (`fee_rate_bps`), which is the
          number the metadata only predicts. Measured maker fee = $0.00, so
          the schedule is taker-only; when the venue reports 0 we still charge
          the schedule analytically because n=1 does not settle it.
        * **It has a `transaction_hash`**, so a booked fill is auditable.

        Returns None when the venue will not say -- which must NOT be read as
        "no fill".

        `since_epoch` is a WALL-CLOCK epoch bound, not a perf_counter: the perf
        clock cannot be compared against a venue timestamp at all. Callers
        right after an order pass `now - a minute`; `settle()` passes the
        fill's own timestamp minus slack, because by then a minute has passed
        and a rolling window would have excluded the very fill being checked.
        """
        c = self._ensure_client()
        # ⛔ THE READ IS INSIDE THE GUARD, NOT JUST THE CALL. `list_account_trades`
        # returns a LAZY PAGINATOR: the HTTP call happens on ITERATION, so
        # `_records(page)` below used to run OUTSIDE this `try` and a malformed
        # response raised straight out of `_trades_since` -- past `_confirm`,
        # past `buy_fak`, and out of the arm's order path, where
        # `except Exception` logged `cross order failed ... not retrying` and
        # wrote the order off ENTIRELY.
        #
        # LIVE, 2026-09-17 19:09:12, btc-updown-5m-1789671900 Down:
        #     CROSS ... sweep_px=0.320 (spot 76584.98 vs strike 76585.14, tau 50s)
        #     ERROR cross order failed ... UnexpectedResponseError:
        #           account trades response items malformed
        # The FAK had MATCHED at the venue (3.1 shares @ 0.335, $1.05), the round
        # settled Down -- the side we bought -- and the venue paid $3.13. The bot
        # booked NOTHING: no `fills` row, no `settlements` row, a released slot,
        # and the day's P&L short by ~$2.08 with the exposure outside every cap
        # the ledger enforces. A read that cannot be parsed is NOT an order that
        # failed, and this method's contract has always been "return None when
        # the venue will not say".
        try:
            page = c.list_account_trades(token_id=token_id)
            recs = _records(page)
        except Exception as exc:                             # noqa: BLE001
            self._log(f"pm trade read failed: {type(exc).__name__} {exc}")
            return None
        shares = notional = fee = 0.0
        taker_shares = 0.0
        seen = False
        try:
            for tr in recs:
                if str(_a(tr, "asset_id", "token_id", default="")) != token_id:
                    continue
                ts = _ts_epoch(_a(tr, "matched_at", "updated_at"))
                # An UNPARSABLE stamp must not disqualify a trade: a wrong time
                # is a parsing failure, not evidence that the fill is not ours.
                if ts is not None and ts < since_epoch:
                    continue
                sz = float(_a(tr, "size", default=0) or 0)
                px = float(_a(tr, "price", default=0) or 0)
                if sz <= 0 or px <= 0:
                    continue
                seen = True
                shares += sz
                notional += sz * px
                rb = _a(tr, "fee_rate_bps")
                if rb:
                    fee += abs(float(rb)) / 1e4 * px * sz
                # `trader_side` is the venue's own attribution, and the fee
                # schedule is TAKERONLY. Without it a maker fill picks up a taker
                # fee -- and an unknown side is charged, because guessing "maker"
                # would understate cost on the path that actually pays.
                if str(_a(tr, "trader_side", default="")).upper() != "MAKER":
                    taker_shares += sz
        except Exception as exc:                             # noqa: BLE001
            # A row that will not parse means the TOTAL is untrustworthy, and a
            # partial total is worse than none: it would book a fraction of a
            # fill as if it were the whole one. Say "no answer" and let the
            # caller fall back to the position list.
            self._log(f"pm trade read unparsable: {type(exc).__name__} {exc}")
            return None
        if not seen or shares <= 0:
            return None
        px = notional / shares
        if fee <= 0.0 and taker_shares > 0.0 \
                and self.cfg.charge_fee_when_unreported:
            fee = taker_fee(px, taker_shares, self.cfg.taker_fee_rate)
        return shares, px, fee

    def _position_size_checked(self, token_id: str) -> float | None:
        """Shares held in `token_id`, or **None when the read fails**.

        ⛔ THIS EXISTS BECAUSE `_position_size` CANNOT ANSWER THIS QUESTION.
        It returns 0.0 for an unreadable position -- documented, and right for
        its original caller (`settle()` asks "is the token held", where 0 and
        "cannot read" lead to the same conservative action). For a SELL they
        lead to OPPOSITE actions, and the difference is a phantom position:

            sold = held - left          with left = 0.0 from a FAILED read
                 = held                 -> the broker reports a completed sale,
                                           the caller books a settlement row,
                                           releases the slot and drops the fill

        The shares are still on-chain, unmanaged, outside every cap -- and the
        round will never be settled for them because `open_fills` no longer
        holds the fill. That is the 2026-09-17 incident in reverse ("an
        unreadable read must never be read as an answer"), so the sell path
        asks a question that can say "I do not know".
        """
        c = self._ensure_client()
        try:
            page = c.list_positions()
            recs = _records(page)
        except Exception:                                    # noqa: BLE001
            return None
        try:
            for it in recs:    # `list_positions()` is a Paginator too
                if str(_a(it, "asset_id", "token_id", default="")) != token_id:
                    continue
                n = float(_a(it, "current_size", "total_size", "size",
                             default=0) or 0)
                if n > 0:
                    return n
        except Exception:                                    # noqa: BLE001
            return None
        # A readable, complete list with no row for this token IS zero.
        return 0.0

    def _position_size(self, token_id: str) -> float:
        """Total shares held in `token_id`, or 0.0 if unreadable.

        `Position` HAS NO `size` FIELD. It is `current_size` (with
        `total_size` as the lifetime variant), and reading the wrong name does
        not raise: it returns 0, which makes every fill look unfilled. This
        bug was found by `tools/pm_sdk_audit.py`, not by a test, which is why
        that script exists.

        NEVER RAISES, for the reason `_trades_since` documents: `list_positions()`
        is a lazy Paginator too, so `_records(page)` must run INSIDE the guard.
        It used to sit outside, and `settle()` now asks this question to decide
        whether an unverified fill was real -- a raise there would turn "cannot
        read the position" into "the position does not exist", i.e. into voiding
        a fill the venue actually filled.

        KEPT AS THE 0.0-EQUIVALENT CALL for `settle()`, and it is now a thin
        wrapper: the sell path must ask `_position_size_checked` instead, where
        "cannot read" and "not held" must not collapse into one answer.
        """
        n = self._position_size_checked(token_id)
        return 0.0 if n is None else n

    def _confirm(self, token_id: str, t0: float,
                 want_shares: float) -> tuple[float, float, float] | None:
        """(shares, price, fee) actually filled, or None if the venue will not
        say.

        Polls rather than sleeping once: measured, a post-only bid filled in
        the SAME second while the cancel ack claimed the order could not be
        found, so the record can lag the ack by an unknown amount.

        Returns None rather than guessing. `settle()` re-checks and VOIDS an
        unverified fill, whereas a fabricated one books fake P&L forever.
        """
        deadline = t0 + self.cfg.confirm_budget_s
        while True:
            got = self._trades_since(token_id, time.time() - 60.0)
            if got is not None:
                return got
            if time.perf_counter() >= deadline:
                return None
            time.sleep(0.15)

    # -- settlement (cold path) -------------------------------------------
    async def settle(self, fill: Fill, winner: str) -> Settlement:
        """Book a fill against the round's winner.

        Polymarket's AutoRedeemer sweeps winners gaslessly, so there is no
        transaction here: `payout` is what the position returns if it wins,
        and `pnl` is that minus what it cost. An `unverified` fill is
        re-verified first, and voided if the venue never actually filled it --
        the same contract `live.py` has, for the same reason.
        """
        now_ms = int(self._now() * 1000)
        if fill.unverified:
            # Widen the window to this fill's own arrival: by settle time a
            # rolling minute would exclude it.
            since = (fill.ts_ms / 1000.0) - 5.0
            got = self._trades_since(fill.token_id, since)
            if got is not None:
                fill.shares, fill.filled_px, fill.fee_paid = got
                fill.stake = fill.shares * fill.filled_px
                fill.unverified = False
            else:
                # ⛔ THE POSITION LIST IS THE SECOND SOURCE, AND A HELD TOKEN
                # MEANS THE ORDER FILLED. Voiding on "the trade list would not
                # parse" threw away a real position's P&L: live 2026-09-17
                # 19:09 on btc-updown-5m-1789671900 the venue matched 3.1
                # shares @ 0.335, settled Down and paid $3.13, while the
                # readback died on `UnexpectedResponseError: account trades
                # response items malformed`. The bot cannot always read the
                # trade FILE; the wallet is the thing that actually holds the
                # shares, so ask it before writing the position off.
                #
                # The fill's own numbers are kept deliberately: `filled_px` is
                # the CEILING of the order it sent, which is the most a FAK can
                # pay, so the stake is an upper bound and the P&L is
                # conservative in both directions. A position SMALLER than the
                # sizes we booked means a partial fill, and the position wins.
                held = self._position_size(fill.token_id)
                if held > 0:
                    if held < fill.shares:
                        fill.shares = held
                        fill.stake = held * fill.filled_px
                    if fill.fee_paid <= 0.0 \
                            and self.cfg.charge_fee_when_unreported:
                        # The venue's own fee is unreadable, so charge the
                        # schedule rather than booking a free fill: the
                        # schedule is taker-only and this is a taker order.
                        fill.fee_paid = taker_fee(fill.filled_px, fill.shares,
                                                  self.cfg.taker_fee_rate)
                    fill.unverified = False
                    self._log(f"pm unverified {fill.order_id} CONFIRMED by the "
                              f"position list: {fill.shares:g} {fill.side} "
                              f"({fill.slug}) -- the trade record is still "
                              f"unreadable, so the price is the ceiling "
                              f"{fill.filled_px}")
                else:
                    return Settlement(slug=fill.slug, won=False, payout=0.0,
                                      pnl=0.0, ts_ms=now_ms, void=True)

        won = (fill.side == winner)
        payout = fill.shares if won else 0.0
        pnl = payout - fill.stake - fill.fee_paid
        self._balance_cache = None
        return Settlement(slug=fill.slug, won=won, payout=payout, pnl=pnl,
                          ts_ms=now_ms)

    async def close(self) -> None:
        c = self._client
        if c is not None:
            try:
                c.close()
            except Exception:                                # noqa: BLE001
                pass

    # -- book, in the shape the arms already expect -----------------------
    async def order_book(self, token_id: str, market_id: Any = None) -> dict | None:
        """Top of book as `LiveBroker.order_book` returns it.

        `market_id` is accepted and ignored: the wrapper needs it, this venue
        addresses a book by token id alone. It keeps the wrapper's NAME so a
        keyword call works too, and it exists at all so an arm can be pointed
        at either venue unchanged.
        """
        t = self.book.best(token_id, self.cfg.max_book_age_s)
        if t is None:
            return None
        return {"bids": list(t.bids), "asks": list(t.asks),
                "bid": t.bid, "ask": t.ask,
                "bidSize": t.bid_size, "askSize": t.ask_size,
                "tsMs": t.ts_ms}

    async def best_prices(self, token_id: str, market_id: Any = None
                          ) -> tuple[float, float] | None:
        t = self.book.best(token_id, self.cfg.max_book_age_s)
        return None if t is None else (t.bid, t.ask)

    # -- positions --------------------------------------------------------
    async def positions(self) -> list[dict]:
        """Open positions, in the wrapper's key style (`tokenId`).

        `main.py::_venue_position_tokens` reads `tokenId` and nothing else, and
        uses an EMPTY list as "the venue holds nothing" -- so a failed read
        must not be reported as an empty account. It returns [] here and logs;
        the caller's `_unbooked_round_tokens` treats None as "cannot see", and
        an inability to read is exactly when the arm should sit out. Callers
        that need that distinction should read `last_failure`.
        """
        c = self._ensure_client()
        try:
            res = c.list_positions()
        except Exception as exc:                             # noqa: BLE001
            self.last_failure = "positions_read_failed"
            self._log(f"pm positions read failed: {type(exc).__name__} {exc}")
            return []
        items = _a(res, "positions", default=None) or _records(res)
        out: list[dict] = []
        for it in items:
            # `current_size` is the open size; there is NO `size` field.
            size = float(_a(it, "current_size", "total_size", "size",
                            default=0) or 0)
            if size <= 0:
                continue
            out.append({
                "tokenId": str(_a(it, "asset_id", "token_id", default="")),
                "size": size,
                "avgPrice": float(_a(it, "avg_price", "avgPrice", default=0) or 0),
                "currentValue": float(
                    _a(it, "current_value", "currentValue", default=0) or 0),
                "redeemable": bool(_a(it, "redeemable", default=False)),
                "totalPnl": float(_a(it, "total_pnl", "totalPnl", default=0) or 0),
                "entryFees": float(
                    _a(it, "entry_fees_usdc", "entryFees", default=0) or 0),
                "conditionId": str(_a(it, "condition_id", default="")),
            })
        return out

    async def claimable_token_ids(self) -> list[str]:
        """Winning positions the venue still shows as redeemable.

        Usually EMPTY, and that is the correct answer: Polymarket's
        AutoRedeemer (the ERC-1155 operator granted by
        `setup_trading_approvals`) sweeps winners gaslessly about 10 s after
        they become redeemable. A non-empty list means the sweep has not run.
        """
        return [p["tokenId"] for p in await self.positions()
                if p["redeemable"] and p["tokenId"]]

    async def redeem(self, token_ids: list[str] | None = None) -> dict | None:
        """Redeem by CONDITION id, and WAIT for the transaction.

        MEASURED FAILURE, 2026-09-16. This called `redeem_positions(token_id=tok)`
        for each claimable token, once every 3 minutes: **20 consecutive
        attempts, `redeem ok` count 0**, every one raising

            TypeError: SecureClient.redeem_positions() got an unexpected
            keyword argument 'token_id'

        The installed client (`polymarket-client` 0.10.0) declares

            redeem_positions(*, condition_id=None, market_id=None,
                             position_id=None, metadata=None) -> handle

        -- there is no `token_id` parameter, and it returns a TRANSACTION
        HANDLE whose `.wait()` must be called. So the call raised on the
        signature before it reached the venue at all, and a version that
        happened to pass a valid kwarg would have dispatched the transaction
        and reported success without ever waiting for it. Neither failure is
        visible in the return value, which is why the sweep reported
        `{'redeemed': []}` every 3 minutes for hours and nobody noticed.

        The token->condition mapping therefore has to come from the position
        record. `positions()` reads the venue's own `condition_id` field, so
        `redeem` resolves it there instead of taking the ids on trust.

        `redeem_positions(condition_id=...)` still raises
        `UserInputError: "No market found for condition 0x..."` when there is
        nothing left to redeem. That reads like a lookup bug and means
        "already swept", so it is reported as a benign no-op.
        """
        want = None if token_ids is None else set(token_ids)
        todo: dict[str, str] = {}
        for p in await self.positions():
            if not (p["redeemable"] and p["tokenId"] and p["conditionId"]):
                continue
            if want is None or p["tokenId"] in want:
                # Keyed by CONDITION, so two tokens of one market (the Up and
                # the Down) redeem once rather than twice.
                todo.setdefault(p["conditionId"], p["tokenId"])
        if not todo:
            return {"redeemed": [], "reason": "nothing_redeemable"}
        c = self._ensure_client()
        if not hasattr(c, "redeem_positions"):
            return {"redeemed": [], "reason": "sdk_has_no_redeem"}
        done: list[str] = []
        for cond, tok in todo.items():
            # BOTH halves block. The dispatch signs and posts, and `wait()`
            # polls the chain for a terminal outcome -- measured at ~10 s
            # after the AutoRedeemer loses the race. This is one event loop and
            # a claim must not stall the tick, so both go to a thread.
            try:
                handle = await asyncio.to_thread(c.redeem_positions,
                                                 condition_id=cond)
                if handle is not None and hasattr(handle, "wait"):
                    await asyncio.to_thread(handle.wait)
                done.append(tok)
            except Exception as exc:                         # noqa: BLE001
                msg = str(exc)
                if "No market found" in msg or "no balance" in msg:
                    self._log(f"pm redeem {tok[:12]}: already swept")
                    done.append(tok)      # swept is a success, not a failure
                else:
                    self._log(f"pm redeem {tok[:12]} failed: "
                              f"{type(exc).__name__} {msg}")
        self._balance_cache = None
        return {"redeemed": done}

    # -- resting orders (the MAKER path) ----------------------------------
    async def active_orders(self) -> list[dict]:
        """Resting orders. The SDK method is `list_open_orders`.

        NOT `list_orders` -- that name does not exist. Calling it raises
        AttributeError, which the handler below swallows into an empty list, so
        the failure mode is a permanently empty order book rather than a
        crash. Found by tools/pm_sdk_audit.py.
        """
        c = self._ensure_client()
        try:
            res = c.list_open_orders()
            # INSIDE the guard, for the reason `_trades_since` documents:
            # `list_open_orders` is a lazy Paginator, so the HTTP call happens
            # on iteration and a `try` around the call guards nothing.
            items = _a(res, "orders", default=None) or _records(res)
        except Exception as exc:                             # noqa: BLE001
            self.last_failure = "open_orders_read_failed"
            self._log(f"pm open-orders read failed: {type(exc).__name__} {exc}")
            return []
        out: list[dict] = []
        try:
            for o in items:
                out.append({
                    "orderId": str(_a(o, "id", "order_id", default="")),
                    "tokenId": str(_a(o, "asset_id", "token_id", default="")),
                    "price": float(_a(o, "price", default=0) or 0),
                    "size": float(_a(o, "original_size", "size", default=0) or 0),
                    "filled": float(_a(o, "size_matched", default=0) or 0),
                    "status": str(_a(o, "status", default="")),
                    "orderType": str(_a(o, "order_type", default="")),
                })
        except Exception as exc:                             # noqa: BLE001
            # LOUD, because an empty list here reads as "nothing is resting"
            # and that is the failure mode this method's own docs complain
            # about. A partial list would be worse: it hides the orders it
            # dropped.
            self.last_failure = "open_orders_unparsable"
            self._log(f"pm open-orders unparsable: {type(exc).__name__} {exc}")
            return []
        return out

    async def cancel_orders(self, order_ids: list[str]) -> list[str]:
        """Cancel, returning the ids the venue ACCEPTED the cancel for.

        This is NOT proof of no fill. Measured: a cancel came back `"order
        can't be found - already canceled or matched"` while the position had
        already been filled in the same second. Read `positions()` for truth.
        """
        c = self._ensure_client()
        done: list[str] = []
        for oid in order_ids or []:
            try:
                c.cancel_order(order_id=str(oid))
                done.append(str(oid))
            except Exception as exc:                         # noqa: BLE001
                self._log(f"pm cancel {oid} failed: {type(exc).__name__} {exc}")
        if done:
            self._balance_cache = None
        return done

    async def buy_fak(self, *, slug: str, side: str, token_id: str,
                      price: float, stake: float,
                      quote: dict | None = None,
                      max_px: float | None = None) -> Fill | None:
        """Take liquidity ATOMICALLY: FAK (Fill-And-Kill == IOC) at a ceiling.

        `quote` is accepted and IGNORED for `LiveBroker.buy` parity; see `buy`
        for the TypeError that a broker mirroring the mode but not the
        signature produces on the first order.

        WHY THIS EXISTS. `buy()` posts a LIMIT, which on this venue either
        crosses (a taker fill) or RESTS on the book. A resting order is filled
        only when the market comes to us -- which is exactly the adverse
        selection measured on the predecessor of this arm: maker fills won 41%
        at limits clustering 0.6-0.7 (EV -0.30/$1), because the fill set is
        selected against you. FAK removes that: the matching engine takes
        whatever is there in ONE step and KILLS the remainder, so nothing is
        left behind as bait.

        WHAT IT COSTS, MEASURED ON THE LIVE BOX 2026-09-16 (n=4 each):
            place_market_order(FAK)          min 404  median 414 ms
            create_limit_order + post_order  min 254  median 261 ms
        **+153 ms, a 1.59x penalty**, against a horizon this arm has already
        measured: 250 ms profitable at EVERY cap, 500 ms NEGATIVE at every
        cap. So FAK is not free and is not the obvious winner -- it is opt-in
        per order (`pm_order_type`) and must be A/B'd on its own fills.

        THE ACK IS STILL NOT THE FILL. A FAK that cannot fill raises
        `RequestRejectedError` rather than returning an accepted order with
        zero fill; that is a NO-FILL, not a venue failure, and it is reported
        as `fak_unfilled` so it lands in `no_cooldown` instead of pausing every
        order for 30 s.

        `max_px` is the ceiling and the ONLY risk bound: the venue will not pay
        above it and kills the rest. Pass `ev_ceiling(p_win, margin)`, never a
        stale ask.

        ⛔ THE BODY RUNS OFF THE EVENT LOOP, AND THAT IS A MEASURED FIX, NOT A
        STYLE. This method used to `await` nothing while calling a SYNCHRONOUS
        SDK client, so the whole POST + `_confirm` readback (400-1000 ms) ran
        with the event loop blocked -- and the venue's websocket consumer is an
        asyncio task on that same loop. It could not drain while we ordered.
        Live proof, 2026-09-17 20:49:
            24,984  pm FAK no fill ... ack 407 ms
            24,993  pm book ws lagging: venue lag 551ms after draining 399 frames
        Nine milliseconds after the ack, 399 frames (~400 ms at ~1k msg/s) were
        applied at once. The book we priced the NEXT decision from was that far
        behind, and the shadow probe's `tok-lag` at every fire read 361-486 ms.
        `asyncio.to_thread` frees the loop for the duration, so the drain runs
        DURING the order instead of after it. Measured effect on the decision
        datum: worth roughly +0.04-0.07/share by the 369-trigger entry-lag
        sweep (0 ms +0.0293 t +1.20, 200 ms -0.0431 t -1.91) -- it does not
        make the arm positive, it stops us pricing it off a stale book.
        """
        return await asyncio.to_thread(
            self._buy_fak_sync, slug=slug, side=side, token_id=token_id,
            price=price, stake=stake, quote=quote, max_px=max_px)

    def _buy_fak_sync(self, *, slug: str, side: str, token_id: str,
                      price: float, stake: float,
                      quote: dict | None = None,
                      max_px: float | None = None) -> Fill | None:
        """`buy_fak`'s blocking body. Runs on a worker thread, under the lock.

        The lock is not decoration: the background warm queue (`Bot` ->
        `PmBroker.warm`) also runs on a worker thread and touches the same SDK
        metadata cache, and before this the POST was issued from the LOOP
        thread while that queue warmed from a thread -- two threads, one
        client. One at a time now.
        """
        with self._client_lock:
            return self._buy_fak_body(
                slug=slug, side=side, token_id=token_id, price=price,
                stake=stake, quote=quote, max_px=max_px)

    def _buy_fak_body(self, *, slug: str, side: str, token_id: str,
                      price: float, stake: float,
                      quote: dict | None = None,
                      max_px: float | None = None) -> Fill | None:
        """Verbatim body of `buy_fak`. Call with `_client_lock` HELD."""
        p = self.plan(slug=slug, side=side, stake=stake, max_px=max_px,
                      size_by_amount=True)
        if p is None:
            self.last_failure = self.last_failure or "no_fresh_book"
            self._log(f"pm plan refused {slug} {side}: no fresh book")
            return None
        # SIZED IN DOLLARS, and this is the whole point of the amount path: the
        # venue's 5-SHARE floor applies to LIMIT orders, not to `amount`, so a
        # $1 stake is sendable at ANY price here (measured 2026-09-17 -- see the
        # two-minimums note in `plan()`). Sizing from shares instead would
        # spend 5 x limit and quietly multiply the operator's stake.
        spend = round(p["spend"], 4)
        if p["ceiling"] <= 0 or spend < MIN_NOTIONAL:
            self.last_failure = "below_min_notional"
            self._log(f"pm fak refused {slug}: spend ${spend:.2f} below the "
                      f"venue minimum (${MIN_NOTIONAL:.2f})")
            return None

        c = self._ensure_client()
        # ⛔ FORCE THE METADATA RESOLVE FOR *THIS* TOKEN, IMMEDIATELY BEFORE THE
        # POST. MEASURED 2026-09-17 over 25 live FAK attempts, by counting the
        # httpx lines between `take via FAK` and the ack line:
        #     20 of 25 paid BOTH metadata GETs -> ack median 947 ms (897-1401)
        #      5 of 25 paid NEITHER            -> ack median 458 ms (415-498)
        # The 489 ms difference IS the two GETs (253 + 238). The background warm
        # (`Bot.tick` -> `PmBroker.warm`) was therefore covering only 20% of
        # orders: it is ONE round per tick off a single-slot task, and it trusts
        # its own `_warm` dict rather than the SDK's cache. This call does not
        # trust that dict -- it resolves the exact token `place_market_order` is
        # about to resolve.
        #
        # IT CANNOT MAKE THINGS WORSE: on a cache hit `resolve_market` returns in
        # ~0-5 ms (measured), and on a miss it costs the same lookup the order
        # would have paid one line later. `warm_now` never raises, so a failure
        # leaves the order path exactly as it is.
        #
        # CALLED INLINE, not via `asyncio.to_thread`: this whole body already
        # runs on a worker thread holding `_client_lock`, so an extra thread
        # would only queue behind that same lock.
        try:
            self.warm_now(p["token_id"])
        except Exception:                                     # noqa: BLE001
            pass
        calls = 0
        t0 = time.perf_counter()
        try:
            resp = c.place_market_order(
                token_id=p["token_id"], side="BUY", amount=spend,
                max_price=p["ceiling"], order_type="FAK")
            calls += 1
        except Exception as exc:                             # noqa: BLE001
            self.venue_calls = calls
            dt_ms = (time.perf_counter() - t0) * 1000.0
            name = type(exc).__name__
            # A kill is a NO-FILL, not a failure. Anything else (a schema or
            # allowance rejection) must stay loud: the message is carried, and
            # this is the class that shipped `slip 0` once on the other venue.
            if "RequestRejected" in name or "Liquidity" in name:
                self.last_failure = "fak_unfilled"
                self._log(f"pm FAK no fill {slug} {side} <= {p['ceiling']} "
                          f"(ack {dt_ms:.0f} ms): {exc}")
            else:
                self.last_failure = "post_failed"
                self._log(f"pm FAK FAILED {slug} {side} after {calls} call(s) "
                          f"({dt_ms:.0f} ms): {name} {exc}")
            self.last_order_error = f"{name}: {exc}"
            return None
        dt_ms = (time.perf_counter() - t0) * 1000.0
        self.venue_calls = calls

        if _a(resp, "ok", default=True) is False:
            # FOK/FAK can also come back as a rejected ORDER OBJECT.
            self.last_failure = "fak_unfilled"
            self.last_order_error = (f"{_a(resp, 'code', default='')} "
                                     f"{_a(resp, 'message', default='')}").strip()
            self._log(f"pm FAK no fill {slug} {side} <= {p['ceiling']}: "
                      f"{self.last_order_error} (ack {dt_ms:.0f} ms)")
            return None

        oid = (_a(resp, "order_id", "orderId", default="") or "")
        status = _a(resp, "status", default=None)
        # The FEWEST shares this spend can buy at the ceiling: the venue fills
        # at or below `max_price`, so `floor(spend/ceiling)` is the guaranteed
        # count. `_confirm` waits for that many, and an estimate rounded UP
        # would make every fill look short and burn the whole budget.
        expect = max(1.0, math.floor(spend / max(p["ceiling"], 1e-9)))
        got = self._confirm(p["token_id"], t0, expect)
        if got is None:
            self.unverified += 1
            self.last_failure = "unverified"
            self._log(f"pm FAK {oid} status {status} ack {dt_ms:.0f} ms -- "
                      f"UNVERIFIED, no trade record yet")
            return Fill(
                slug=slug, side=side, requested_px=p["ceiling"],
                filled_px=p["ceiling"], shares=p["shares"],
                stake=p["shares"] * p["ceiling"],
                fee_paid=0.0, order_id=str(oid),
                ts_ms=int(self._now() * 1000), token_id=p["token_id"],
                raw=f"fak ack {dt_ms:.0f}ms status={status}",
                unverified=True)

        shares, px, fee = got
        self._balance_cache = None
        self._log(f"pm FAK FILL {shares:g} {side} @ {px} ({slug}) "
                  f"ceiling {p['ceiling']} ask {p['ask']} fee {fee:.4f} "
                  f"ack {dt_ms:.0f} ms")
        return Fill(slug=slug, side=side, requested_px=p["ceiling"],
                    filled_px=px, shares=shares, stake=shares * px,
                    fee_paid=fee, order_id=str(oid),
                    ts_ms=int(self._now() * 1000), token_id=p["token_id"],
                    raw=f"fak ack {dt_ms:.0f}ms")

    async def sell_fak(self, *, slug: str, side: str, token_id: str,
                       shares: float, min_px: float | None = None,
                       slip: float = 0.03,
                       quote: dict | None = None) -> Fill | None:
        """Take the BID: sell `shares` for no less than `min_px`. FAK (IOC).

        THE FIRST SELL PATH IN THIS REPO. Every other order here passes
        `side="BUY"`; this venue's SDK has always been able to do the other
        side (`place_market_order(side=Literal["SELL"], shares=..., min_price=)`,
        `polymarket-client` 0.10.0, async_secure.py:2535) and the note that says
        otherwise -- "this venue has no sell, only buy" in `config.py` -- is
        inherited from the PREVIOUS venue, where an unwind path got 37
        `no SELL quote` errors and was deleted as dead code.

        `stake` IS THE PROCEEDS HERE, NOT AN OUTLAY: it is `shares * price`, the
        cash coming IN. Do not hand the returned Fill to `settle()` -- that pays
        a position out against the round's winner, and this position no longer
        exists. The caller books the sale itself (see `Bot._cross_exit_execute`).

        ⛔ THE CONFIRMATION IS THE POSITION SIZE, NOT `_trades_since`, AND THAT
        IS THE WHOLE TRAP. `_trades_since` sums every trade for the token with
        NO SIDE FILTER, and it is read over a rolling minute -- while this exit
        fires a second or two after the entry. A sell confirmed that way would
        read our own BUY back as the fill, book a phantom exit, and leave the
        real position unaccounted. The position list has no such ambiguity: it
        is the venue's own answer to "how much of this token do you hold", which
        can only go DOWN when a sale happens (this arm sells a position once).

        `min_px` is the only risk bound on the order and the venue will not take
        less. When the venue's own fill price cannot be isolated, it is also what
        gets booked -- the mirror of the entry convention, where `filled_px` is
        the CEILING and the stake is an upper bound. Here the floor makes the
        proceeds a LOWER bound and the P&L conservative in both directions.

        ⛔ `min_px=None` MEANS "DERIVE IT FROM THIS BROKER'S OWN BID", AND THAT
        IS THE FIX FOR A LIVE FAILURE. The caller first computed the floor from
        the ARM's websocket book and refused the exit when that book was
        unreadable -- `exit: no fresh bid to sell into`, live 13:08:51, with the
        position then riding to the bell. The broker HAS a fresh book (it is the
        live PM feed, and `Touch.bid` is the authoritative side), so the floor
        is derived here from `t.bid - slip` and rounded UP to the tick. One
        book, one source of truth, and the refusal that remains is the venue's
        own "nothing to take" rather than our own read of a second book.
        """
        return await asyncio.to_thread(
            self._sell_fak_sync, slug=slug, side=side, token_id=token_id,
            shares=shares, min_px=min_px, slip=slip, quote=quote)

    def _sell_fak_sync(self, *, slug: str, side: str, token_id: str,
                       shares: float, min_px: float | None = None,
                       slip: float = 0.03,
                       quote: dict | None = None) -> Fill | None:
        """`sell_fak`'s blocking body. Runs on a worker thread, under the lock
        (see `_buy_fak_sync` for why the lock is not decoration)."""
        with self._client_lock:
            return self._sell_fak_body(slug=slug, side=side, token_id=token_id,
                                       shares=shares, min_px=min_px, slip=slip,
                                       quote=quote)

    def _sell_fak_body(self, *, slug: str, side: str, token_id: str,
                       shares: float, min_px: float | None = None,
                       slip: float = 0.03,
                       quote: dict | None = None) -> Fill | None:
        """Verbatim body of `sell_fak`. Call with `_client_lock` HELD."""
        held = float(shares or 0.0)
        if held <= 0.0:
            self.last_failure = "sell_no_shares"
            self._log(f"pm sell refused {slug} {side}: no shares to sell")
            return None
        # ⛔ SIZE FROM THE VENUE, NOT FROM OUR OWN RECORD. `held` is what the
        # ledger BELIEVES; the position list is what is actually there. If our
        # figure is high (an entry booked at its ceiling size, a partial entry
        # fill), a sell of `held` would still fill only what exists -- and
        # `sold = held - left` would then report a LARGER sale than happened,
        # booking proceeds for shares that were never sold.
        # ⛔ THE LEDGER IS THE AUTHORITY FOR WHAT WE INTEND TO SELL; THE VENUE'S
        # POSITION LIST ONLY CAPS THE SIZE -- and that is a live fix, not a
        # preference. The list lagged a CONFIRMED buy by 60 s (2026-09-18:
        # `pm FAK FILL ... ack 453 ms` at 14:47:03, the list still showing 0
        # shares at 14:47:07, and 1.9607 at 14:48:07), so requiring a positive
        # read before selling made the FAST flips unsellable -- and those are
        # where this mechanism's value is (the replay puts the median flip 1.0 s
        # after the entry). An order whose position we cannot see is still an
        # order: if the shares are genuinely gone the FAK answers
        # `fak_unfilled`, which is a no-fill and retries next tick.
        before = self._position_size_checked(token_id)
        shown = "unreadable" if before is None else f"{before:g}"
        if before is not None and before > 0.0:
            sell_shares = min(held, before)
            if sell_shares < held - 1e-9:
                self._log(f"pm sell {slug} {side}: sizing down {held:g} -> "
                          f"{sell_shares:g} shares to what the venue holds")
        else:
            sell_shares = held
            self._log(f"pm sell {slug} {side}: the venue shows {shown} shares "
                      f"held; selling this fill's {held:g} (the position list "
                      f"lags the fill -- it is a cap, not a permission)")
        # A STALE BOOK IS A REFUSAL, not a price to sell into: the floor is
        # derived from the bid we can SEE, and an old bid would set a floor the
        # live market has already left behind.
        t = self.book.best(token_id, self.cfg.max_book_age_s)
        if t is None or t.bid <= 0:
            self.last_failure = self.last_failure or "no_fresh_book"
            self._log(f"pm sell refused {slug} {side}: no fresh bid "
                      f"(the book goes one-sided near the bell)")
            return None
        bid = float(t.bid)
        r = self.round_for(slug)
        tick = (getattr(r, "tick", None) if r is not None else None) or DEFAULT_TICK
        # ROUNDED UP, not down: the floor is a bound and rounding it down would
        # authorise a price below the one the caller chose as its bound.
        floor = _round_to_tick(t.bid - max(0.0, slip) if min_px is None
                               else min_px, tick, up=True)
        if not (0.0 < floor < bid):
            self.last_failure = "sell_floor_above_bid"
            self._log(f"pm sell refused {slug} {side}: floor {floor:.4f} is not "
                      f"below the bid {bid:.4f} -- nothing to take")
            return None
        c = self._ensure_client()
        try:
            self.warm_now(token_id)
        except Exception:                                     # noqa: BLE001
            pass
        calls = 0
        t0 = time.perf_counter()
        t0_wall = time.time()      # wall clock: `_trades_since` is epoch-based
        try:
            resp = c.place_market_order(
                token_id=token_id, side="SELL", shares=sell_shares,
                min_price=floor, order_type="FAK")
            calls += 1
        except Exception as exc:                              # noqa: BLE001
            self.venue_calls = calls
            dt_ms = (time.perf_counter() - t0) * 1000.0
            name = type(exc).__name__
            # A kill is a NO-FILL; anything else stays loud, because the
            # classes this must not swallow are a schema rejection and a
            # minimum-size rejection -- both of which are how we find out that
            # a 1.7-share position is unsellable on this venue.
            if "RequestRejected" in name or "Liquidity" in name:
                self.last_failure = "fak_unfilled"
                self._log(f"pm SELL no fill {slug} {side} {held:g} sh >= "
                          f"{floor:.4f} (ack {dt_ms:.0f} ms): {exc}")
            else:
                self.last_failure = "post_failed"
                self._log(f"pm SELL FAILED {slug} {side} after {calls} call(s) "
                          f"({dt_ms:.0f} ms): {name} {exc}")
            self.last_order_error = f"{name}: {exc}"
            return None
        dt_ms = (time.perf_counter() - t0) * 1000.0
        self.venue_calls = calls
        if _a(resp, "ok", default=True) is False:
            self.last_failure = "fak_unfilled"
            self.last_order_error = (f"{_a(resp, 'code', default='')} "
                                     f"{_a(resp, 'message', default='')}").strip()
            self._log(f"pm SELL no fill {slug} {side} >= {floor:.4f}: "
                      f"{self.last_order_error} (ack {dt_ms:.0f} ms)")
            return None
        oid = (_a(resp, "order_id", "orderId", default="") or "")
        status = _a(resp, "status", default=None)
        # THE VENUE'S OWN PRICE WHEN IT VOLUNTEERS ONE, the floor otherwise (see
        # the docstring: the floor is the lower bound, so a fallback is honest
        # rather than optimistic).
        px = _a(resp, "avg_price", "average_price", "price", default=None)
        try:
            px = float(px)
        except (TypeError, ValueError):
            px = 0.0
        if not (0.0 < px < 1.0):
            px = floor
        deadline = t0 + self.cfg.confirm_budget_s
        # ⛔ THE AFTER-READ MUST BE ABLE TO SAY "I DO NOT KNOW". `_position_size`
        # answers 0.0 for a FAILED read, and downstream that is arithmetic for
        # "the whole position sold": proceeds booked, slot released, fill
        # dropped -- while the shares are still on-chain and now outside every
        # cap. `_position_size_checked` returns None instead, which is treated
        # as UNVERIFIED (halt), never as a completed sale.
        left = None
        while True:
            left = self._position_size_checked(token_id)
            if left is not None and left < sell_shares - 1e-9:
                break
            if left is None:
                break
            if time.perf_counter() >= deadline:
                break
            time.sleep(0.2)
        sold = 0.0 if left is None else max(0.0, sell_shares - left)
        if left is None or sold <= 1e-9:
            # ---- THE VENUE'S TRADE RECORD, WHICH IS THE ONLY SOURCE THAT IS A
            # FILL RATHER THAN A TOTAL -------------------------------------
            # The position list is a lagging aggregate in BOTH directions, and
            # this is the fallback that sees the sale the list has not caught
            # up with yet. The window starts at the ORDER (`t0_wall`), NOT at
            # the entry, so it cannot read our own buy back as the sale -- the
            # distinction the docstring above is built on.
            got = self._trades_since(token_id, t0_wall)
            if got is not None and got[0] > 0 and got[0] >= sell_shares * 0.5:
                sold = min(got[0], sell_shares)
                if 0.0 < got[1] < 1.0:
                    px = got[1]
                fee = (got[2] if got[2] > 0
                       else taker_fee(px, sold, self.cfg.taker_fee_rate))
                self._balance_cache = None
                self._log(f"pm SELL FILL {sold:g} {side} @ {px} ({slug}) floor "
                          f"{floor} bid {bid} fee {fee:.4f} ack {dt_ms:.0f} ms "
                          f"-- confirmed by the venue's trade record")
                return Fill(slug=slug, side=side, requested_px=floor,
                            filled_px=px, shares=sold, stake=sold * px,
                            fee_paid=fee, order_id=str(oid),
                            ts_ms=int(self._now() * 1000), token_id=token_id,
                            raw=f"sell fak ack {dt_ms:.0f}ms via trades")
            # ⛔ PENDING, NOT UNVERIFIED. The order matched at the venue (its
            # own ack) and nothing is visible yet, so the SALE MAY HAVE
            # HAPPENED -- but calling it unverified halted the watcher, the row
            # was never written, and the fill then rode `settle_due` into a
            # phantom settlement. Every one of today's four exits died here
            # (`status matched ack ~500 ms -- UNVERIFIED, the position list
            # still shows 1.8867 of 1.8867 shares`). Pending keeps the position
            # watched so `confirm_sale` can close it when the venue catches up.
            self.last_failure = "pending_confirm"
            self._log(f"pm SELL {oid} status {status} ack {dt_ms:.0f} ms -- "
                      f"PENDING: matched at the venue, nothing visible yet "
                      f"(the list is behind, not silent); the watcher keeps "
                      f"the position and confirms")
            return Fill(slug=slug, side=side, requested_px=floor,
                        filled_px=px, shares=0.0, stake=0.0, fee_paid=0.0,
                        order_id=str(oid), ts_ms=int(self._now() * 1000),
                        token_id=token_id,
                        raw=f"sell fak ack {dt_ms:.0f}ms pending",
                        pending=True)
        fee = taker_fee(px, sold, self.cfg.taker_fee_rate)
        self._balance_cache = None
        self._log(f"pm SELL FILL {sold:g} {side} @ {px} ({slug}) floor {floor} "
                  f"bid {bid} fee {fee:.4f} ack {dt_ms:.0f} ms")
        return Fill(slug=slug, side=side, requested_px=floor, filled_px=px,
                    shares=sold, stake=sold * px, fee_paid=fee,
                    order_id=str(oid), ts_ms=int(self._now() * 1000),
                    token_id=token_id, raw=f"sell fak ack {dt_ms:.0f}ms")

    def confirm_sale(self, *, token_id: str, shares: float, since_ts: float,
                     fallback_px: float = 0.0
                     ) -> tuple[float, float, float] | None:
        """Did a sale we PLACED but could not SEE actually happen?

        -> `(sold_shares, price, fee)` or None (still unknown; call again).

        This is the other half of the `pending` outcome. The venue's position
        list lags a fill in BOTH directions and by more than a confirm budget
        can cover -- measured 2026-09-18: 0 shares 4 s after a confirmed buy,
        and `still 1.8867 of 1.8867` 0.7 s after a matched sell (the same lag
        this repo measured at 3.5 s on 2026-09-12). So a sale that could not be
        confirmed in the moment is confirmed HERE, later, instead of the
        position being halted into limbo.

        ORDER OF THE TWO PRIMITIVES IS DELIBERATE:
          * the POSITION delta first, because it cannot be confused by our own
            buy (a position can only go DOWN here -- this arm sells once);
          * the TRADE record as the fallback, which carries the venue's REAL
            price and fee, with the window starting at the ORDER (`since_ts`)
            rather than at the entry -- so it cannot read our buy back as the
            sale, which is the trap `_trades_since`'s docstring documents.
        """
        left = self._position_size_checked(token_id)
        got = self._trades_since(token_id, since_ts)
        traded = got[0] if got else 0.0
        px = got[1] if got and 0.0 < got[1] < 1.0 else fallback_px
        fee = got[2] if got and got[2] > 0 else 0.0
        if left is not None and left < shares - 1e-9:
            sold = max(0.0, shares - left)
            if traded > 0:
                sold = min(sold, traded) if traded < sold else sold
            return (sold, px, fee)
        if traded >= shares * 0.5:
            return (min(traded, shares), px, fee)
        return None

    async def buy_limit(self, *, slug: str, side: str, token_id: str,
                        limit_px: float, stake: float, wait_s: float,
                        poll_s: float = 0.75,
                        fill_break: float = 0.999) -> Fill | None:
        """Rest a post-only bid at `limit_px` and wait up to `wait_s` for it.

        SIGNATURE MATCHES `LiveBroker.buy_limit` EXACTLY, and that matters
        more than it looks: `main.py` guards this call with
        `hasattr(broker, "buy_limit")`, so a broker that HAS the method with a
        different signature passes the guard and then raises TypeError on the
        first order. The parameter names are the contract, not a suggestion.

        MEASURED ADVERSE ON BOTH VENUES -- see the maker tests in /memories. A
        resting order fills when the book comes to it, i.e. when the price is
        falling, and realised lands 3-9 pp below the limit paid in every
        configuration tried. It is implemented so the `stale_maker` arm can be
        pointed here and RE-MEASURED, not because it is expected to make money.

        On this venue a resting order is exactly `post_only=True`, which the
        venue rejects above the touch (`invalid post-only order: order crosses
        book`) -- so the price must genuinely be behind the market.
        """
        r = self.round_for(slug)
        if r is None:
            self.last_failure = "no_round"
            return None
        tick = r.tick or DEFAULT_TICK
        limit = _round_to_tick(limit_px, tick, up=True)
        if _below_entry_floor(limit, self.cfg.min_entry_px):
            # The resting path sizes from the limit too, and it can fill at
            # that price later (a taker crossing into us matches a mint pair),
            # so a clamped limit rests here just as expensively as it fills as
            # a taker. Refused BEFORE any venue call.
            self.last_failure = "px_below_floor"
            self._log(
                f"pm entry refused {slug} {side}: limit {limit:.4f} below the "
                f"entry floor {self.cfg.min_entry_px:.2f} (stake {stake:.2f} "
                f"would buy {math.ceil(stake / max(limit, 1e-9)):g} shares)")
            return None
        shares = max(r.min_order or DEFAULT_MIN_SHARES,
                     math.ceil(stake / limit)) if limit > 0 else 0.0
        if shares <= 0 or shares * limit < MIN_NOTIONAL:
            self.last_failure = "below_min_notional"
            return None
        if _size_above_cap(shares, limit, self.cfg.stake_cap_usdt):
            # Same guard as the taker plan, and the same shrink-to-fit rule: a
            # resting order is not cheaper (it can fill later, so the notional
            # is committed either way), but a SMALLER resting order is. Refused
            # here only when no legal size exists, and before any venue call,
            # so nothing is left resting.
            shrunk = _shares_within_cap(shares, limit,
                                        self.cfg.stake_cap_usdt,
                                        r.min_order or DEFAULT_MIN_SHARES)
            if shrunk is None:
                self.last_failure = "size_above_cap"
                self._log(
                    f"pm entry refused {slug} {side}: the venue minimum of "
                    f"{shares:g} shares at {limit:.2f} costs "
                    f"${shares * limit:.2f}, above the stake cap "
                    f"${self.cfg.stake_cap_usdt:.2f} (stake {stake:.2f})")
                return None
            self._log(
                f"pm size fitted to the cap {slug} {side}: {shares:g} -> "
                f"{shrunk:g} shares at {limit:.2f} "
                f"(${shrunk * limit:.2f} <= cap "
                f"${self.cfg.stake_cap_usdt:.2f})")
            shares = shrunk

        c = self._ensure_client()
        tok = r.token(side)
        t0 = time.perf_counter()
        try:
            signed = c.create_limit_order(token_id=tok, price=limit,
                                          size=shares, side="BUY",
                                          post_only=True)
            resp = c.post_order(signed)
        except Exception as exc:                             # noqa: BLE001
            self.last_failure = "post_rejected"
            self.last_order_error = f"{type(exc).__name__}: {exc}"
            self._log(f"pm post-only rejected {slug}: {exc}")
            return None
        dt_ms = (time.perf_counter() - t0) * 1000.0
        oid = _a(resp, "order_id", "orderId", default="")
        # A rejection is not a resting order. See `buy()`: `post_order`
        # returns AcceptedOrder | RejectedOrder.
        if _a(resp, "ok", default=True) is False:
            self.last_failure = "rejected"
            self.last_order_error = (f"{_a(resp, 'code', default='')} "
                                     f"{_a(resp, 'message', default='')}").strip()
            self._log(f"pm post-only REJECTED {slug}: {self.last_order_error}")
            return None

        # A post-only order should REST, and it is crossed into only when a
        # taker decides to. So unlike `buy()`, this waits: measured, a bid 5
        # ticks under the touch was still filled in the same second, and the
        # cancel ack then claimed the order could not be found.
        deadline = t0 + max(0.0, wait_s)
        got = self._trades_since(tok, time.time() - 60.0)
        while got is None or got[0] < shares * fill_break:
            if time.perf_counter() >= deadline:
                break
            await asyncio.sleep(min(poll_s, max(0.05, wait_s)))
            nxt = self._trades_since(tok, time.time() - 60.0)
            if nxt is not None and (got is None or nxt[0] > got[0]):
                got = nxt
        waited_ms = (time.perf_counter() - t0) * 1000.0

        if got is None:
            # Never filled. Cancel so we do not leave a resting order behind
            # -- but do NOT trust the cancel: it is not proof of anything, and
            # `_confirm` below has already run.
            if oid:
                try:
                    c.cancel_order(order_id=str(oid))
                except Exception as exc:                     # noqa: BLE001
                    self._log(f"pm cancel {oid} failed: "
                              f"{type(exc).__name__} {exc}")
            self.last_failure = "limit_unfilled"
            self._log(f"pm limit unfilled {slug} {side} @ {limit} "
                      f"after {waited_ms:.0f} ms")
            return None

        sh, px, fee = got
        self._balance_cache = None
        self._log(f"pm MAKER fill {sh:g}/{shares:g} {side} @ {px} "
                  f"(limit {limit}, {waited_ms:.0f} ms)")
        return Fill(slug=slug, side=side, requested_px=limit, filled_px=px,
                    shares=sh, stake=sh * px,
                    # takerOnly, so a maker fill should carry zero fee; if the
                    # venue charged one, `_trades_since` read it and it wins.
                    fee_paid=fee,
                    order_id=str(oid), ts_ms=int(self._now() * 1000),
                    token_id=tok, raw=f"maker {waited_ms:.0f}ms")


__all__ = ["PmBroker", "PmBrokerConfig", "taker_fee", "TAKER_FEE_RATE"]
