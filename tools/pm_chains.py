"""Where are the funds? Read the same address on every chain Polymarket uses.

READ ONLY, no keys, no auth.

WHY: a deposit sent to the Polymarket address on the WRONG CHAIN is invisible on
Polygon and looks identical to "the deposit never arrived" -- and Polymarket's
own UI offers deposits from Ethereum, Base and Arbitrum, which it then pulls to
Polygon through a relayer. If the relayer has not run (or the transfer went
straight to an address instead of through the UI), the USDC sits on the source
chain and every Polygon-side check says zero.

So this prints, per chain: whether the address is a contract (a Polymarket proxy
is one), and the USDC balance in each of that chain's USDC contracts. An all-zero
row is a real answer -- it means look at the exchange/bridge side instead.

RPC notes, both learned the hard way: every one of these endpoints 403s a request
without a browser User-Agent, and `polygon-rpc.com` / `blockpi` refuse this host
outright regardless.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0.0.0 Safari/537.36")

#: chain -> (rpc, {label: usdc contract}). Only contracts that exist on that
#: chain: asking Base for USDC.e returns a plausible-looking zero.
CHAINS = {
    "polygon": ("https://polygon-bor-rpc.publicnode.com", 6, {
        "USDC.e(collateral)": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        "USDC(native)": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"}),
    "ethereum": ("https://ethereum-rpc.publicnode.com", 6, {
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"}),
    "base": ("https://base-rpc.publicnode.com", 6, {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}),
    "arbitrum": ("https://arbitrum-one-rpc.publicnode.com", 6, {
        "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "USDC.e": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8"}),
    "optimism": ("https://optimism-rpc.publicnode.com", 6, {
        "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"}),
}


def load_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def rpc(url: str, method: str, params: list, timeout: float = 15.0):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": _UA,
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        d = json.loads(fh.read().decode())
    return d.get("result")


def balance(url: str, token: str, holder: str, dec: int) -> str:
    data = "0x70a08231" + holder.lower().replace("0x", "").rjust(64, "0")
    r = rpc(url, "eth_call", [{"to": token, "data": data}, "latest"])
    if not isinstance(r, str) or not r.startswith("0x"):
        return "  ?  "
    return format(int(r, 16) / (10 ** dec), ".6f")


def kind(url: str, addr: str) -> str:
    r = rpc(url, "eth_getCode", [addr, "latest"])
    if not isinstance(r, str):
        return "unreachable"
    return "CONTRACT (proxy)" if len(r) > 2 else "EOA"


def main() -> None:
    env = load_env(ENV)
    addrs = {}
    pk = env.get("POLYMARKET_PRIVATE_KEY")
    if pk:
        try:
            from eth_account import Account
            addrs["EOA (from key)"] = Account.from_key(
                pk if pk.startswith("0x") else "0x" + pk).address
        except Exception as e:                            # noqa: BLE001
            print("could not derive the EOA: %s" % e)
    if env.get("POLYMARKET_FUNDER_ADDRESS"):
        f = env["POLYMARKET_FUNDER_ADDRESS"]
        addrs["funder in .env"] = f if f.startswith("0x") else "0x" + f
    for extra in sys.argv[1:]:
        addrs["argv %s" % extra[:8]] = extra
    if not addrs:
        print("no addresses: set POLYMARKET_* in .env or pass them as arguments")
        return

    for label, a in addrs.items():
        print("=== %s  %s" % (label, a))
        for chain, (url, dec, tokens) in CHAINS.items():
            try:
                k = kind(url, a)
            except Exception as e:                        # noqa: BLE001
                print("  %-9s rpc failed (%s)" % (chain, type(e).__name__))
                continue
            row = []
            for tlab, tok in tokens.items():
                try:
                    row.append("%s %s" % (tlab, balance(url, tok, a, dec)))
                except Exception:                         # noqa: BLE001
                    row.append("%s ?" % tlab)
            print("  %-9s %-16s %s" % (chain, k, "   ".join(row)))
        print()


if __name__ == "__main__":
    main()
