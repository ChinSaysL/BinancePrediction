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
    token_id: str = ""
    # The order was placed (HTTP 200 + orderId) but order-history could not
    # confirm its status. The venue returns 200 even for FOK orders that fail,
    # so such a fill is provisional: settle() re-verifies and may void it.
    unverified: bool = False
    unverified_checks: int = 0

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
    # True when the fill turned out to be phantom: the venue never filled the
    # order, so nothing was staked and the recorded pnl is zero by design.
    void: bool = False


@runtime_checkable
class Broker(Protocol):
    mode: str

    async def buy(self, *, slug: str, side: str, token_id: str,
                  price: float, stake: float,
                  max_px: float | None = None) -> Fill | None:
        """Buy `stake` USDT worth of `side`. Returns None if the order failed.

        `max_px` is the highest price the caller will accept. It exists because
        a MARKET order prices off the venue's quote, and that quote follows the
        DISPLAYED price, which lags the book -- once by 0.67, buying at 0.84
        what was on offer at 0.17. Brokers MUST refuse rather than fill above it.
        """
        ...

    async def settle(self, fill: Fill, winner: str) -> Settlement:
        """Resolve a fill against the round's winning outcome."""
        ...

    async def balance(self) -> float:
        ...
