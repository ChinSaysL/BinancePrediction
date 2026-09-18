"""Watch a Polymarket round settle, measure the lag, then redeem.

This measures the number no doc gives us: how long after the bell Polymarket
actually flips a position to REDEEMABLE, and how long the gasless redeem takes to
land. It is the Polymarket half of the "when is the winner known" question that
the Binance wrapper answered with a point snapshot at T+0.02 s.

Logs only on CHANGE -- a 5-minute round is ~all silence, and a busy log would
hide the one transition that matters.

Usage (on the VPS):
    python tools/pm_settle_watch.py btc-updown-5m-1789577400            # watch only
    python tools/pm_settle_watch.py btc-updown-5m-1789577400 --redeem   # redeem when ready
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys
import time

from polymarket import ApiKeyCreds, SecureClient

ENV = pathlib.Path(__file__).resolve().parents[1] / ".env"


def load_env() -> dict[str, str]:
    """Parse .env without exposing values in any log line."""
    txt = ENV.read_text()
    out: dict[str, str] = {}
    for m in re.finditer(r"(?m)^([A-Z_0-9]+)\s*=\s*\"?([^\"\r\n]*)\"?\s*$", txt):
        out[m.group(1)] = m.group(2).strip()
    return out


def client(env: dict[str, str]) -> SecureClient:
    return SecureClient.create(
        private_key=env["POLYMARKET_PRIVATE_KEY"],
        wallet=env["POLYMARKET_FUNDER_ADDRESS"],
        credentials=ApiKeyCreds(
            key=env["POLYMARKET_API_KEY"],
            secret=env["POLYMARKET_API_SECRET"],
            passphrase=env["POLYMARKET_PASSPHRASE"],
        ),
    )


def utc(ts: float | None = None) -> str:
    return dt.datetime.fromtimestamp(
        ts if ts is not None else time.time(), dt.timezone.utc
    ).strftime("%H:%M:%S.%f")[:-3]


def position(c: SecureClient, condition_id: str):
    """The position for this condition, or None."""
    for page in c.list_positions(condition_id=condition_id):
        for item in page.items:
            return item
    return None


def state(c: SecureClient, condition_id: str) -> dict:
    p = position(c, condition_id)
    if p is None:
        return {"status": "NO_POSITION", "value": None, "size": None, "pnl": None}
    return {
        "status": getattr(p.status, "value", str(p.status)),
        "value": p.current_value,
        "size": p.current_size,
        "pnl": p.total_pnl,
        "redeemable": p.redeemable,
    }


def redeem_of(c: SecureClient, condition_id: str):
    """The REDEEM activity for this condition, if the AutoRedeemer already ran."""
    try:
        for page in c.list_activity(activity_types=["REDEEM"], page_size=20):
            for a in page.items:
                if a.condition_id == condition_id:
                    return a
            break
    except Exception as exc:
        print("%s  redeem lookup error: %s: %s" % (utc(), type(exc).__name__, exc))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--redeem", action="store_true")
    args = ap.parse_args()

    env = load_env()
    c = client(env)

    m = c.get_market(slug=args.slug)
    cond = m.condition_id
    end_s = int(args.slug.rsplit("-", 1)[-1]) + 300
    end = dt.datetime.fromtimestamp(end_s, dt.timezone.utc)

    print("%s  round %s ends %sZ  (condition %s…)" % (utc(), args.slug, end.strftime("%H:%M:%S"), cond[:12]))
    print("%s  position at start: %r" % (utc(), state(c, cond)))

    # Already finished before we got here? Then there is nothing to watch, and
    # waiting for a REDEEMABLE transition that already happened would just burn
    # the whole budget.
    if state(c, cond)["status"] == "NO_POSITION":
        a = redeem_of(c, cond)
        if a is not None:
            print("%s  already settled and swept: REDEEM %sZ  tx %s  amount %s"
                  % (utc(), a.timestamp.strftime("%H:%M:%S"), a.transaction_hash, a.amount))
            print("%s  collateral: %s" % (utc(), c.get_balance_allowance(asset_type="COLLATERAL").balance))
            return 0

    last: dict | None = None
    settled_at: float | None = None
    deadline = time.time() + args.minutes * 60

    while time.time() < deadline:
        try:
            st = state(c, cond)
        except Exception as exc:  # a poll blip must not kill the watch
            print("%s  poll error: %s: %s" % (utc(), type(exc).__name__, exc))
            time.sleep(args.interval)
            continue

        if st != last:
            lag = ""
            if settled_at is not None:
                lag = "  (+%.1fs after the bell)" % (settled_at - end_s)
            print("%s  %s%s" % (utc(), st, lag))
            if st["status"] == "REDEEMABLE" and last is not None and last["status"] != "REDEEMABLE":
                settled_at = time.time()
                print("%s  === SETTLED: REDEEMABLE %.1fs after the bell ===" % (utc(), settled_at - end_s))
            last = st

        if st["status"] == "REDEEMABLE":
            break
        time.sleep(args.interval)

    else:
        print("%s  gave up after %.0f min (still %r)" % (utc(), args.minutes, (last or {}).get("status")))
        return 1

    if not (last and last["status"] == "REDEEMABLE" and args.redeem):
        return 0

    before = c.get_balance_allowance(asset_type="COLLATERAL").balance
    print("%s  collateral at settle: %s" % (utc(), before))

    # The AutoRedeemer (0xa1200000d0002264C9a1698e001292D00E1b00af, the ERC-1155
    # operator granted by setup_trading_approvals) usually sweeps the winner
    # within seconds of REDEEMABLE, so the position is often ALREADY GONE and
    # redeem_positions raises a misleading "No market found". Watch for the
    # position to disappear FIRST, and only redeem what survives.
    t0 = time.time()
    while time.time() - t0 < 90:
        try:
            if state(c, cond)["status"] == "NO_POSITION":
                print("%s  position gone after %.1fs -> AutoRedeemer swept it" % (utc(), time.time() - t0))
                break
        except Exception as exc:
            print("%s  poll error: %s: %s" % (utc(), type(exc).__name__, exc))
        time.sleep(args.interval)
    else:
        print("%s  position still open after 90s -> redeeming manually" % utc())
        try:
            out = c.redeem_positions(condition_id=cond).wait()
            print("%s  redeem wait -> %s" % (utc(), out))
        except Exception as exc:
            print("%s  redeem failed: %s: %s" % (utc(), type(exc).__name__, exc))

    after = c.get_balance_allowance(asset_type="COLLATERAL").balance
    print("%s  collateral %s -> %s  (delta %s)" % (utc(), before, after, after - before))

    try:
        a = redeem_of(c, cond)
        if a is not None:
            print("%s  REDEEM at %sZ  tx %s  amount %s"
                  % (utc(), a.timestamp.strftime("%H:%M:%S"), a.transaction_hash, a.amount))
    except Exception as exc:
        print("%s  activity error: %s: %s" % (utc(), type(exc).__name__, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
