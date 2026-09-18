#!/usr/bin/env python3
"""Audit the REAL Polymarket SDK surface against what `broker.py` calls.

WHY THIS EXISTS. `broker.py` reaches into the SDK with a few defensive
`getattr`s, which is the right call for field renames but the WRONG call for
method names: a method that does not exist does not raise where you would
notice, it turns a fill into an "unverified" order, which `settle()` then
VOIDS, which books zero P&L for every trade. Silent, and it looks like the
venue never filled us.

Run this after ANY `pip install -U polymarket-client`, and before any deploy.

    .venv/bin/python tools/pm_sdk_audit.py

Read-only: constructs nothing, places nothing, needs no key.
"""
from __future__ import annotations

import inspect
import sys

#: (name, why broker.py needs it). ABSENT is a hard failure.
BROKER_CALLS = [
    ("create_limit_order", "RULE 4: sign locally (~5 ms) before posting"),
    ("post_order", "the hot-path POST (~275 ms)"),
    ("get_balance_allowance", "collateral read; must be OFF the hot path"),
    ("list_positions", "the ONLY trustworthy post-trade evidence"),
    ("list_account_trades", "fallback confirmation by token id"),
    ("list_open_orders", "active_orders()"),
    ("cancel_order", "cancel_orders()"),
    ("redeem_positions", "redeem(); usually a no-op (AutoRedeemer)"),
    ("close", "PmBroker.close()"),
]

#: Nice to have; their absence degrades a path rather than breaking it.
OPTIONAL = [
    ("place_market_order", "taker FOK/FAK with an explicit max_price"),
    ("place_limit_order", "sign+post in one (~797 ms; NOT for the hot path)"),
    ("wait_for_order_fill_settlement", "venue-side confirmation primitive"),
    ("get_trading_approvals_state", "pre-flight allowance check"),
]


def show(name: str) -> str:
    try:
        from polymarket import SecureClient
    except ImportError as e:                                 # pragma: no cover
        return f"IMPORT FAILED: {e}"
    a = getattr(SecureClient, name, None)
    if a is None:
        return "*** ABSENT ***"
    try:
        return str(inspect.signature(a))
    except (TypeError, ValueError):
        return "(signature unavailable)"


def model_fields(cls_path: str) -> list[str]:
    mod, _, cls = cls_path.rpartition(".")
    try:
        m = __import__(mod, fromlist=[cls])
        c = getattr(m, cls)
    except Exception as e:                                   # noqa: BLE001
        return [f"<{type(e).__name__}: {e}>"]
    fields = getattr(c, "model_fields", None)
    if fields is None:
        fields = {f: None for f in getattr(c, "__slots__", ())}
    return sorted(fields)


def main() -> int:
    try:
        import polymarket as P
        print(f"polymarket SDK version: {getattr(P, '__version__', '?')}")
    except ImportError as e:
        print(f"cannot import polymarket: {e}")
        return 2
    from polymarket import SecureClient

    print("\n=== REQUIRED BY broker.py ===")
    rc = 0
    for name, why in BROKER_CALLS:
        sig = show(name)
        bad = "ABSENT" in sig
        rc |= 1 if bad else 0
        print(f"  {'FAIL' if bad else 'ok  '}  {name}")
        print(f"          {why}")
        print(f"          {sig}")

    print("\n=== OPTIONAL ===")
    for name, why in OPTIONAL:
        sig = show(name)
        print(f"  {'MISS' if 'ABSENT' in sig else 'ok  '}  {name}")
        print(f"          {why}")
        print(f"          {sig}")

    print("\n=== NAMES THAT ARE EASY TO GET WRONG ===")
    names = {n for n in dir(SecureClient) if not n.startswith("_")}
    for near in ("list_orders", "positions", "orders", "open_orders",
                 "get_positions", "trades", "account_trades"):
        hits = sorted(n for n in names if near in n)
        print(f"  {near:18s} -> {hits or 'no match'}")

    print("\n=== MODEL FIELDS (what the code must read) ===")
    for cls in ("polymarket.models.data.portfolio.Position",
                "polymarket.models.clob.account.ClobTrade",
                "polymarket.models.clob.account.OpenOrder",
                "polymarket.models.clob.order_response.AcceptedOrder",
                "polymarket.models.clob.order_response.RejectedOrder"):
        print(f"  {cls.rpartition('.')[2]:16s} {model_fields(cls)}")

    # A paginator is not a list. broker.py does `getattr(res, "positions", res)`
    # and then iterates -- verify that iteration is what actually happens.
    try:
        from polymarket.pagination import Paginator
        print(f"\n  Paginator is iterable: {hasattr(Paginator, '__iter__')}"
              f"   has __len__: {hasattr(Paginator, '__len__')}")
    except ImportError:
        print("\n  Paginator class not found where expected")

    print(f"\nRESULT: {'FAILURES PRESENT' if rc else 'all required methods present'}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
