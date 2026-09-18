"""Polymarket venue adapter.

A SECOND VENUE, NOT A SECOND STRATEGY. The arms, gates and sizing stay where
they are; this package supplies the four venue-specific things they assume:

    markets.py   round discovery + settlement read-back              DONE
    book.py      live top-of-book (same shape as the Binance feed)   DONE
    broker.py    order placement, fills, redemption                  DONE
    pricing.py   the terminal distribution + the entry's fee/edge     DONE

Read `markets.py`'s docstring before touching any number here: the venue lists
the SAME `btc-updown-5m-<unix>` market as the Binance wrapper, with a DIFFERENT
settlement rule (a 60 s Chainlink TWAP against the range's opening price, versus
the wrapper's point snapshot). Measured disagreement between the two: 10.4%
over 48 settled rounds, and 12.7% over 2,303. Everything fitted on the
wrapper's rule is invalid here -- which is why `settings.venue` defaults to the
incumbent rather than flipping.

`pricing.py` settles which WINDOW the TWAP averages over, and that is a
measurement, not a reading of the venue's prose: the last 60 seconds fits
Polymarket's own published winners on 90.7% of 2,303 rounds, the whole round on
84.1%. `btcpredict/pricing/twap.py` is the whole-round derivation -- correct
algebra, wrong window for this venue. Use `window=TAIL` (the default) here.

THIS PACKAGE MUST NOT IMPORT `btcpredict.models`. It holds venue primitives;
the model adapter for the distribution is `btcpredict/models/twap.py`. Putting
it here created a real import cycle that only failed when this module was
imported FIRST, so every test starting from `btcpredict.models` hid it.

LATENCY IS THE DESIGN CONSTRAINT. Every clob.polymarket.com call costs
~260 ms regardless of our box, our IP family or a warm connection, and the
order path is `sign + post`. `broker.py`'s four rules (prime the signer, no
per-order discovery, no balance read on the hot path, sign and post separately)
exist to keep an order at 2 calls ~ 280 ms instead of 4 calls ~ 780 ms.
"""
