"""Fetch 1-minute klines covering the Polymarket era, for the bot's OWN sigma.

WHY. `data/bars_1m/*_1m_30d.csv` stops at 2026-09-12 and the Polymarket era
starts 2026-09-16, so the DB cannot reproduce the input the deployed model would
run on. `btcpredict/pricing/vol.py` maintains sigma_1m as an EWMA over 240
ONE-MINUTE log returns (4 h), which is a different estimator from anything the
`quotes` table can support (it holds ~300 s of spot per round). Scoring the
model with a substituted sigma measures the substitute, not the model.

Writes `data/bars_1m/<SYM>_1m_pm.csv` in the same column layout as the 30d files
so one reader can serve both.

Usage: PYTHONPATH=/root/BinancePrediction /root/analysis-venv/bin/python \
           tools/pm_fetch_klines_pm.py
"""
from __future__ import annotations

import pathlib
import time

import httpx
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "bars_1m"
SYMS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
URL = "https://api.binance.com/api/v3/klines"
START_MS = 1_789_200_000_000          # 2026-09-14 (a day of lead-in for the EWMA)


def fetch(sym: str, client: httpx.Client) -> pd.DataFrame:
    rows, start = [], START_MS
    while True:
        r = client.get(URL, params=dict(symbol=sym, interval="1m",
                                        startTime=start, limit=1000))
        r.raise_for_status()
        got = r.json()
        if not got:
            break
        rows.extend(got)
        start = got[-1][0] + 60_000
        if len(got) < 1000 or start > int(time.time() * 1000):
            break
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
    df = df[["open_time", "close_time", "open", "high", "low", "close",
             "volume", "trades"]]
    df["ts"] = (df["open_time"] // 1000).astype("int64")
    return df


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=20.0, headers={"User-Agent": "btcpredict/1.0"}) as c:
        for sym in SYMS:
            df = fetch(sym, c)
            p = OUT / f"{sym}_1m_pm.csv"
            df.to_csv(p, index=False)
            print(f"{sym:9s} {len(df):5d} bars  {df.ts.iloc[0]} .. {df.ts.iloc[-1]}"
                  f"  -> {p.name}")
            time.sleep(0.3)


if __name__ == "__main__":
    main()
