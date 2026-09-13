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
    # Rounds held by a RESERVED-slot arm (the cross arm), tracked apart from
    # `open_rounds` so it gets a slot of its own. A shared counter cannot
    # express "two positions, one of them reserved": the main arm, which fires
    # far more often, would simply take both.
    reserved_rounds: set[str] = field(default_factory=set)
    # Concurrent reserved POSITIONS, which is not the same as rounds: the cross
    # arm may take several crosses in one round (they alternate sides), so a
    # set of slugs cannot count the exposure.
    reserved_open: int = 0
    # Pools for every OTHER arm, keyed by arm name. Arms are independent by
    # explicit instruction: one arm's position must never refuse another's, so
    # each gets a budget of its own rather than competing for `open_rounds`.
    # `open_rounds` stays the MAIN arm's pool and `reserved_*` the cross arm's;
    # both predate this and are left alone rather than migrated, because the
    # daily cap -- not the slot count -- is what bounds total exposure, and a
    # rename here would touch every call site that guards real money.
    pool_rounds: dict[str, set[str]] = field(default_factory=dict)
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

        A cap of 0 means OFF for either knob, and a guard with these and the
        loss-streak rule all off has no limit of its own left. That is why this
        cannot simply `min` the two: a disabled dollar cap would otherwise read
        as a zero-dollar budget and halt on the first loser.
        """
        caps: list[float] = []
        if self.daily_loss_cap > 0:
            caps.append(self.daily_loss_cap)
        if self.daily_loss_cap_pct > 0 and self.bankroll is not None:
            b = self.bankroll()
            if b and b > 0:
                caps.append(self.daily_loss_cap_pct * b)
        return min(caps) if caps else float("inf")

    def _streak_breached(self, losses: int) -> bool:
        """Is the loss-streak rule breached? A limit of 0 means OFF.

        One helper because this comparison appeared in three places (check,
        on_settle, seed_daily) and the off case has to be handled in ALL of
        them: `losses >= 0` is true on the first call, so a bare comparison
        reads "disabled" as "halt now" -- the exact opposite of the ask, and
        the bot would have stopped on its first settled trade.
        """
        return (self.max_consecutive_losses > 0
                and losses >= self.max_consecutive_losses)

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
        # A limit of 0 is OFF, the same convention as the caps above -- see
        # _streak_breached for why that needs its own code path.
        if self._streak_breached(s.consecutive_losses):
            self._halt("streak", f"{s.consecutive_losses} consecutive losses")
            return False, s.halt_reason
        if slug in s.open_rounds:
            return False, "already positioned in this round"
        if len(s.open_rounds) >= self.max_open_rounds:
            return False, f"max open rounds ({self.max_open_rounds})"
        return True, "ok"

    def check_add(self, slug: str) -> tuple[bool, str]:
        """May the DOUBLE-DOWN add to a round we are already holding?

        Deliberately NOT `check()`: that refuses a slug we already hold, which
        is exactly what an add is. Every halt still applies -- an add must never
        happen on a day the account has been stopped -- but the round-dedup and
        the open-round count are what this exists to bypass, and it is the only
        caller in the codebase permitted to bypass them.
        """
        self.state.roll_day_if_needed()
        s = self.state
        if s.halted:
            return False, f"halted: {s.halt_reason}"
        cap = self.effective_daily_cap()
        if s.daily_pnl <= -cap:
            self._halt("daily", f"daily loss cap {cap:.2f} hit")
            return False, s.halt_reason
        if self._streak_breached(s.consecutive_losses):
            self._halt("streak", f"{s.consecutive_losses} consecutive losses")
            return False, s.halt_reason
        return True, "ok"

    def check_reserved(self, slug: str, *, max_open: int = 1) -> tuple[bool, str]:
        """May a RESERVED-slot arm open on `slug`? Currently the cross arm.

        Deliberately NOT `check()`. The main arm enters at tau 60-210 and holds
        to settlement, which covers the cross arm's whole tau 3-60 window, so a
        shared slot let the main arm crowd it out of 82% of the rounds it wanted
        -- and 61% of those it wanted the OPPOSITE side, i.e. hedges were being
        refused rather than double exposure prevented.

        Every HALT still applies, and so does the daily loss cap: a reserved arm
        must never trade on a day the account has been stopped. What it bypasses
        is the main arm's round-dedup and open-round count. It keeps its own
        single slot, so the account holds at most one main position plus one
        reserved one.

        Both sides are allowed. Opposite-side is a hedge and same-side a
        correlated add; on the measured sample allowing both was the better of
        the two (16 fills at +0.116/$1 against 9 at +0.068). The cost is that a
        same-side loser costs the main stake plus the cross stake rather than
        the two offsetting, and the daily cap is what bounds that.
        """
        self.state.roll_day_if_needed()
        s = self.state
        if s.halted:
            return False, f"halted: {s.halt_reason}"
        cap = self.effective_daily_cap()
        if s.daily_pnl <= -cap:
            self._halt("daily", f"daily loss cap {cap:.2f} hit")
            return False, s.halt_reason
        if self._streak_breached(s.consecutive_losses):
            self._halt("streak", f"{s.consecutive_losses} consecutive losses")
            return False, s.halt_reason
        if s.reserved_open >= max_open:
            return False, f"reserved slots full ({max_open})"
        return True, "ok"

    def check_pool(self, slug: str, *, pool: str,
                   max_open: int) -> tuple[bool, str]:
        """May the arm named `pool` open on `slug`? Its own budget only.

        Every HALT still applies, and so does the daily loss cap -- those are
        account-wide and no arm is exempt. What an arm does NOT inherit is
        another arm's slot usage: the main arm enters at tau 60-210 and holds
        to the bell, which covers the whole window every faster arm works in,
        so a shared counter means whichever arm fires first silently disables
        the rest for the round.

        The round-dedup is per pool too, so an arm still cannot double its own
        position while another arm may take the same round on either side.
        """
        self.state.roll_day_if_needed()
        s = self.state
        if s.halted:
            return False, f"halted: {s.halt_reason}"
        cap = self.effective_daily_cap()
        if s.daily_pnl <= -cap:
            self._halt("daily", f"daily loss cap {cap:.2f} hit")
            return False, s.halt_reason
        if self._streak_breached(s.consecutive_losses):
            self._halt("streak", f"{s.consecutive_losses} consecutive losses")
            return False, s.halt_reason
        rounds = s.pool_rounds.setdefault(pool, set())
        if slug in rounds:
            return False, f"{pool}: already positioned in this round"
        if len(rounds) >= max_open:
            return False, f"{pool}: max open rounds ({max_open})"
        return True, "ok"

    def on_open(self, slug: str, *, reserved: bool = False,
                pool: str | None = None) -> None:
        if pool is not None:
            self.state.pool_rounds.setdefault(pool, set()).add(slug)
        elif reserved:
            self.state.reserved_rounds.add(slug)
            self.state.reserved_open += 1
        else:
            self.state.open_rounds.add(slug)

    def on_settle(self, slug: str, pnl: float, *, reserved: bool = False,
                  pool: str | None = None) -> None:
        if pool is not None:
            self.state.pool_rounds.setdefault(pool, set()).discard(slug)
        elif reserved:
            self.state.reserved_rounds.discard(slug)
            self.state.reserved_open = max(0, self.state.reserved_open - 1)
        else:
            self.state.open_rounds.discard(slug)
        self.state.daily_pnl += pnl
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0
        cap = self.effective_daily_cap()
        if self.state.daily_pnl <= -cap:
            self._halt("daily", f"daily loss cap {cap:.2f} hit")
        elif self._streak_breached(self.state.consecutive_losses):
            self._halt("streak",
                       f"{self.state.consecutive_losses} consecutive losses")

    def release(self, slug: str, *, reserved: bool = False,
                pool: str | None = None) -> None:
        """Free a slot WITHOUT recording a settlement. Voids only.

        A void means the venue never filled the order, so no stake was ever
        deployed: there is no P&L to add and no loss streak to advance. But
        `_book_fill` already claimed the slot via on_open, and the void branch
        of the settle loop used to `continue` past on_settle entirely -- so the
        claim was never released and the arm lost that slot for the life of the
        process.

        The blast radius scaled with the pool. The cross arm went quiet after
        `cross_max_per_round` voids; the MAIN arm went quiet after ONE, because
        `open_rounds` is a set and `max_open_rounds` is 1, so a single leaked
        slug refused every subsequent round. It self-healed only on restart --
        a voided fill has a settlement row, so _restore_open_positions does not
        replay it -- which is why a bot that restarts often never showed it.

        Deliberately NOT on_settle(pnl=0.0): that would count the void as a
        non-loss and reset consecutive_losses, laundering a real streak.
        """
        if pool is not None:
            self.state.pool_rounds.setdefault(pool, set()).discard(slug)
        elif reserved:
            self.state.reserved_rounds.discard(slug)
            self.state.reserved_open = max(0, self.state.reserved_open - 1)
        else:
            self.state.open_rounds.discard(slug)
        log.info("released the %s slot for %s without settling (void)",
                 pool or ("reserved" if reserved else "main"), slug)

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
        elif self._streak_breached(consecutive_losses):
            self._halt("streak", f"{consecutive_losses} consecutive losses")
