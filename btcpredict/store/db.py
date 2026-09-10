"""SQLite store for Phase 0 recording and downstream replay.

Design notes:
  * WAL mode so the recorder can write while a report reads.
  * `quotes` is the high-frequency table: one row per poll per round.
  * `rounds` is upserted as facts firm up (strike at open, settlement at close).
  * Every price we can observe is stored side by side so the Chainlink-vs-spot
    basis can be measured after the fact rather than assumed.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    slug            TEXT PRIMARY KEY,
    market_topic_id INTEGER,
    market_id       INTEGER,
    condition_id    TEXT,
    symbol          TEXT,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    strike          REAL,
    end_price       REAL,
    fee_rate_bps    INTEGER,
    up_token_id     TEXT,
    down_token_id   TEXT,
    price_feed_id   TEXT,
    feed_provider   TEXT,
    status          TEXT,
    winner          TEXT,
    settled_at_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rounds_start ON rounds(start_ms);

CREATE TABLE IF NOT EXISTS quotes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL,
    ts_ms         INTEGER NOT NULL,
    tau_s         REAL,
    up_price      REAL,
    down_price    REAL,
    up_chance     REAL,
    down_chance   REAL,
    liquidity     REAL,
    trade_volume  REAL,
    up_bid        REAL,      -- real order-book top of book (authenticated feed)
    up_ask        REAL,
    down_bid      REAL,
    down_ask      REAL,
    fee_rate_bps  INTEGER,   -- as quoted live; the market metadata overstates it
    spot          REAL,      -- Binance spot mid at poll time
    spot_ts_ms    INTEGER,
    oracle        REAL,      -- Chainlink feed price at poll time
    oracle_ts_ms  INTEGER,
    strike        REAL
);
CREATE INDEX IF NOT EXISTS idx_quotes_slug ON quotes(slug);
CREATE INDEX IF NOT EXISTS idx_quotes_ts   ON quotes(ts_ms);

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL,
    ts_ms        INTEGER NOT NULL,
    tau_s        REAL,
    side         TEXT,          -- Up / Down / NONE
    fair         REAL,          -- model probability for the chosen side
    quote        REAL,          -- market price for the chosen side
    edge         REAL,          -- fair - quote
    hurdle       REAL,          -- fee + spread + margin that edge had to clear
    sigma_1m     REAL,
    spot         REAL,
    oracle       REAL,
    strike       REAL,
    stake        REAL,
    acted        INTEGER,       -- 1 if an order was actually attempted
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_slug ON decisions(slug);

CREATE TABLE IF NOT EXISTS fills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id   INTEGER,
    slug          TEXT NOT NULL,
    ts_ms         INTEGER NOT NULL,
    mode          TEXT,          -- paper / live
    side          TEXT,
    requested_px  REAL,
    filled_px     REAL,
    shares        REAL,
    stake         REAL,
    fee_paid      REAL,
    order_id      TEXT,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_slug ON fills(slug);

CREATE TABLE IF NOT EXISTS settlements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id     INTEGER,
    slug        TEXT NOT NULL,
    ts_ms       INTEGER NOT NULL,
    won         INTEGER,
    payout      REAL,
    pnl         REAL,
    mode        TEXT,
    void        INTEGER DEFAULT 0   -- 1: order never filled; no PnL is real
);
CREATE INDEX IF NOT EXISTS idx_settlements_slug ON settlements(slug);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(quotes)")}
        for col, typ in (("up_bid", "REAL"), ("up_ask", "REAL"),
                         ("down_bid", "REAL"), ("down_ask", "REAL"),
                         ("fee_rate_bps", "INTEGER")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE quotes ADD COLUMN {col} {typ}")
        shave = {r["name"] for r in self.conn.execute("PRAGMA table_info(settlements)")}
        if "void" not in shave:
            self.conn.execute("ALTER TABLE settlements ADD COLUMN void INTEGER DEFAULT 0")

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
        self.conn.commit()

    # -- rounds -------------------------------------------------------------
    def upsert_round(self, r: dict[str, Any]) -> None:
        cols = ", ".join(r)
        ph = ", ".join("?" * len(r))
        # Only overwrite a stored column when the incoming value is non-NULL,
        # so a late poll that lost `strike` cannot erase a good earlier value.
        upd = ", ".join(f"{c}=COALESCE(excluded.{c}, rounds.{c})" for c in r if c != "slug")
        self.conn.execute(
            f"INSERT INTO rounds ({cols}) VALUES ({ph}) "
            f"ON CONFLICT(slug) DO UPDATE SET {upd}",
            list(r.values()),
        )
        self.conn.commit()

    def get_round(self, slug: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM rounds WHERE slug=?", (slug,)).fetchone()

    def unsettled_rounds(self, before_ms: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM rounds WHERE winner IS NULL AND end_ms < ? ORDER BY end_ms",
            (before_ms,),
        ).fetchall()

    # -- generic inserts ----------------------------------------------------
    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        ph = ", ".join("?" * len(row))
        cur = self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({ph})", list(row.values())
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("rounds", "quotes", "decisions", "fills", "settlements"):
            out[t] = self.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        return out

    def close(self) -> None:
        self.conn.close()
