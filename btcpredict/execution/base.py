"""Broker interface.

Everything uncertain about the live venue is confined to this contract. The
Binance prediction REST trading endpoints are not in the public docs (the
`w3w_prediction` tree is gated), so `live.py` is the only module that should
need editing once the exact paths are known.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class Fill:
    slug: str
    side: str
    requested_px: float
    filled_px: float
    shares: float
    stake: float
    fee_paid: float
    order_id: str
    ts_ms: int
    raw: str = ""

    @property
    def max_payout(self) -> float:
        """What the position returns if it wins (shares each pay $1)."""
        return self.shares


@dataclass(slots=True)
class Settlement:
    slug: str
    won: bool
    payout: float
    pnl: float
    ts_ms: int


@runtime_checkable
class Broker(Protocol):
    mode: str

    async def buy(self, *, slug: str, side: str, token_id: str,
                  price: float, stake: float) -> Fill | None:
        """Buy `stake` USDT worth of `side`. Returns None if the order failed."""
        ...

    async def settle(self, fill: Fill, winner: str) -> Settlement:
        """Resolve a fill against the round's winning outcome."""
        ...

    async def balance(self) -> float:
        ...
