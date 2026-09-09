import math
import tempfile
from pathlib import Path

import pytest

from btcpredict.feeds.market_feed import parse_round
from btcpredict.pricing.digital import fair_value
from btcpredict.store.db import Store

# A real payload shape captured from the live endpoint, trimmed to what we read.
SAMPLE = {
    "marketTopicId": 5436179,
    "slug": "btc-updown-5m-1788871500",
    "eventSlug": "btc-up-or-down-5m",
    "symbol": "BTCUSDT",
    "startDate": 1788871500000,
    "endDate": 1788871800000,
    "feeRateBps": 200,
    "liveStatus": "ENDED",
    "status": "RESOLVED",
    "variantData": {
        "type": "CRYPTO_UP_DOWN",
        "startPrice": 78333.995,
        "endPrice": 78399.875,
        "priceFeedId": "0x0003ef42",
        "priceFeedProvider": "CHAINLINK",
    },
    "markets": [{
        "marketId": 10239094,
        "conditionId": "0x3d748fc2",
        "status": "RESOLVED",
        "tradingStatus": "CLOSED",
        "liquidity": 33819.04,
        "tradeVolume": 2088.18,
        "outcomes": [
            {"name": "Up", "price": 1.0, "chance": 1.0, "index": 0,
             "marketOutcomeId": 23883392, "tokenId": "637104", "winner": True},
            {"name": "Down", "price": 0.0, "chance": 0.0, "index": 1,
             "marketOutcomeId": 23883393, "tokenId": "427697", "winner": False},
        ],
    }],
}


def test_parses_a_real_payload():
    r = parse_round(SAMPLE)
    assert r is not None
    assert r.slug == "btc-updown-5m-1788871500"
    assert r.strike == 78333.995
    assert r.end_price == 78399.875
    assert r.feed_provider == "CHAINLINK"
    assert r.fee_rate_bps == 200
    assert r.up.token_id == "637104"
    assert r.winner_name == "Up"


def test_round_duration_is_five_minutes():
    r = parse_round(SAMPLE)
    assert r.end_ms - r.start_ms == 300_000


def test_settlement_rule_matches_the_venue():
    # end > strike -> Up. Confirmed against 10 harvested settled rounds.
    r = parse_round(SAMPLE)
    assert (r.end_price > r.strike) == (r.winner_name == "Up")


def test_tau_counts_down():
    r = parse_round(SAMPLE)
    assert r.tau_s(r.end_ms) == 0.0
    assert r.tau_s(r.start_ms) == pytest.approx(300.0)


def test_malformed_payloads_return_none_rather_than_raising():
    assert parse_round({}) is None
    assert parse_round({**SAMPLE, "markets": []}) is None
    assert parse_round({**SAMPLE, "startDate": None}) is None
    one_sided = {**SAMPLE, "markets": [{**SAMPLE["markets"][0],
                                        "outcomes": [SAMPLE["markets"][0]["outcomes"][0]]}]}
    assert parse_round(one_sided) is None


def test_missing_strike_is_tolerated():
    # Rounds appear briefly before startPrice is populated.
    r = parse_round({**SAMPLE, "variantData": {"startPrice": None, "endPrice": None}})
    assert r is not None and r.strike is None


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "t.db")
        yield s
        s.close()


def test_upsert_never_erases_a_known_value_with_null(store):
    # A late poll that has lost `strike` must not wipe the good earlier value.
    store.upsert_round({"slug": "x", "start_ms": 1, "end_ms": 2, "strike": 78333.995})
    store.upsert_round({"slug": "x", "start_ms": 1, "end_ms": 2, "strike": None,
                        "winner": "Up"})
    row = store.get_round("x")
    assert row["strike"] == 78333.995
    assert row["winner"] == "Up"


def test_unsettled_rounds_query(store):
    store.upsert_round({"slug": "done", "start_ms": 1, "end_ms": 100, "winner": "Up"})
    store.upsert_round({"slug": "open", "start_ms": 1, "end_ms": 100})
    slugs = [r["slug"] for r in store.unsettled_rounds(before_ms=200)]
    assert slugs == ["open"]


def test_counts_and_insert(store):
    store.insert("quotes", {"slug": "x", "ts_ms": 1, "up_price": 0.4})
    assert store.counts()["quotes"] == 1


def test_sigma_is_used_unscaled():
    """Regression lock: no volatility fudge factor.

    Fitting a scalar sigma multiplier improved TRAIN Brier at every horizon and
    made HELD-OUT Brier worse at every horizon (see FINDINGS.md). Any future
    change that scales sigma inside fair_value will break this test.
    """
    S, K, tau, sigma = 78450.0, 78400.0, 120.0, 0.0004
    expected = 0.5 * (1 + math.erf(
        (math.log(S / K) / (sigma * math.sqrt(tau / 60.0))) / math.sqrt(2)))
    assert fair_value(S, K, tau, sigma) == pytest.approx(expected, abs=1e-12)
