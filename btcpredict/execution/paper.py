"""Simulated broker.

Fills are deliberately pessimistic: we pay the displayed price *plus* the
assumed half-spread, then the fee. Because there is no public order book, the
displayed price is a last-trade/mid rather than something executable, so an
optimistic fill model here would produce a backtest that cannot be reproduced
live. `half_spread` is the single knob Phase 1's cost-sensitivity curve sweeps.
"""
from __future__ import annotations

import itertools
import logging
import time

from btcpredict.execution.base import Fill, Settlement
from btcpredict.edge.signal import fee_rate_for_price

log = logging.getLogger(__name__)


class PaperBroker:
    mode = "paper"

    def __init__(self, starting_balance: float = 100.0, fee_rate: float | None = None,
                 half_spread: float = 0.02, fee_on_notional: bool = True):
        self.starting_balance = starting_balance
        self._balance = starting_balance
        # fee_rate=None -> charge the measured per-price fee curve instead of a
        # flat rate: fee_bps = 180 * min(1, (1-px)/px).
        self.fee_rate = fee_rate
        self.half_spread = half_spread
        self.fee_on_notional = fee_on_notional
        self._ids = itertools.count(1)

    async def buy(self, *, slug: str, side: str, token_id: str,
                  price: float, stake: float,
                  max_px: float | None = None) -> Fill | None:
        fill_px = min(0.999, price + self.half_spread)
        if fill_px <= 0 or stake <= 0:
            return None
        # Same contract as the live broker: never fill above the caller's
        # ceiling. Paper honours it so a backtest cannot show fills the live
        # path would have refused.
        if max_px is not None and fill_px > max_px + 1e-9:
            return None
        rate = self.fee_rate if self.fee_rate is not None else fee_rate_for_price(fill_px)
        fee = stake * rate if self.fee_on_notional else 0.0
        total_cost = stake + fee
        if total_cost > self._balance:
            log.warning("paper: insufficient balance %.4f < %.4f", self._balance, total_cost)
            return None
        self._balance -= total_cost
        shares = stake / fill_px
        return Fill(
            slug=slug, side=side, requested_px=price, filled_px=fill_px,
            shares=shares, stake=stake, fee_paid=fee,
            order_id=f"paper-{next(self._ids)}", ts_ms=int(time.time() * 1000),
        )

    async def settle(self, fill: Fill, winner: str) -> Settlement:
        won = fill.side.lower() == (winner or "").lower()
        payout = fill.shares if won else 0.0
        if won and not self.fee_on_notional:
            rate = self.fee_rate if self.fee_rate is not None else fee_rate_for_price(fill.filled_px)
            profit = payout - fill.stake
            payout -= max(0.0, profit) * rate
        self._balance += payout
        pnl = payout - fill.stake - fill.fee_paid
        return Settlement(slug=fill.slug, won=won, payout=payout, pnl=pnl,
                          ts_ms=int(time.time() * 1000))

    async def balance(self) -> float:
        return self._balance
