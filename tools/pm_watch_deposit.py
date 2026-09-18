"""Watch the Polymarket deposit land, instead of guessing about it.

The situation this exists for (2026-09-16): the UI showed $10 cash while the
wallet held 0 on-chain, because the deposit went through an off-chain payment
rail (Bybit Pay). The venue credits the balance immediately; the proxy wallet's
USDC only appears when the settlement lands. Both are true at once and only a
watcher can say when the second one happens.

It polls, on a fixed cadence:
  * the on-chain USDC.e / USDC balance of every candidate address,
  * the venue's own view (positions value + activity count).
and logs a line ONLY when a number changes, plus a heartbeat every 10th sample.
Quiet by design: a log that prints the same zero every 30s is a log nobody reads.

READ ONLY. No auth, no keys, no orders.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

ADDRS = {
    "API/UI wallet 0xC07A": "0xC07A686E2fb9c062c733cD6fd1640a0C03Aa0e68",
    "funder 0x1d7f":        "0x1d7f38d7c72a66a0d67b85190D86945a03602802",
    "EOA from .env key":    "0x0B5505ad4ff7c37df4809a92950C8a18ee34957F",
}
USDC = {"USDC.e": ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6),
        "USDC":   ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6)}
RPC = "https://polygon-bor-rpc.publicnode.com"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126 Safari/537.36",
      "Accept": "application/json", "Content-Type": "application/json"}

INTERVAL_S = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
MINUTES = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0


def call(method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as fh:
            return json.loads(fh.read().decode()).get("result")
    except Exception:                                     # noqa: BLE001
        return None


def balance(token: str, dec: int, holder: str):
    data = "0x70a08231" + holder.lower().replace("0x", "").rjust(64, "0")
    r = call("eth_call", [{"to": token, "data": data}, "latest"])
    try:
        return int(r, 16) / (10 ** dec)
    except (TypeError, ValueError):
        return None


def venue(addr: str):
    try:
        req = urllib.request.Request(
            "https://data-api.polymarket.com/value?user=" + addr, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as fh:
            v = json.loads(fh.read().decode())
        val = v[0].get("value") if isinstance(v, list) and v else None
    except Exception:                                     # noqa: BLE001
        val = None
    try:
        req = urllib.request.Request(
            "https://data-api.polymarket.com/activity?user=%s&limit=5" % addr,
            headers=UA)
        with urllib.request.urlopen(req, timeout=20) as fh:
            a = json.loads(fh.read().decode())
        n = len(a) if isinstance(a, list) else None
    except Exception:                                     # noqa: BLE001
        n = None
    return val, n


def sample() -> dict:
    out = {}
    for lab, a in ADDRS.items():
        row = {}
        for tlab, (tok, dec) in USDC.items():
            row[tlab] = balance(tok, dec, a)
        val, n = venue(a)
        row["value"] = val
        row["events"] = n
        out[lab] = row
    return out


def main() -> None:
    prev = None
    n = 0
    deadline = time.time() + MINUTES * 60
    print("watching %d addresses every %.0fs for %.0f min (read-only)"
          % (len(ADDRS), INTERVAL_S, MINUTES), flush=True)
    while time.time() < deadline:
        n += 1
        cur = sample()
        if cur != prev:
            ts = time.strftime("%H:%M:%S", time.gmtime())
            if prev is not None:
                print("[%s] *** CHANGE ***" % ts, flush=True)
            for lab in ADDRS:
                r = cur[lab]
                print("[%s] %-22s USDC.e %-10s USDC %-10s value %-6s events %s"
                      % (ts, lab, r["USDC.e"], r["USDC"], r["value"], r["events"]),
                      flush=True)
            hot = [lab for lab in ADDRS
                   if (cur[lab]["USDC.e"] or 0) + (cur[lab]["USDC"] or 0) > 0]
            if hot:
                print("[%s] *** FUNDS FOUND AT: %s ***" % (ts, ", ".join(hot)),
                      flush=True)
            prev = cur
        elif n % 10 == 0:
            print("[%s] heartbeat: still all zero (%d samples)"
                  % (time.strftime("%H:%M:%S", time.gmtime()), n), flush=True)
        time.sleep(INTERVAL_S)
    print("watcher finished after %.0f min" % MINUTES, flush=True)


if __name__ == "__main__":
    main()
