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
    book_age_ms   INTEGER,   -- age of the ws book behind the four prices above
    up_ask_size   REAL,      -- SHARES resting at up_ask; NULL when REST-sourced
    up_bid_size   REAL,      -- and at up_bid. The Down book mirrors both.
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

-- Reconstructed/authoritative 1m bars, including Binance kline field 9 (taker
-- buy base volume). The quotes table can only ever reconstruct close+volume,
-- so the order-flow models (sentiment, hybrid) cannot be replayed from it.
-- Binance serves this history, so the table is backfillable for any window.
CREATE TABLE IF NOT EXISTS bars (
    symbol    TEXT NOT NULL,
    open_ms   INTEGER NOT NULL,
    close     REAL,
    volume    REAL,
    taker_buy REAL,
    PRIMARY KEY (symbol, open_ms)
);

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
                         ("fee_rate_bps", "INTEGER"),
                         # Age of the websocket book behind up_bid/up_ask/etc,
                         # in ms. NULL means the REST poll supplied them (the
                         # primary) or nothing did. See
                         # config.ws_book_record_max_age_s: the recorder keeps a
                         # wider staleness window than the trading paths, and
                         # this column is what makes that safe to replay.
                         ("book_age_ms", "INTEGER"),
                         # SIZE AT THE TOUCH, in shares. The tape recorded what
                         # the book COST and never what it HELD, so no replay
                         # could ask the one question every fill depends on:
                         # was our stake actually there to buy? `BookTop` has
                         # carried `up_ask_size`/`up_bid_size` all along and
                         # `ask_dollars()` already turns them into the dollar
                         # depth on either side -- it simply was never written
                         # down. NULL where the REST poll supplied the prices
                         # (`best_prices` returns a pair, no size), so a replay
                         # must filter on NOT NULL rather than treat 0 as thin.
                         ("up_ask_size", "REAL"),
                         ("up_bid_size", "REAL")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE quotes ADD COLUMN {col} {typ}")
        shave = {r["name"] for r in self.conn.execute("PRAGMA table_info(settlements)")}
        if "void" not in shave:
            self.conn.execute("ALTER TABLE settlements ADD COLUMN void INTEGER DEFAULT 0")
        hfill = {r["name"] for r in self.conn.execute("PRAGMA table_info(fills)")}
        if "ev_at_fill" not in hfill:
            # Per-fill EV at the price actually paid, next to the gate's EV at
            # the quote. Added 2026-09-11, when the difference turned out to be
            # worth about a third of the claimed edge.
            self.conn.execute("ALTER TABLE fills ADD COLUMN ev_at_fill REAL")
        if "token_id" not in hfill:
            # Which outcome token the fill actually bought. Without it, a fill
            # restored after a restart carries token_id None, and settle()'s
            # verification -- "is this token in the venue's position list?" --
            # asks about nothing, concludes the venue holds none, and VOIDS a
            # real position after 20 checks: the stake is spent, the PnL reads
            # zero, and the round's result is missing from the record.
            self.conn.execute("ALTER TABLE fills ADD COLUMN token_id TEXT")

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

    # -- 1m bars ------------------------------------------------------------
    def upsert_bars(self, symbol: str, bars: Iterable[tuple[int, float, float, float]]
                    ) -> int:
        """Store (open_ms, close, volume, taker_buy) bars, newest value wins.

        Keeps the order-flow models replayable: the venue-side quotes table has
        no taker data at all, and only Binance can supply it (live or backfilled).
        """
        rows = [(symbol, int(o), float(c), float(v), float(t))
                for o, c, v, t in bars]
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO bars (symbol, open_ms, close, volume, taker_buy) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, open_ms) DO UPDATE SET "
            "close = excluded.close, volume = excluded.volume, "
            "taker_buy = excluded.taker_buy", rows)
        self.conn.commit()
        return len(rows)

    def bars_since(self, symbol: str, start_ms: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT open_ms, close, volume, taker_buy FROM bars "
            "WHERE symbol = ? AND open_ms >= ? ORDER BY open_ms",
            (symbol, start_ms)).fetchall()

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
