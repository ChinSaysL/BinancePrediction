#!/usr/bin/env python3
"""Where does the Polymarket order latency actually go?

The 797 ms "order ack" was measured once, with no breakdown, and every number
in this repo before it was the wrong number. This tool prices every phase of
the order path so the broker can be built around the real breakdown instead of
a guess.

MEASURED BASELINE (2026-09-16, Contabo Tokyo -> Cloudflare NRT edge):
    edge-only request (never touches the origin)   22-29 ms
    any clob.polymarket.com API call              260-283 ms
    ... identical from Marseille (205-304 ms)
    ... identical on IPv4 (262) and IPv6 (255)
    ... identical on a warm keep-alive connection (258 -> 237 steady)
So the ~240 ms is a property of the API, NOT of our VPS, our resolver, our IP
family, or our TLS setup. Moving the box does not help: Marseille measured the
same as Tokyo. What we control is HOW MANY of those we pay on the hot path.

That is what this tool measures, and the number to beat is the SUM of the
sequential calls the broker makes per order, not any single one of them.

Read-only by default. With --live it places ONE post-only order far below the
touch (it cannot cross, so it cannot fill) and cancels it, purely to time the
real POST. Nothing else is written.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
import time
from pathlib import Path

from polymarket import ApiKeyCreds, RelayerApiKey, SecureClient

CLOB = "https://clob.polymarket.com"
ROUND_S = 300


def percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    i = min(len(xs) - 1, int(q * len(xs)))
    return xs[i]


def report(name: str, ms: list[float]) -> None:
    if not ms:
        print(f"  {name:<34} --")
        return
    print(f"  {name:<34} n={len(ms):<3} "
          f"med {st.median(ms):7.1f}  p90 {percentile(ms, 0.9):7.1f}  "
          f"min {min(ms):7.1f}  max {max(ms):7.1f}")


class Timer:
    def __init__(self) -> None:
        self.ms: list[float] = []

    def __call__(self, fn, *a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            self.ms.append((time.perf_counter() - t0) * 1000.0)


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"missing {name} in the environment")
    return v


def build_client() -> SecureClient:
    return SecureClient.create(
        private_key=env("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ.get("POLYMARKET_FUNDER_ADDRESS") or None,
        credentials=ApiKeyCreds(
            key=env("POLYMARKET_API_KEY"),
            secret=env("POLYMARKET_API_SECRET"),
            passphrase=env("POLYMARKET_PASSPHRASE"),
        ),
        api_key=RelayerApiKey(
            key=env("POLYMARKET_RELAYER_API"),
            address=env("POLYMARKET_RELAYER_ADDRESS"),
        ),
    )


def btc_slug(now: float | None = None) -> str:
    """The CURRENT btc 5m round -- the only market we trade."""
    t = int(now if now is not None else time.time())
    return f"btc-updown-5m-{t - t % ROUND_S}"


def _get(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def token_ids(market) -> tuple[str, str]:
    """(yes_token, no_token).

    The SDK returns `outcomes` as a pydantic MarketOutcomes with `.yes`/`.no`
    MarketOutcome children; older/other shapes use a dict or a flat list. All
    three are handled, because guessing this wrong costs a 240 ms round trip
    to discover.
    """
    out = _get(market, "outcomes")

    yes = _get(out, "yes")
    no = _get(out, "no")
    if yes is not None and no is not None:
        y, n = _get(yes, "token_id") or yes, _get(no, "token_id") or no
        if y and n:
            return str(y), str(n)

    if isinstance(out, dict) and len(out) >= 2:
        vals = [_get(v, "token_id") or v for v in out.values()][:2]
        if all(vals):
            return str(vals[0]), str(vals[1])

    if isinstance(out, (list, tuple)) and len(out) >= 2:
        vals = [_get(v, "token_id") or v for v in out][:2]
        if all(vals):
            return str(vals[0]), str(vals[1])

    for attr in ("clob_token_ids", "clobTokenIds", "token_ids", "tokens"):
        v = _get(market, attr)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                continue
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            got = [_get(x, "token_id") or x for x in v[:2]]
            if all(got):
                return str(got[0]), str(got[1])

    raise SystemExit(f"cannot find token ids on {type(market).__name__}; "
                     f"outcomes={out!r}")


def market_facts(market) -> dict:
    """The three numbers the broker must not re-fetch per order."""
    tr = _get(market, "trading") or {}
    return {
        "tick_size": str(_get(tr, "minimum_tick_size") or "0.01"),
        "min_size": str(_get(tr, "minimum_order_size") or "5"),
        "fees_enabled": _get(tr, "fees_enabled"),
        "condition_id": _get(market, "condition_id"),
        "slug": _get(market, "slug"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5,
                    help="how many times to repeat the read phases")
    ap.add_argument("--live", action="store_true",
                    help="also place+cancel one post-only order to time the POST")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--json-out", default="data/pm_latency.json")
    a = ap.parse_args()

    timings: dict[str, list[float]] = {}
    for k in ("client_build", "raw_http_baseline", "raw_http_origin",
              "get_market_gamma", "get_order_book", "get_balance",
              "sign_local", "post_order", "cancel_order"):
        timings[k] = []
    t = Timer()

    # ---------------------------------------------------------------- client
    t0 = time.perf_counter()
    client = build_client()
    timings["client_build"].append((time.perf_counter() - t0) * 1000)

    # ------------------------------------------------------- round discovery
    slug = btc_slug()
    t(lambda: client.get_market(slug=slug))
    timings["get_market_gamma"] = t.ms[:]
    t.ms.clear()
    market = client.get_market(slug=slug)
    yes_id, no_id = token_ids(market)
    facts = market_facts(market)
    print(f"round {slug}\n  yes token {yes_id}\n  no  token {no_id}")
    print(f"  tick {facts['tick_size']}  min size {facts['min_size']}  "
          f"fees {facts['fees_enabled']}\n")

    # --------------------------------------------------- warm the connection
    # A cold connection pays TCP+TLS on the first call; we never want the
    # FIRST order of a round to be that call.
    for _ in range(a.warmup):
        t(lambda: client.get_order_book(token_id=yes_id))
    warm = t.ms[:]
    t.ms.clear()
    print("warm-up (discarded, first call is the cold one)")
    report("get_order_book (cold->warm)", warm)
    print()

    # -------------------------------------------------------- read baseline
    for _ in range(a.rounds):
        t(lambda: client.get_order_book(token_id=yes_id))
    timings["get_order_book"] = t.ms[:]
    t.ms.clear()

    for _ in range(a.rounds):
        t(lambda: client.get_balance_allowance(asset_type="COLLATERAL"))
    timings["get_balance"] = t.ms[:]
    t.ms.clear()

    for _ in range(max(2, a.rounds // 2)):
        t(lambda: client.get_market(slug=slug))
    timings["get_market_gamma"] = t.ms[:]
    t.ms.clear()

    # a bare request to the same host, no auth, tiny body: the floor.
    # /cdn-cgi/trace is served BY THE EDGE and never reaches the origin, so
    # the gap between it and /time IS the origin trip.
    try:
        import httpx
        c = httpx.Client(http2=True)
        c.get(f"{CLOB}/cdn-cgi/trace")
        for _ in range(3):
            t(lambda: c.get(f"{CLOB}/cdn-cgi/trace"))
        timings["raw_http_baseline"] = t.ms[:]
        t.ms.clear()
        for _ in range(3):
            t(lambda: c.get(f"{CLOB}/time"))
        timings["raw_http_origin"] = t.ms[:]
        t.ms.clear()
        c.close()
    except Exception as exc:                                   # noqa: BLE001
        print("  raw baseline failed:", exc)

    report("edge-only /cdn-cgi/trace", timings["raw_http_baseline"])
    report("origin /time (no auth)", timings.get("raw_http_origin", []))
    report("get_market (gamma)", timings["get_market_gamma"])
    report("get_order_book (clob)", timings["get_order_book"])
    report("get_balance (clob)", timings["get_balance"])

    # ------------------------------------------------------------ the order
    # Price the probe off GAMMA, not the CLOB book: it is the same data, it is
    # already in hand, and it costs 10 ms instead of 238 ms. That substitution
    # is the whole optimisation in miniature.
    pr = _get(market, "prices") or {}
    best_bid = float(_get(pr, "best_bid") or 0)
    best_ask = float(_get(pr, "best_ask") or 0)
    px = round(max(0.01, (best_ask or 0.60) - 0.10), 2)
    size = max(5, math.ceil(1.0 / px))
    print(f"\n  gamma bid {best_bid} ask {best_ask}  ->  probe {size} shares "
          f"at {px} = ${size * px:.2f} (post-only, below the touch)")

    if not a.live:
        print("\n  read-only run; add --live to time the POST")
        return 0

    # PRIME THE SIGNER. The first create_limit_order in a process pays for
    # whatever it lazily initialises: measured 504 ms cold, 4.8-5.0 ms warm.
    # That cold cost must never land on the hot path, so it is paid here.
    t0 = time.perf_counter()
    try:
        client.create_limit_order(token_id=yes_id, price=px, size=size,
                                  side="BUY", post_only=True)
    except Exception as exc:                                   # noqa: BLE001
        print("  prime failed:", type(exc).__name__, exc)
    cold_sign = (time.perf_counter() - t0) * 1000.0
    print(f"  signer prime (the 500ms cold call): {cold_sign:.0f} ms")

    for i in range(3):
        t(lambda: client.create_limit_order(
            token_id=yes_id, price=px, size=size, side="BUY", post_only=True))
    timings["sign_local"] = t.ms[:]
    t.ms.clear()
    report("create_limit_order (WARM, local)", timings["sign_local"])

    resp = None
    signed = None
    for attempt in range(5):
        signed = client.create_limit_order(
            token_id=yes_id, price=px, size=size, side="BUY", post_only=True)
        try:
            resp = t(lambda: client.post_order(signed))
            break
        except Exception as exc:                               # noqa: BLE001
            t.ms.clear()          # only a successful post is a measurement
            if "cross" not in str(exc).lower():
                raise
            px = round(max(0.01, px - 0.10), 2)
            size = max(5, math.ceil(1.0 / px))
            print(f"   crossed; retrying {size} @ {px}")
    timings["post_order"] = t.ms[:]
    t.ms.clear()
    report("post_order (the 240ms one)", timings["post_order"])

    oid = getattr(resp, "order_id", None) or getattr(resp, "orderId", None)
    status = getattr(resp, "status", None) or getattr(resp, "success", None)
    print(f"   -> status {status} order {oid}")

    if oid:
        t(lambda: client.cancel_order(order_id=str(oid)))
        timings["cancel_order"] = t.ms[:]
        t.ms.clear()
        report("cancel_order", timings["cancel_order"])

    # ------------------------------------------------- the hot-path verdict
    print()
    print("  HOT PATH: one order, no per-order discovery")
    hot = (st.median(timings["sign_local"]) if timings["sign_local"] else 0.0)
    hot += st.median(timings["post_order"]) if timings["post_order"] else 0.0
    print(f"    sign {hot - (st.median(timings['post_order']) if timings['post_order'] else 0):.0f} ms"
          f"  +  post {st.median(timings['post_order']) if timings['post_order'] else 0:.0f} ms"
          f"  =  {hot:.0f} ms")
    cold = hot + st.median(timings["get_market_gamma"]) \
        + st.median(timings["get_order_book"]) + st.median(timings["get_balance"])
    print(f"    the same order done the naive way (discovery + book + balance"
          f" first): {cold:.0f} ms")
    print(f"    -> skipping the three reads saves {cold - hot:.0f} ms")

    Path(a.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.json_out).write_text(json.dumps(
        {k: v for k, v in timings.items() if v}, indent=2))
    print(f"\n  wrote {a.json_out}")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
