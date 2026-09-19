"""Venue selection: the translation from settings to a broker.

The failure mode this guards is a silent one. If `settings.venue` is misspelt,
or the Polymarket knobs keep their defaults while the operator changes them
expecting an effect, nothing crashes -- the bot simply trades a market whose
settlement rule the model was not fitted on. That is why `normalise` RAISES
instead of falling back.

Second guard: `POLYMARKET_API_KEY/_SECRET/_PASSPHRASE` are L2 credentials and
CANNOT sign an order. Presence of those alone made this look configured when it
was not. `pm_credentials_present` therefore insists on the private key.
"""
from __future__ import annotations

import pytest

from btcpredict.venues import registry
from btcpredict.venues.polymarket.broker import PmBroker, PmBrokerConfig


class FakeSettings:
    """Only the fields the factory reads, so a new knob cannot hide."""

    pm_series = "btc-updown-5m"
    pm_max_book_age_s = 3.5
    pm_slippage = 0.03
    pm_confirm_budget_s = 1.25


# ---------------------------------------------------------------- normalise
class TestNormalise:
    def test_the_default_is_the_incumbent_venue(self):
        # A switch must be an event, not a default.
        assert registry.normalise(None) == "binance"

    def test_it_accepts_case_and_whitespace(self):
        assert registry.normalise("  PolyMarket ") == "polymarket"

    def test_an_unknown_venue_RAISES_rather_than_falling_back(self):
        # Defaulting to the other venue would trade a market whose settlement
        # rule the model was never fitted on: PM resolves a 60 s TWAP, the
        # wrapper a point snapshot, and they disagree on 10.4% of rounds.
        with pytest.raises(ValueError, match="unknown venue"):
            registry.normalise("polymarket-clob")

    def test_the_config_default_is_binance(self):
        """THE DEFAULT, read as the default. `settings` is the RESOLVED value
        and the live `.env` sets `VENUE=polymarket`, so reading it here asserts
        which profile is deployed rather than what ships -- green locally, red
        on the VPS, and saying nothing either way."""
        from btcpredict.config import Settings
        assert Settings.model_fields["venue"].default == "binance"


# ---------------------------------------------------------------- config map
class TestSettingsToVenueConfig:
    def test_every_knob_is_carried_across(self):
        c = registry.pm_config(FakeSettings())
        assert c.series == "btc-updown-5m"
        assert c.max_book_age_s == pytest.approx(3.5)
        assert c.slippage == pytest.approx(0.03)
        assert c.confirm_budget_s == pytest.approx(1.25)

    def test_it_returns_a_real_venue_config(self):
        assert isinstance(registry.pm_config(FakeSettings()), PmBrokerConfig)

    def test_a_missing_knob_does_not_explode(self):
        class Bare:
            pass

        c = registry.pm_config(Bare())
        assert c.series == "btc-updown-5m"
        assert c.max_book_age_s > 0


# ---------------------------------------------------------------- the bundle
class _Bare:
    pm_series = "btc-updown-5m"
    pm_max_book_age_s = 2.0
    pm_slippage = 0.02
    pm_confirm_budget_s = 2.5


class TestTheBundle:
    def test_it_builds_without_a_socket_or_a_key(self):
        b = registry.make_pm_bundle(settings=_Bare(), client=object())
        assert b.name == "polymarket"
        assert isinstance(b.broker, PmBroker)
        assert b.book is b.broker.book

    def test_the_note_records_the_settlement_rule(self):
        # The rule must travel with the object: it is the thing that makes
        # every point-rule calibration cell invalid here.
        b = registry.make_pm_bundle(settings=_Bare(), client=object())
        assert "TWAP" in b.note

    def test_there_is_no_binance_websocket_task_on_this_venue(self):
        # PmBook runs its own CLOB socket; a second task would double-subscribe.
        b = registry.make_pm_bundle(settings=_Bare(), client=object())
        assert b.ws_task is None


# ---------------------------------------------------------------- drop-in
class TestThePolymarketBrokerIsDropIn:
    """The surface main.py reads, asserted in one place. A venue swap that
    misses one of these fails at the first order of a live round instead of
    here."""

    REQUIRED = ("buy", "buy_limit", "settle", "equity", "balance", "positions",
                "order_book", "best_prices", "active_orders", "cancel_orders",
                "redeem", "claimable_token_ids", "prime", "remember",
                "round_for", "get_quote")

    def test_every_method_main_py_calls_exists(self):
        b = registry.make_pm_bundle(settings=_Bare(), client=object())
        missing = [m for m in self.REQUIRED if not callable(getattr(b.broker, m, None))]
        assert missing == []

    def test_it_carries_the_attributes_main_py_reads(self):
        b = registry.make_pm_bundle(settings=_Bare(), client=object())
        for attr in ("mode", "wallet_address", "last_failure", "last_order_error"):
            assert hasattr(b.broker, attr), attr
        assert b.broker.mode == "live"


# ---------------------------------------------------------------- the L2 trap
class TestCredentialsGate:
    L2 = {"POLYMARKET_API_KEY": "k", "POLYMARKET_API_SECRET": "s",
          "POLYMARKET_PASSPHRASE": "p"}
    FULL = dict(L2, POLYMARKET_PRIVATE_KEY="0xabc")

    def test_the_L2_creds_alone_are_NOT_enough(self):
        """The exact trap: those four env vars make the venue look configured.

        L2 creds authenticate reads and cannot sign an order, so a bot that
        gated on them would start and then fail on the first POST.
        """
        assert registry.pm_credentials_present(env=self.L2) is False

    def test_the_private_key_is_what_unlocks_it(self):
        assert registry.pm_credentials_present(env=self.FULL) is True

    def test_an_empty_environment_is_not_configured(self):
        assert registry.pm_credentials_present(env={}) is False

    def test_SETTINGS_WIN_over_an_empty_environ(self):
        """The crash-restart loop, pinned as a test.

        systemd starts this bot with only PYTHONUNBUFFERED=1. pydantic-settings
        loads `.env` into `settings` and does NOT export those names, so an
        `os.environ`-only gate reported "private key missing" for a correctly
        configured bot -- and live mode then refused to start every 10 s.
        """

        class S:
            polymarket_private_key = "0xabc"
            polymarket_api_key = "k"
            polymarket_api_secret = "s"
            polymarket_passphrase = "p"

        assert registry.pm_credentials_present(S(), env={}) is True

    def test_the_real_settings_object_can_sign(self):
        # Guards the production wiring end to end: the fields exist on Settings
        # and `.env` actually populates them.
        from btcpredict.config import settings
        for attr in ("polymarket_private_key", "polymarket_api_key",
                     "polymarket_api_secret", "polymarket_passphrase"):
            assert hasattr(settings, attr), attr
        assert registry.pm_credentials_present(settings) is True

    def test_pm_creds_reports_every_field_the_sdk_needs(self):
        c = registry.pm_creds(env=self.FULL)
        assert set(c) == {"private_key", "wallet", "key", "secret",
                          "passphrase", "relayer_key", "relayer_address"}
        assert c["private_key"] == "0xabc"

    def test_an_injected_client_is_enough_to_sign(self):
        # Tests and the paper path inject a client; the env check is only for
        # the real construction path.
        b = registry.make_pm_bundle(settings=_Bare(), client=object())
        assert b.broker.enabled is True
