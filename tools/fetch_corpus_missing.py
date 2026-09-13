"""Fetch only the corpus files that are missing, straight off the URL list.

The hub's downloader issues a HEAD per file, and at 8 workers that trips HF's 429
limiter (2.76 files/s with 128s backoff). This bypasses that: it walks the exact
URL list we already generated, skips anything on disk, and GETs the rest with
courteous concurrency and exponential backoff.

metadata.json is 5 KB and is all that is needed to unlock the price windows (the
trades are already downloaded), so --only metadata is the fast path to more days.
"""
from __future__ import annotations

import argparse
import os
import random
import socket
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# This host publishes AAAA records but its IPv6 route is dead. getaddrinfo
# returns the v6 address first, the connect never fails fast, and the socket
# timeout does not cover name resolution -- so threads hang forever and the
# download appears to stall at 0 files/s. Drop v6 answers entirely.
_orig_gai = socket.getaddrinfo


def _v4_only(host, port, family=0, *a, **kw):
    res = _orig_gai(host, port, family, *a, **kw)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res


socket.getaddrinfo = _v4_only
socket.setdefaulttimeout(20)

ROOT = "data/btc5m"
LOCK = threading.Lock()
STAT = {"ok": 0, "skip": 0, "fail": 0, "429": 0, "bytes": 0}
T0 = time.time()


def bump(field: str, nbytes: int = 0) -> None:
    with LOCK:
        STAT[field] += 1
        STAT["bytes"] += nbytes
        done = STAT["ok"] + STAT["fail"]
        if field == "ok" and done % 250 == 0:
            el = time.time() - T0
            print(f"  {STAT['ok']:,} ok  {STAT['fail']:,} fail  "
                  f"{STAT['429']:,} 429s  {STAT['bytes'] / 1e6:.1f} MB  "
                  f"{STAT['ok'] / max(el, 1e-9):.1f} files/s", flush=True)


def local_path(url: str) -> str:
    rel = url.split("/resolve/", 1)[1].split("/", 1)[1]
    return os.path.join(ROOT, rel)


def get(url: str, path: str, tries: int = 5) -> bool:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            bump("ok", len(data))
            return True
        except urllib.error.HTTPError as e:  # noqa: PERF203
            if e.code == 429:
                bump("429")
                time.sleep(min(45, 3 * (a + 1) ** 2) + random.random() * 2)
            elif e.code == 404:
                bump("fail")
                return False
            else:
                time.sleep(1 + a)
        except Exception:
            time.sleep(1 + a)
    bump("fail")
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", default="data/btc5m_urls.txt")
    ap.add_argument("--only", default="metadata",
                    choices=["metadata", "ticks", "all"])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    want = {"metadata": ["metadata.json"], "ticks": ["ticks_dense.parquet"],
            "all": ["metadata.json", "ticks_dense.parquet"]}[args.only]
    with open(args.urls) as f:
        urls = [u.strip() for u in f if u.strip()]
    todo = []
    for u in urls:
        if not any(u.endswith(w) or f"/{w}" in u for w in want):
            continue
        p = local_path(u)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            with LOCK:
                STAT["skip"] += 1
            continue
        todo.append((u, p))
    if args.limit:
        todo = todo[:args.limit]
    print(f"urls {len(urls):,}  already present {STAT['skip']:,}  "
          f"to fetch {len(todo):,} ({args.only}) with {args.workers} workers")
    if not todo:
        print("nothing to do"); return

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(lambda up: get(up[0], up[1]), todo))
    dt = time.time() - t0
    print(f"done in {dt:.0f}s  ok {STAT['ok']:,}  failed {STAT['fail']:,}  "
          f"429s {STAT['429']:,}  {STAT['bytes'] / 1e6:.1f} MB  "
          f"({STAT['ok'] / max(dt, 1e-9):.1f} files/s)")


if __name__ == "__main__":
    main()
