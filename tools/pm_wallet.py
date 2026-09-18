"""Which Polymarket wallet holds the money, and do the .env creds belong to it?

READ ONLY. This tool never posts an order and never signs anything that leaves
the process: it reads addresses off the chain and asks the CLOB for balances.

WHY IT EXISTS
-------------
`POLYMARKET_SIGNATURE_TYPE` is not a preference, it is a claim about WHERE the
USDC sits, and getting it wrong does not error -- it quietly reports a balance
of zero, because the API reads whichever address the claim names:

    type 0  EOA                  the funds are IN the wallet the private key
                                 controls; maker == taker == EOA
    type 1  POLY_PROXY           the funds are in the proxy the Polymarket site
                                 creates per user; the EOA controls it
    type 2  POLY_GNOSIS_SAFE     same idea, Gnosis Safe flavour

So the check is empirical: read the on-chain balance of BOTH the derived EOA and
the configured funder, then ask the CLOB for the collateral balance under each
signature type. Exactly one combination should show the money, and that is the
correct value -- not the one that makes the code run.

It also settles a second failure that looks identical from the outside: L2 API
credentials derived from a DIFFERENT wallet than the private key. The suite
re-derives the key's own creds (L1) and compares them to .env, so "the creds are
for another account" is distinguished from "the signature type is wrong".
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

#: Polymarket's collateral is **pUSD**, not USDC.e (docs `resources/contracts`).
#: Reading only USDC.e is what made a settled $10 deposit look missing for
#: hours on 2026-09-16 -- pUSD carries the balance and USDC.e is the empty one.
#: USDC.e / native USDC stay in the list only to tell "wrong token" apart from
#: "wrong wallet", which is the distinction that actually cost the time.
USDC = {
    "pUSD (collateral)": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
    "USDC.e (legacy)": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "USDC (native)": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
}
#: Public Polygon RPCs that actually answer. Two of the usual suspects
#: (`polygon-rpc.com`, `blockpi`) refuse this host outright, and every one of
#: them 403s a request without a browser User-Agent -- the same trap as the
#: gamma API. Verified 2026-09-16 from both the local box and the VPS.
RPCS = ("https://polygon-bor-rpc.publicnode.com",
        "https://polygon.drpc.org",
        "https://1rpc.io/matic")
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0.0.0 Safari/537.36")

#: Polymarket's own read API. Preferred over an RPC for the "which address holds
#: the money" question, because it needs no node and reports the VENUE's view of
#: a wallet -- which is the view the trading path will act on.
DATA_API = "https://data-api.polymarket.com"


def data_api(path: str):
    try:
        req = urllib.request.Request(DATA_API + path, headers={
            "User-Agent": "Mozilla/5.0 Chrome/126",
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as fh:
            return json.loads(fh.read().decode())
    except Exception:                                     # noqa: BLE001
        return None


def wallet_snapshot(addr: str) -> dict:
    """What the venue thinks this address holds.

    This is the field that identifies the real funder: a wallet with positions
    or value is the one the funds sit in, whether or not .env agrees.
    """
    out = {"value": None, "positions": None, "sample": []}
    v = data_api("/value?user=" + addr)
    if isinstance(v, list) and v:
        out["value"] = v[0].get("value")
    elif isinstance(v, dict):
        out["value"] = v.get("value")
    p = data_api("/positions?user=%s&sizeThreshold=0&limit=500" % addr)
    if isinstance(p, list):
        out["positions"] = len(p)
        out["sample"] = [(str(x.get("title"))[:30], x.get("outcome"),
                           x.get("size"), x.get("currentValue")) for x in p[:4]]
    return out


def load_env(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def rpc(method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    last = None
    for url in RPCS:
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json",
                                        "User-Agent": _UA,
                                        "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as fh:
                d = json.loads(fh.read().decode())
            if "result" in d:
                return d["result"]
            last = d.get("error")
        except Exception as e:                            # noqa: BLE001
            last = e
    return {"__error__": str(last)}


def balance_of(token: str, holder: str) -> float | None:
    data = "0x70a08231" + holder.lower().replace("0x", "").rjust(64, "0")
    r = rpc("eth_call", [{"to": token, "data": data}, "latest"])
    if not isinstance(r, str) or not r.startswith("0x"):
        return None
    try:
        return int(r, 16) / 1e6
    except ValueError:
        return None


def is_contract(addr: str) -> bool | None:
    r = rpc("eth_getCode", [addr, "latest"])
    if not isinstance(r, str):
        return None
    return len(r) > 2


def derive_and_write(pk: str, env_path: Path) -> None:
    """Derive API creds for THIS key and write them into .env (backup first).

    The mismatch this exists for: L2 API credentials are issued per wallet, and
    pairing one wallet's creds with another wallet's key is not a permissions
    problem -- it is a 401 on every single call, which reads like one. Deriving
    fresh creds from the key we hold makes the pair self-consistent.

    It is still only half the fix: the key must be the one that CONTROLS the
    funder. If it is not, the creds will authenticate and the balance will be
    zero, which the caller can tell apart from a 401.
    """
    import time
    from eth_account import Account
    from py_clob_client.client import ClobClient

    creds = ClobClient(host=HOST, key=pk, chain_id=CHAIN_ID) \
        .create_or_derive_api_creds()
    text = env_path.read_text()
    bak = env_path.with_name(env_path.name + ".bak-"
                             + time.strftime("%Y%m%d-%H%M%S") + "-pre-creds")
    bak.write_text(text)

    new = {"POLYMARKET_API_KEY": creds.api_key,
           "POLYMARKET_API_SECRET": creds.api_secret,
           "POLYMARKET_PASSPHRASE": creds.api_passphrase}
    out, seen = [], set()
    for line in text.splitlines():
        k = line.split("=", 1)[0].strip() if "=" in line else ""
        if k in new:
            out.append("%s=%s" % (k, new[k]))
            seen.add(k)
        else:
            out.append(line)
    for k, v in new.items():
        if k not in seen:
            out.append("%s=%s" % (k, v))
    env_path.write_text("\n".join(out) + "\n")

    print("derived fresh API creds for %s" % Account.from_key(pk).address)
    print("  api key suffix ...%s   written to %s"
          % (str(creds.api_key)[-4:], env_path.name))
    print("  backup: %s" % bak.name)


def main() -> None:
    import sys

    env = load_env(ENV)
    missing = [k for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS",
                           "POLYMARKET_SIGNATURE_TYPE",
                           "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET",
                           "POLYMARKET_PASSPHRASE") if not env.get(k)]
    if missing:
        print("MISSING in .env:", ", ".join(missing))
        return

    from eth_account import Account
    pk = env["POLYMARKET_PRIVATE_KEY"]
    if not pk.startswith("0x"):
        pk = "0x" + pk
    eoa = Account.from_key(pk).address
    funder = env["POLYMARKET_FUNDER_ADDRESS"]
    if not funder.startswith("0x"):
        funder = "0x" + funder
    sig = int(env["POLYMARKET_SIGNATURE_TYPE"])

    if "--write-creds" in sys.argv:
        derive_and_write(pk, ENV)
        return

    print("=== addresses ===")
    print("  EOA from the private key : %s" % eoa)
    print("  funder in .env           : %s" % funder)
    print("  configured signature_type: %d" % sig)
    for lab, a in (("EOA", eoa), ("funder", funder)):
        print("  %-6s is a contract: %s" % (lab, is_contract(a)))
    print()
    print("=== what the VENUE thinks each address holds (no auth) ===")
    for lab, a in (("EOA", eoa), ("funder", funder)):
        s = wallet_snapshot(a)
        print("  %-6s value=%s  open positions=%s"
              % (lab, s["value"], s["positions"]))
        for t in s["sample"]:
            print("          %s" % (t,))
    print()
    print("=== on-chain (Polygon) ===")
    for lab, a in (("EOA", eoa), ("funder", funder)):
        print("  %-6s is a contract: %s" % (lab, is_contract(a)))
        for tlab, tok in USDC.items():
            b = balance_of(tok, a)
            print("      %-20s %s" % (tlab,
                                       "unavailable" if b is None else "%.6f" % b))
    print()

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
    from py_clob_client.clob_types import AssetType

    env_creds = ApiCreds(api_key=env["POLYMARKET_API_KEY"],
                         api_secret=env["POLYMARKET_API_SECRET"],
                         api_passphrase=env["POLYMARKET_PASSPHRASE"])

    # L1: do the .env API creds belong to THIS key? (prints no secret values)
    derived_creds = None
    try:
        derived_creds = ClobClient(
            host=HOST, key=pk, chain_id=CHAIN_ID).create_or_derive_api_creds()
        same = str(derived_creds.api_key) == env["POLYMARKET_API_KEY"]
        print("=== are the .env API creds this wallet's? ===")
        print("  re-derived from the key : ...%s" % str(derived_creds.api_key)[-4:])
        print("  .env api key            : ...%s" % env["POLYMARKET_API_KEY"][-4:])
        print("  MATCH: %s" % same)
        if not same:
            print("  -> the API creds were issued for a DIFFERENT wallet than the"
                  " private key.")
    except Exception as e:                                # noqa: BLE001
        print("=== L1 derive FAILED: %s: %s" % (type(e).__name__, str(e)[:120]))
    print()

    # The decisive matrix. A pair that works shows the money; the 401s say the
    # creds are not this wallet's; a 0 balance with valid auth says the funder is
    # wrong. Each outcome needs a different fix, so print which one happened.
    print("=== CLOB collateral: creds x signature_type ===")
    print("   type 0 ignores `funder` (the EOA IS the maker); 1/2 wrap it.")
    for clab, creds in ((".env creds", env_creds),
                        ("derived creds", derived_creds)):
        if creds is None:
            print("  [%s] not available" % clab)
            continue
        for st in (0, 1, 2):
            label = {0: "EOA", 1: "POLY_PROXY", 2: "POLY_GNOSIS_SAFE"}[st]
            funders = [None] if st == 0 else [funder, eoa]
            for f in funders:
                kw = {} if f is None else {"funder": f}
                try:
                    c = ClobClient(host=HOST, key=pk, chain_id=CHAIN_ID,
                                   signature_type=st, **kw)
                    c.set_api_creds(creds)
                    r = c.get_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                    raw = r.get("balance") if isinstance(r, dict) else None
                    val = (int(raw) / 1e6) if raw not in (None, "") else None
                    who = "EOA" if f is None else ("funder" if f == funder else "EOA")
                    flag = "  <== WORKS" if (val or 0) > 0 else ""
                    print("  [%-13s] type %d %-17s funder=%-6s balance %s%s"
                          % (clab, st, label, who,
                             "n/a" if val is None else "%.6f" % val, flag))
                except Exception as e:                    # noqa: BLE001
                    msg = str(e)
                    short = ("401 unauthorized (creds not this wallet's)"
                             if "401" in msg else msg[:70])
                    print("  [%-13s] type %d %-17s funder=%-6s %s"
                          % (clab, st, label,
                             "EOA" if f is None else ("funder" if f == funder else "EOA"),
                             short))
    print()

    print("=== with the CONFIGURED .env pairing, what else does the API say ===")
    kw = {"funder": funder} if sig != 0 else {}
    try:
        c = ClobClient(host=HOST, key=pk, chain_id=CHAIN_ID,
                       signature_type=sig, **kw)
        c.set_api_creds(env_creds)
        for lab, fn in (("collateral/allowance", lambda: c.get_balance_allowance(
                            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))),
                        ("open orders", lambda: len(c.get_orders() or [])),
                        ("api keys on wallet", lambda: len(c.get_api_keys() or []))):
            try:
                print("  %-20s %s" % (lab, fn()))
            except Exception as e:                        # noqa: BLE001
                print("  %-20s ERROR %s" % (lab, str(e)[:70]))
    except Exception as e:                                # noqa: BLE001
        print("  ERROR %s: %s" % (type(e).__name__, str(e)[:160]))


if __name__ == "__main__":
    main()
