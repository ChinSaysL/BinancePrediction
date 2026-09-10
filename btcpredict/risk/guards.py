"""Hard limits that halt trading regardless of what the model wants.

These are deliberately blunt. A subtle bug in pricing shows up as a run of
losses, and the only reliable defence is a rule that does not consult the model.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Halts a new UTC day clears on its own. Both of the guard's own halts are
# day-scoped: the daily budget is spent for the day, and a 5-loss streak is a
# verdict on one day rather than forever. A halt whose kind is anything else --
# including an empty kind, which is what a halt raised outside this class looks
# like -- is treated as permanent and needs a manual resume.
_DAY_SCOPED_HALTS = frozenset({"daily", "streak"})


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    consecutive_losses: int = 0
    open_rounds: set[str] = field(default_factory=set)
    halted: bool = False
    halt_reason: str = ""
    # "daily" or "streak" for the guard's own halts, "" otherwise.
    halt_kind: str = ""
    _day: str = field(default_factory=lambda: time.strftime("%Y-%m-%d", time.gmtime()))

    def roll_day_if_needed(self) -> None:
        """New UTC day: a fresh budget AND a fresh loss streak.

        The streak halt used to be permanent. This method only cleared a halt
        whose reason started with "daily loss", and it never reset
        `consecutive_losses`, so `check` kept re-deriving a streak halt from a
        counter that could not fall -- 5 losses stopped the bot until someone
        ran --resume by hand, even days later. Both halts are day-scoped now.

        The streak counter has to be cleared here rather than only `halted`:
        `check` re-derives the halt from the counter, so clearing the flag alone
        would report success and then refuse every trade.
        """
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today == self._day:
            return
        log.info("new UTC day: daily PnL %+.4f -> 0, loss streak %d -> 0",
                 self.daily_pnl, self.consecutive_losses)
        self._day = today
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        if self.halted and self.halt_kind in _DAY_SCOPED_HALTS:
            log.warning("clearing day-scoped halt at the new UTC day: %s",
                        self.halt_reason)
            self.halted = False
            self.halt_reason = ""
            self.halt_kind = ""


class RiskGuard:
    def __init__(self, *, daily_loss_cap: float, max_consecutive_losses: int,
                 max_open_rounds: int, daily_loss_cap_pct: float = 0.0,
                 bankroll: Callable[[], float] | None = None):
        self.daily_loss_cap = abs(daily_loss_cap)
        self.daily_loss_cap_pct = abs(daily_loss_cap_pct)
        self.bankroll = bankroll
        self.max_consecutive_losses = max_consecutive_losses
        self.max_open_rounds = max_open_rounds
        self.state = RiskState()

    def effective_daily_cap(self) -> float:
        """Loss limit for the day: whichever of the two caps is reached first.

        The percentage is taken against the CURRENT bankroll, so the limit
        tightens as the account shrinks -- deliberately the conservative
        direction, and the reason a percentage cap needs to be several stakes
        wide to be usable rather than hair-trigger.

        A cap of 0 means OFF for either knob, and a guard with both off has no
        daily limit at all (the loss-streak rule still applies). That is why
        this cannot simply `min` the two: a disabled dollar cap would otherwise
        read as a zero-dollar budget and halt on the first loser.
        """
        caps: list[float] = []
        if self.daily_loss_cap > 0:
            caps.append(self.daily_loss_cap)
        if self.daily_loss_cap_pct > 0 and self.bankroll is not None:
            b = self.bankroll()
            if b and b > 0:
                caps.append(self.daily_loss_cap_pct * b)
        return min(caps) if caps else float("inf")

    def _halt(self, kind: str, reason: str) -> None:
        """Raise a halt; `kind` decides whether a new UTC day clears it."""
        self.state.halted = True
        self.state.halt_kind = kind
        self.state.halt_reason = reason
        log.error("RISK HALT: %s", reason)

    def check(self, slug: str) -> tuple[bool, str]:
        """May we open a position on `slug`? Returns (allowed, reason)."""
        self.state.roll_day_if_needed()
        s = self.state
        if s.halted:
            return False, f"halted: {s.halt_reason}"
        cap = self.effective_daily_cap()
        if s.daily_pnl <= -cap:
            self._halt("daily", f"daily loss cap {cap:.2f} hit")
            return False, s.halt_reason
        if s.consecutive_losses >= self.max_consecutive_losses:
            self._halt("streak", f"{s.consecutive_losses} consecutive losses")
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
        cap = self.effective_daily_cap()
        if self.state.daily_pnl <= -cap:
            self._halt("daily", f"daily loss cap {cap:.2f} hit")
        elif self.state.consecutive_losses >= self.max_consecutive_losses:
            self._halt("streak",
                       f"{self.state.consecutive_losses} consecutive losses")

    def resume(self) -> None:
        """Manual override -- never called automatically.

        Clearing `halted` alone is not enough: `check` re-derives the halt from
        daily PnL and the loss streak, so a resume that left those in place
        would report success and then refuse every trade. Both counters are
        therefore reset, which is why this is manual-only and logged loudly.

        This is now the escape hatch for the rare halt that is NOT day-scoped;
        the daily cap and the loss streak both clear by themselves at the UTC
        day boundary (see RiskState.roll_day_if_needed).
        """
        log.warning(
            "risk guard manually resumed (was: %s; daily_pnl %+.4f, streak %d -> reset)",
            self.state.halt_reason or "not halted",
            self.state.daily_pnl, self.state.consecutive_losses,
        )
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.halt_kind = ""
        self.state.consecutive_losses = 0
        self.state.daily_pnl = 0.0

    def seed_daily(self, pnl_today: float, consecutive_losses: int = 0) -> None:
        """Persist daily state across restarts.

        A fresh process starts with zero counters, which would let a bot that
        already lost the daily cap today keep trading after a restart. Seed the
        state from stored settlements and re-derive the halt.

        Both halts are re-derived, so a restart does not launder a streak that is
        still live: the CALLER is responsible for passing a streak that belongs
        to today (see Bot._seed_guard_state), otherwise yesterday's losses would
        halt a fresh day that should have started clean.
        """
        self.state.daily_pnl = pnl_today
        self.state.consecutive_losses = consecutive_losses
        cap = self.effective_daily_cap()
        if pnl_today <= -cap:
            self._halt("daily", f"daily loss cap {cap:.2f} hit")
        elif consecutive_losses >= self.max_consecutive_losses:
            self._halt("streak", f"{consecutive_losses} consecutive losses")
