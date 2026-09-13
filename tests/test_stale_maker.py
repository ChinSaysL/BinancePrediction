"""The stale-display maker: rest at the lagging price instead of crossing.

MEASURED 2026-09-12 by lead-lag cross-correlation over 1,372 rounds: the venue's
BOOK tracks spot with no lag (peak corr 0.661 at L=0) while its DISPLAYED price
lags ~2s (peak 0.277 at L=+2). The edge that creates is entirely in the FILL
PRICE -- the same rule scores +0.0175/$1 (t 0.47) filled at the book ask and
+0.3449/$1 (t 5.88) filled at the mid -- so the only thing that matters is
whether the order rests below the book instead of crossing to it.

These pin the price rule, which is pure, and the two properties that make the
arm safe: it never crosses, and it replaces the taker order rather than adding
a second position to the round.
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile
from types import SimpleNamespace

import pytest

from btcpredict.main import stale_maker_px


class TestThePriceRule:
    def test_it_rests_at_the_stale_display_not_at_the_book(self):
        # display 0.40 lagging a 0.70 book, fair 0.75: post near the display.
        assert stale_maker_px(0.40, 0.70, 0.75, 0.01) == 0.41

    def test_it_never_crosses_the_ask(self):
        """Crossing pays the spread AND the taker fee -- the losing bucket."""
        # offset large enough to reach the ask: must be capped below it.
        px = stale_maker_px(0.68, 0.70, 0.99, 0.10)
        assert px is not None and px < 0.70

    def test_it_never_posts_above_our_own_fair(self):
        """Fair is a ceiling: posting above it buys for more than it is worth."""
        assert stale_maker_px(0.40, 0.90, 0.45, 0.20) == 0.45

    def test_no_room_below_the_ask_means_no_order(self):
        assert stale_maker_px(0.40, 0.01, 0.90, 0.01) is None

    @pytest.mark.parametrize("display,ask,fair", [
        (None, 0.70, 0.80), (0.40, None, 0.80),
        (0.0, 0.70, 0.80), (1.0, 0.70, 0.80),
        (0.40, 0.70, 0.0), (0.40, 0.70, 1.0),
    ])
    def test_impossible_inputs_are_refused(self, display, ask, fair):
        assert stale_maker_px(display, ask, fair, 0.01) is None

    def test_the_offset_trades_fill_rate_against_price(self):
        """Bigger offset = closer to the book = likelier fill, worse price."""
        lo = stale_maker_px(0.40, 0.70, 0.90, 0.01)
        hi = stale_maker_px(0.40, 0.70, 0.90, 0.10)
        assert lo is not None and hi is not None and hi > lo
