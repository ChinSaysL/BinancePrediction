"""A claim the venue rejects must be retried on a clock, not on the next win.

2026-09-15 audit: `redeem()` fired only from `settle_due`, and only for a WON
fill -- so a claim that failed there (the venue rejects a not-yet-final
position, -9000 SYSTEM_ERROR, seen on the claim call that fired 6 seconds
after the first 15m round ended) waited for the NEXT win to be retried.
`sweep_claims()` is the retry clock: wallet-wide, on a wall-clock cooldown,
and a no-op outside live mode.
"""
import asyncio
import time

from btcpredict.main import Bot, _CLAIM_SWEEP_S


class FakeBroker:
    def __init__(self, mode="live", result=None):
        self.mode = mode
        self.result = result
        self.redeems = 0

    async def redeem(self):
        self.redeems += 1
        return self.result


def make_bot(broker):
    bot = Bot.__new__(Bot)              # skip network setup
    bot.broker = broker
    bot._last_claim_sweep = 0.0
    return bot


def test_the_sweep_claims_and_stamps_the_clock():
    broker = FakeBroker(result={"redeemed": 1})
    bot = make_bot(broker)
    asyncio.run(bot.sweep_claims())
    assert broker.redeems == 1
    assert bot._last_claim_sweep > 0.0


def test_the_sweep_respects_its_cooldown():
    broker = FakeBroker(result={"redeemed": 1})
    bot = make_bot(broker)
    bot._last_claim_sweep = time.time()          # just swept
    asyncio.run(bot.sweep_claims())
    assert broker.redeems == 0


def test_the_sweep_fires_again_once_the_cooldown_passes():
    broker = FakeBroker(result={"redeemed": 1})
    bot = make_bot(broker)
    bot._last_claim_sweep = time.time() - _CLAIM_SWEEP_S - 1.0
    asyncio.run(bot.sweep_claims())
    assert broker.redeems == 1


def test_the_sweep_asks_even_when_nothing_is_claimable():
    broker = FakeBroker(result=None)             # venue lists nothing
    bot = make_bot(broker)
    asyncio.run(bot.sweep_claims())
    assert broker.redeems == 1                   # it asked; nothing to POST


def test_the_sweep_is_a_noop_outside_live_mode():
    broker = FakeBroker(mode="paper")
    bot = make_bot(broker)
    asyncio.run(bot.sweep_claims())
    assert broker.redeems == 0


def test_the_sweep_survives_a_bot_without_a_broker():
    bot = Bot.__new__(Bot)                       # no broker attribute at all
    asyncio.run(bot.sweep_claims())              # must not raise
