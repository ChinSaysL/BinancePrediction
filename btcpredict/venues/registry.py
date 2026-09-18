"""Venue selection: turn `settings` into a broker, without touching main.py.

`main.py` should not learn about either venue's constructor. It asks this
module for a bundle and gets back something that satisfies the surface it
uses (`buy`, `buy_limit`, `settle`, `equity`, `positions`, `order_book`,
`active_orders`, `cancel_orders`, `redeem`, `mode`, `wallet_address`,
`last_failure`).

Why this is a module and not three lines inline: the translation from flat
settings to a venue config is exactly the kind of thing that silently keeps a
stale default, and it is testable here with no network and no key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

VENUES = ("binance", "polymarket")


def normalise(name: str | None) -> str:
    """The venue name, checked. An unknown name is a config error, not a
    silent fallback -- defaulting to the other venue would mean trading a
    market whose settlement rule the model was not fitted on."""
    v = (name or "binance").strip().lower()
    if v not in VENUES:
        raise ValueError(f"unknown venue {name!r}; expected one of {VENUES}")
    return v


@dataclass
class VenueBundle:
    name: str
    broker: Any
    #: The object the arms read prices from. On Polymarket the broker IS the
    #: book holder, so this is the same instance and there is no second socket
    #: to keep alive.
    book: Any = None
    #: Binance-only: the HMAC-signed websocket task. None for Polymarket,
    #: which uses its own CLOB socket inside `PmBook`.
    ws_task: Any = None
    note: str = ""
    extras: dict = field(default_factory=dict)


def pm_config(settings: Any):
    """`PmBrokerConfig` from flat settings. Imported lazily so the Binance
    path never needs the polymarket SDK installed."""
    from btcpredict.venues.polymarket.broker import PmBrokerConfig
    return PmBrokerConfig(
        series=getattr(settings, "pm_series", "btc-updown-5m"),
        max_book_age_s=float(getattr(settings, "pm_max_book_age_s", 2.0)),
        slippage=float(getattr(settings, "pm_slippage", 0.02)),
        confirm_budget_s=float(getattr(settings, "pm_confirm_budget_s", 2.5)),
        order_type=str(getattr(settings, "pm_order_type", "limit")),
        min_entry_px=float(getattr(settings, "pm_min_entry_px", 0.05)),
        stake_cap_usdt=float(getattr(settings, "stake_cap_usdt", 0.0)),
    )


def make_pm_bundle(*, settings: Any, book: Any = None, client: Any = None,
                   log: Callable[[str], None] | None = None,
                   round_obj: Any = None) -> VenueBundle:
    """A live Polymarket bundle.

    `book` and `client` are injected so this is exercisable without a socket,
    a key or the SDK. `round_obj`, when given, pre-seeds the round cache so a
    test can bypass discovery entirely.
    """
    from btcpredict.venues.polymarket.book import PmBook
    from btcpredict.venues.polymarket.broker import PmBroker

    bk = book if book is not None else PmBook()
    br = PmBroker(book=bk, client=client, cfg=pm_config(settings), log=log,
                  creds=pm_creds(settings))
    if round_obj is not None:
        br.remember(round_obj)
    return VenueBundle(
        name="polymarket", broker=br, book=bk,
        note=("Polymarket: 60 s Chainlink TWAP settlement (sigma^2*T/3), "
              "~58 s to redeemable after the bell, taker fee 0.07*p*(1-p)"))


def pm_creds(settings: Any = None, env: dict | None = None) -> dict:
    """The Polymarket credentials, from `settings` first.

    ONE source for the gate, the broker and any tool. `settings` is a
    pydantic-settings object that has already merged `.env`; the process
    environment has NOT, which is exactly the trap that put this bot into a
    crash-restart loop (see config.py's credential block).
    """
    import os
    e = os.environ if env is None else env

    def pick(attr: str, var: str) -> str:
        if settings is not None:
            v = getattr(settings, attr, "") or ""
            if v:
                return str(v).strip()
        return str(e.get(var, "") or "").strip()

    return {
        "private_key": pick("polymarket_private_key",
                            "POLYMARKET_PRIVATE_KEY"),
        "wallet": pick("polymarket_funder_address",
                       "POLYMARKET_FUNDER_ADDRESS"),
        "key": pick("polymarket_api_key", "POLYMARKET_API_KEY"),
        "secret": pick("polymarket_api_secret", "POLYMARKET_API_SECRET"),
        "passphrase": pick("polymarket_passphrase",
                           "POLYMARKET_PASSPHRASE"),
        "relayer_key": pick("polymarket_relayer_api",
                            "POLYMARKET_RELAYER_API"),
        "relayer_address": pick("polymarket_relayer_address",
                                "POLYMARKET_RELAYER_ADDRESS"),
    }


def pm_credentials_present(settings: Any = None,
                           env: dict | None = None) -> bool:
    """Whether we can actually SIGN a Polymarket order.

    Checks the PRIVATE KEY, not just the L2 credentials. The `.env` carries
    `POLYMARKET_API_KEY/_SECRET/_PASSPHRASE`, which are L2 and **cannot sign an
    order**; presence of those alone is what made this look configured when it
    was not.

    Reads `settings` FIRST. systemd does not export `.env`, so an
    `os.environ`-only check reports "missing" for a correctly configured bot.
    """
    c = pm_creds(settings, env)
    return bool(c["private_key"] and c["key"] and c["secret"]
                and c["passphrase"])


__all__ = ["VENUES", "VenueBundle", "normalise", "pm_config",
           "make_pm_bundle", "pm_creds", "pm_credentials_present"]
