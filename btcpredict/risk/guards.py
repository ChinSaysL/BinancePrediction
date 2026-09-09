"""Hard limits that halt trading regardless of what the model wants.

These are deliberately blunt. A subtle bug in pricing shows up as a run of
losses, and the only reliable defence is a rule that does not consult the model.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    consecutive_losses: int = 0
    open_rounds: set[str] = field(default_factory=set)
    halted: bool = False
    halt_reason: str = ""
    _day: str = field(default_factory=lambda: time.strftime("%Y-%m-%d", time.gmtime()))

    def roll_day_if_needed(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._day:
            log.info("new UTC day: resetting daily PnL (was %+.4f)", self.daily_pnl)
            self._day = today
            self.daily_pnl = 0.0
            if self.halted and self.halt_reason.startswith("daily loss"):
                self.halted = False
                self.halt_reason = ""


class RiskGuard:
    def __init__(self, *, daily_loss_cap: float, max_consecutive_losses: int,
                 max_open_rounds: int):
        self.daily_loss_cap = abs(daily_loss_cap)
        self.max_consecutive_losses = max_consecutive_losses
        self.max_open_rounds = max_open_rounds
        self.state = RiskState()

    def check(self, slug: str) -> tuple[bool, str]:
        """May we open a position on `slug`? Returns (allowed, reason)."""
        self.state.roll_day_if_needed()
        s = self.state
        if s.halted:
            return False, f"halted: {s.halt_reason}"
        if s.daily_pnl <= -self.daily_loss_cap:
            s.halted = True
            s.halt_reason = f"daily loss cap {self.daily_loss_cap:.2f} hit"
            return False, s.halt_reason
        if s.consecutive_losses >= self.max_consecutive_losses:
            s.halted = True
            s.halt_reason = f"{s.consecutive_losses} consecutive losses"
            return False, s.halt_reason
        if slug in s.open_rounds:
            return False, "already positioned in this round"
        if len(s.open_rounds) >= self.max_open_rounds:
            return False, f"max open rounds ({self.max_open_rounds})"
        return True, "ok"

    def on_open(self, slug: str) -> None:
        self.state.open_rounds.add(slug)

    def on_settle(self, slug: str, pnl: float) -> None:
        self.state.open_rounds.discard(slug)
        self.state.daily_pnl += pnl
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0
        if self.state.daily_pnl <= -self.daily_loss_cap:
            self.state.halted = True
            self.state.halt_reason = f"daily loss cap {self.daily_loss_cap:.2f} hit"
            log.error("RISK HALT: %s", self.state.halt_reason)
        elif self.state.consecutive_losses >= self.max_consecutive_losses:
            self.state.halted = True
            self.state.halt_reason = f"{self.state.consecutive_losses} consecutive losses"
            log.error("RISK HALT: %s", self.state.halt_reason)

    def resume(self) -> None:
        """Manual override -- never called automatically.

        Clearing `halted` alone is not enough: `check` re-derives the halt from
        daily PnL and the loss streak, so a resume that left those in place
        would report success and then refuse every trade. Both counters are
        therefore reset, which is why this is manual-only and logged loudly.
        """
        log.warning(
            "risk guard manually resumed (was: %s; daily_pnl %+.4f, streak %d -> reset)",
            self.state.halt_reason or "not halted",
            self.state.daily_pnl, self.state.consecutive_losses,
        )
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.consecutive_losses = 0
        self.state.daily_pnl = 0.0

    def seed_daily(self, pnl_today: float, consecutive_losses: int = 0) -> None:
        """Persist daily state across restarts.

        A fresh process starts with zero counters, which would let a bot that
        already lost the daily cap today keep trading after a restart. Seed the
        state from stored settlements and re-derive the halt.
        """
        self.state.daily_pnl = pnl_today
        self.state.consecutive_losses = consecutive_losses
        if pnl_today <= -self.daily_loss_cap:
            self.state.halted = True
            self.state.halt_reason = f"daily loss cap {self.daily_loss_cap:.2f} hit"
            log.error("RISK HALT: %s (persisted across restart)", self.state.halt_reason)
