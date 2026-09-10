"""Configuration. Values come from .env, overridable by real environment vars."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# Public prediction-market feed (unauthenticated, reverse-engineered from the web app).
BAPI_BASE = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/prediction"
MARKET_LIST_URL = f"{BAPI_BASE}/market/list"
MARKET_DETAIL_URL = f"{BAPI_BASE}/market/detail"

SPOT_REST = "https://api.binance.com"
SPOT_WS = "wss://stream.binance.com:9443/ws"

# The 5-minute BTC up/down series.
EVENT_SLUG = "btc-up-or-down-5m"
ROUND_SECONDS = 300

# Markets to record, as (slug prefix, Binance symbol, round seconds).
# BTC 5m is the flagship and is efficiently priced; the 15m markets carry
# 30-450x less volume, which is where looser pricing is more likely.
# 5-minute markets only. BTC 5m is the flagship and was shown to be efficiently
# priced (the market beats our model near expiry, p=0.008). ETH 5m runs on the
# same cadence with far less volume, so it is the remaining untested candidate.
MARKETS = [
    ("btc-updown-5m", "BTCUSDT", 300),
    ("eth-updown-5m", "ETHUSDT", 300),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- credentials -------------------------------------------------------
    # The repo's original .env used lowercase `binance_api_key` / `secret_key`.
    # Both those and the canonical names are accepted; canonical wins.
    binance_api_key: str = ""
    binance_api_secret: str = ""
    secret_key: str = ""

    # --- mode --------------------------------------------------------------
    trading_mode: Literal["record", "paper", "live"] = "record"

    # --- sizing ------------------------------------------------------------
    # "edge": the weakest accepted signal (edge == min_edge) is staked at
    #   min_stake_usdt, the strongest (edge >= stake_edge_hi) at max_stake_usdt,
    #   linear in between. "kelly": fractional Kelly, and a Kelly value below
    #   the venue minimum SKIPS the trade instead of rounding up to full size.
    #   "fraction": a fixed PERCENTAGE of the live bankroll (stake_fraction),
    #   floored at the venue minimum and capped by stake_cap_usdt. Unlike the
    #   band, the stake shrinks as the bankroll shrinks, which is the whole
    #   point: with fixed dollars the risk FRACTION rises exactly when losing.
    #   A fraction that cannot reach the venue minimum SKIPS the trade rather
    #   than rounding up, because rounding up silently raises the real fraction.
    sizing_mode: Literal["edge", "kelly", "fraction"] = "edge"
    # Which measure of "signal strength" drives the stake inside the band:
    #   edge -> the gated, post-haircut edge   (min_edge .. stake_edge_hi)
    #   ev   -> expected profit per $1 staked  (stake_ev_lo .. stake_ev_hi)
    # Measured on the 61 recorded accepted trades, the edge metric is the
    # weakest candidate: its top quartile realised WR 0.667 against 0.933 for
    # the third quartile, i.e. the largest claims are the most inflated --
    # exactly what edge_haircut documents. Sizing on ev/$, which prices the
    # payoff asymmetry in, roughly doubles recorded PnL for ~35% more
    # drawdown. Kept on "edge" until the live sample says otherwise.
    sizing_metric: Literal["edge", "ev"] = "edge"
    min_stake_usdt: float = 5.0
    max_stake_usdt: float = 20.0
    # sizing_mode="fraction" knobs. 4% of bankroll, chosen on DRAWDOWN rather
    # than return: the old $5-10 band risked 9.0% of a $75 account per trade,
    # and that fraction does not fall as the account shrinks. Simulated over the
    # recorded trades (3000 paths, 19 trades/day), at 30 days:
    #   band $5-10   median DD 29.7%   p95 DD 72.4%   p95 DD 100.2% at +0.15/$1
    #   f=4%         median DD 23.5%   p95 DD 35.6%   p95 DD  49.1% at +0.15/$1
    # and 4% also earns more in the central case ($1,818 vs $910), so it
    # dominates the band on both axes there. The drawdown gain holds at BOTH
    # edge assumptions because variance is far better estimated from 70 trades
    # than the mean is; the profit gain does not.
    stake_fraction: float = 0.04
    # Ceiling on the fractional stake, so compounding cannot run the stake up
    # to a size the venue's book cannot absorb in one FOK order. Also the
    # ceiling once a sizing tilt is on.
    stake_cap_usdt: float = 25.0
    # Sizing tilt: multiply the stake by a factor that depends on how undecided
    # the round still is, then re-clamp. Available in every sizing mode, so it
    # redistributes the stake without having to change the base rule.
    #   none  - no tilt
    #   inv_z - stake UP when |z| is small. Justified on the recording: per-$1
    #           return FALLS with |z| (+1.03, +0.49, +0.06, +0.17 by quartile)
    #           -- the mirror of Polymarket, whose CLOB prices small moves
    #           correctly. Out of sample, at matched average exposure, every
    #           tilt exponent from 0.5 to 1.5 beat flat on BOTH slices (test
    #           $134 -> $157..$314) with the gain monotone in the exponent, so
    #           this is a plateau and not a fitted peak. It costs drawdown:
    #           -9.8% flat vs -10.8% to -22% tilted.
    # Turning it on widens the effective stake range beyond [min_stake_usdt,
    # max_stake_usdt] up to [min_order_usdt, stake_cap_usdt].
    stake_tilt: Literal["none", "inv_z"] = "none"
    # The multiplier is clamp(reference/|z|, tilt_min, tilt_max). NOTE the clip
    # makes its MEAN exceed reference/median|z| -- with the recording's |z|
    # distribution, reference 0.65 leaves the mean multiplier at 1.30, i.e. a
    # silent 30% stake increase. 0.30 is the value that makes the mean
    # multiplier 1.0 on the 70 replayed trades, so turning the tilt on changes
    # only the SHAPE of the bets, which is the part that was tested at matched
    # exposure: same mean stake ($6.76 vs $6.83), PnL +$385.57 vs +$252.39
    # (+53%), drawdown -$22.81 vs -$20.14, PnL/DD 16.9 vs 12.5.
    # Caveat: |z| = |ln(S/K)|/(sigma*sqrt(tau/60)) shrinks when volatility
    # rises, so a fixed reference drifts exposure with the regime. Re-derive it
    # if the realised |z| distribution moves.
    tilt_reference_z: float = 0.30
    tilt_min: float = 0.5            # bounds, so one trade cannot dominate
    tilt_max: float = 2.0
    # Edge at which the maximum stake is reached. The gate accepts from
    # min_edge up; recorded accepted edges ran 0.12 -> 0.22.
    stake_edge_hi: float = 0.22
    # The same anchors for the ev/$ metric: 5 cents of expected profit per
    # dollar at the floor, 40 cents at the ceiling.
    stake_ev_lo: float = 0.05
    stake_ev_hi: float = 0.40
    # Flat fallback, used only when sizing_mode is not "edge" and no stake can
    # be computed.
    stake_usdt: float = 10.0
    # Venue minimum order size. Measured, not assumed: 54 live orders of exactly
    # $1.00 filled with real order ids (1.04-3.86 shares), so the floor is $1.
    # The old $5 was self-inflicted and leaked into the balance gate and the
    # Kelly minimum, silently blocking stakes the venue would have accepted.
    min_order_usdt: float = 1.0
    kelly_fraction: float = 0.25
    # Venue tolerance on price movement between quote and fill. 100 bps was
    # producing FOK failures ("Failed to execute the market order") when the
    # book moved mid-flight; 300 bps cuts those while the 0.08 edge floor
    # still leaves room for a 3% slip on a winning trade.
    slippage_bps: int = 300
    # A FOK order that fails is usually just the book moving between the quote
    # and the execution -- transient, and worth one more shot with a FRESH
    # quote rather than sitting out the round. Total attempts per decision,
    # including the first; 1 disables retrying. Each attempt costs a quote plus
    # an order round trip (~2s), so 3 is what fits inside a 10s candle-open
    # window. Retries only ever follow a failure the venue confirmed, never an
    # order whose outcome is unknown (see execution/live.RETRYABLE_FAILURES).
    order_max_attempts: int = 3
    # Small pause between attempts: re-quoting into the same instant that just
    # failed tends to fail the same way.
    order_retry_delay_s: float = 0.75
    # Backoff after all attempts are spent, so a persistent rejection cannot
    # re-fire on every 1 Hz tick for the rest of the round.
    order_cooldown_s: float = 30.0
    # Kelly sizes against this. Kept explicit rather than derived from the stake,
    # so raising the bankroll is one edit and sizing scales as intended.
    bankroll_usdt: float = 40.0

    # --- edge gate ---------------------------------------------------------
    # Fair-value model producing P(Up). "digital" is the original closed-form
    # model; the rest are the arena bots ported from polymarket-bot-arena
    # (see btcpredict/models/__init__.py for the full list).
    pricing_model: str = "digital"
    # Opt-in per-side hybrid gate: Down candidates are priced by
    # `pricing_model`, Up candidates by `up_model` (prototype: drift_digital).
    # Equal names disable the hybrid and keep the single-model gate.
    up_model: str = "digital"
    # Rising-regime gate for Up: skip Up when the 1h log return is above this
    # (0 disables). Measured on the recording: Up WR 0.50 in rising hours vs
    # 0.82 flat/down; the gate recovers ~+$3 and flattens the drawdown.
    up_regime_gate: float = 0.0
    # Fallback only: the fee comes from the measured curve
    # fee_bps = 180 * min(1, (1-px)/px) (see edge/signal.py). 180 bps is the
    # curve's cap, so it is the conservative fallback when no fee is known.
    fallback_fee_bps: int = 180
    # With the authenticated order book we trade against the real ask, so no
    # spread guess is needed. This only applies when the book is unavailable.
    assumed_half_spread: float = 0.005
    safety_margin: float = 0.01
    # Never buy a favourite richer than this: the payoff asymmetry out there
    # exceeds what the model's calibration can support.
    max_quote: float = 0.90
    # Fee is per-price: fee_bps = 180 * min(1, (1-px)/px), capped at 180 bps
    # below px 0.50. Cheap longshots pay the cap; favourites are cheap. This
    # floor and the re-sweep under the true curve agree on 0.25 (+$8.98 vs
    # +$7.66 at 0.15, +$5.90 at 0.35 on the 94-round recording).
    # Signal-strength floor, in units of the remaining move:
    # |ln(spot/strike)| / (sigma*sqrt(tau/60)). 0.0 = off.
    # Motivated by an independent venue (Polymarket 5m crypto, 1,292 trades,
    # 2026-03-06..12) where per-trade return rises monotonically with |z| and
    # the weak-signal trades actually LOSE money. It does NOT reproduce here:
    # in tools/walk_forward --sweep moneyness every floor from 0.25 to 1.50
    # scores worse than 0.0 on both train and test (train +0.303/trade at 0.0
    # vs +0.027 at 0.5; total PnL +15.47 -> +0.96). Our edge comes from the
    # venue's quote lagging spot, which is largest exactly when the move is
    # SMALL; Polymarket's CLOB prices those rounds correctly (realised WR 0.374
    # against its own price of 0.359). Kept at 0.0, do not re-tune without a
    # bigger recording.
    min_moneyness_sd: float = 0.0
    min_quote: float = 0.25
    # Walk-forward check (tools/walk_forward.py, 60/40 time split): widening
    # this to 0.20 looks better on the pooled window (+35.8 vs +30.3 per $1)
    # but NOT out of sample -- test-slice per-trade is +0.47 at 0.20 against
    # +0.69 at 0.25, with a deeper drawdown (-2.04 vs -1.02). So 0.25 stays.
    # The code comment about sub-0.25 "losing money" predates edge_haircut.
    # Replay evidence under the measured fee curve (backtest_candle_open,
    # BTC-only): the winning variant is the ungated price-catcher -- no
    # candle-open requirement, a BIG edge floor instead. Small edges are
    # noise; big edges are the quote lag, and they can appear at any second.
    # BTC-only joint re-tune (471 rounds, 2026-09-10): live runs min_edge 0.12
    # with edge_haircut 0.85 and side_filter Down (set in .env): +$19.34,
    # mdd -$1.35, WR 0.80, halves +8.42/+10.91, bootstrap CI [+7.35,+31.50].
    min_edge: float = 0.09
    # Fraction of the model's disagreement with the venue that is treated as
    # real: p_used = quote + edge_haircut * (fair - quote).
    #
    # NOT a fitted constant, on purpose. Measured on 31h of btc-5m recordings
    # the realised edge is roughly half the claimed edge (claimed 0.096 ->
    # realised 0.061 in the first half of the window, +0.007 in the second),
    # but every band's 95% CI spans zero once errors are clustered by round,
    # and the two halves are not statistically distinguishable. So 0.5 is a
    # deliberate safety haircut, not an estimate: the sign of the bias is
    # solid (every band, both halves, the model claims more than it delivers),
    # the magnitude is not. min_edge is set against the HAIRCUT edge, so
    # changing this knob alone changes which trades qualify.
    #
    # DEFAULT IS 0.5 (a deliberate safety haircut, not a fitted constant).
    # Measured on 31h of live btc-5m recordings the realised edge is roughly
    # half the claimed edge, so believing half of the disagreement is the
    # conservative choice. min_edge is compared against the HAIRCUT edge: with
    # min_edge 0.09, a haircut of 0.5 demands a raw edge of 0.18 and cuts the
    # replayed trade count from ~155 to ~13 (WR 0.77, mdd -$1.07, both halves
    # positive). 0.7 is the more-active compromise (56 trades, mdd -$5.09).
    # Always change the pair together: 0.5 with min_edge 0.045 reproduces the
    # haircut-off selection exactly.
    edge_haircut: float = 1.0
    # Which sides to trade. "both" is the neutral default; the live choice is
    # "Down" (set in .env). Evidence (BTC-only, measured fee curve): across
    # every configuration and BOTH chronological halves, Up trades are where
    # the money is lost -- old 0.08/4/240: Up -11.07 vs Down +13.54; 0.09/2/210:
    # first half Up +0.73 vs Down +25.52, second half Up -6.62 vs Down +2.13.
    # The venue's Up side is persistently rich relative to the model (the crowd
    # is long-biased), so the model's Up edge is an illusion more often than
    # not. Down-only keeps the first half's PnL and turns the losing second
    # half positive: n=95, WR 0.60, +$26.46, mdd -$9.05, halves +23.38 / +3.08,
    # bootstrap 95% CI [+2.41, +50.97] -- the first CI here to exclude zero.
    side_filter: Literal["both", "Up", "Down"] = "both"
    # The market beats us in 88% of rounds inside the final minute (p<0.0001).
    # Trading there is a measured loss, so the window starts at 60s.
    min_tau_s: float = 60.0
    # BTC-only joint re-tune (366 rounds): with persistence=2 the tau-210 cut
    # beats 240 (+$29.18 vs +$18.69 at 0.08 edge) -- the newest candle's first
    # 30s (candle 4 at tau 240-210) is the only losing slice on clean data.
    max_tau_s: float = 210.0
    # An edge must survive this many consecutive polls before we act: a
    # single-tick edge is usually our own stale data, not a real dislocation.
    # BTC-only re-tune (366 rounds, 2026-09-09): 2 beats 4 on clean data
    # (the old persistence-4 tune was run with ETH mixed into the replay,
    # inflating it). 0.09/2/210: +$25.31, mdd -$5.58, both halves positive
    # (+$17.32 / +$7.99); 0.09/3/210's newest half is already negative.
    signal_persistence: int = 2

    # --- execution timing ---------------------------------------------------
    # OFF: the candle-open gate lost to the price-catcher on every robustness
    # metric (see min_edge above). Instead of restricting WHEN to act, the bot
    # now restricts WHAT to act on: any second of the round, if the edge is at
    # least min_edge and holds for signal_persistence polls, buy immediately.
    # Set true to go back to open-only execution; window and candle selection
    # below still apply when it is on.
    trade_on_candle_open: bool = False
    candle_open_window_s: float = 10.0
    # Which candle opens are tradeable, indexed by minutes remaining at the
    # open: 1 = final minute (tau~60s), 2 = tau~120s, 3 = tau~180s, 4 = tau~240s.
    # The replay showed tau~180 losing on its own, but excluding it just reroutes
    # those trades to later candles and got worse overall (n=54 rounds), so the
    # default keeps all candles and the knob stays for a bigger recording.
    trade_candle_opens: list[int] = [1, 2, 3, 4]

    # --- freshness ---------------------------------------------------------
    max_spot_age_s: float = 2.0
    max_quote_age_s: float = 5.0
    max_basis_usd: float = 60.0

    # --- risk --------------------------------------------------------------
    # Daily stop as a FRACTION of the bankroll: the day halts once cumulative
    # PnL reaches -pct * bankroll, measured against the CURRENT bankroll, so it
    # tightens as the account shrinks and -- with the intraday equity refresh
    # in main.Bot._refresh_bankroll -- also grows with the account.
    #
    # The percentage is the primary rule because a fixed dollar stop does not
    # mean the same thing at two account sizes. The old -$35 was ~46% of a $76
    # account but only 3.5% of a $1,000 one, where it would trip on the second
    # loser and sit the day out.
    #
    # 0.20 -- five stakes at 4% -- is the knee of the swept curve
    # (tools/project_balance.py --sweep-cap; 30 days, 44 trades/day, at four
    # edge levels from +0.25/$1 down to zero):
    #   * a daily stop CANNOT shrink a MONTHLY drawdown, at any level. medDD and
    #     p95DD are flat across the whole 5%-to-disabled range at every edge
    #     (central: 18.5-18.7% and 30.6-31.5%), because the cap resets each UTC
    #     day while a monthly drawdown accumulates ACROSS days. The lever for
    #     that is stake_fraction, not this.
    #   * what it does buy is a bound on the worst DAY. p95 worst day at 45% was
    #     16.1% / 23.4% / 33.4% (at +0.25 / +0.17 / +0.06 per $1); at 20% it is
    #     19.6% / 20.7% / 22.6% -- bounded at ~20-23% whatever the edge turns
    #     out to be.
    #   * the premium rises as the edge falls: 45% -> 20% gives up 0.2% of profit
    #     at +0.25/$1 and 0.8% at +0.17/$1, but 12% at +0.06/$1. Below ~15% the
    #     cap fires 5-15 days a month and eats real EV for no monthly benefit;
    #     above ~30% it stops buying any tail protection at all.
    # The previous 0.45 was continuity with the old -$35 stop at $76.49, not a
    # tuned number -- it let a single day take 45% of the account.
    daily_loss_cap_pct: float = 0.20
    # Optional dollar CEILING on top of the percentage, for an account large
    # enough that 20% is an unacceptable absolute loss. It takes part in the
    # same min(), and 0.0 = off, so at $76 the percentage alone binds.
    daily_loss_cap_usdt: float = 0.0
    # Deliberately the same width as max_consecutive_losses: five losses at 4%
    # compounding is ~18.5% of the bankroll, so the two rules now agree on what
    # a bad day is. This cap catches the choppy days that never string five
    # losses together; the streak rule catches the rest.
    max_consecutive_losses: int = 5
    max_open_rounds: int = 1

    # --- volatility --------------------------------------------------------
    vol_lookback_minutes: int = 240
    vol_ewma_lambda: float = 0.97
    vol_floor_1m: float = 0.00015

    # --- infrastructure ----------------------------------------------------
    poll_interval_s: float = 1.0
    db_path: Path = ROOT / "data" / "btcpredict.db"
    bsc_rpc_url: str = "https://bsc-dataseed.binance.org"
    # 1rpc.io key for BSC log access. Public BSC endpoints refuse eth_getLogs
    # ("limit exceeded"), which is what the on-chain trade reconstruction needs.
    rpcio_api_key: str = ""
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _reconcile_credentials(self) -> "Settings":
        # Fall back to the legacy `secret_key` name if the canonical one is unset.
        if not self.binance_api_secret and self.secret_key:
            self.binance_api_secret = self.secret_key
        return self

    @model_validator(mode="after")
    def _validate_pricing_model(self) -> "Settings":
        from btcpredict.models import MODEL_NAMES
        for name in (self.pricing_model, self.up_model):
            if name.strip().lower() not in MODEL_NAMES:
                raise ValueError(
                    f"unknown model '{name}'; choose from {MODEL_NAMES}")
        return self

    @property
    def has_credentials(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @property
    def fee_rate(self) -> float:
        return self.fallback_fee_bps / 10_000.0


settings = Settings()
