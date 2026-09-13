import pytest

from btcpredict.edge.signal import (FeeModel, breakeven_prob, evaluate,
                                    evaluate_hybrid, expected_value, kelly_stake,
                                    stake_for_edge, stake_for_fraction,
                                    tilt_factor)

FEE = 0.02


@pytest.mark.parametrize("q", [0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95])
def test_notional_fee_model_is_the_conservative_one(q):
    # We default to NOTIONAL because the real semantics are unconfirmed; that
    # choice is only safe if it is stricter everywhere.
    assert breakeven_prob(q, FEE, FeeModel.NOTIONAL) >= breakeven_prob(q, FEE, FeeModel.PROFIT)


@pytest.mark.parametrize("q", [0.1, 0.3, 0.5, 0.7, 0.9])
def test_expected_value_is_zero_at_breakeven(q):
    for model in (FeeModel.NOTIONAL, FeeModel.PROFIT):
        p = breakeven_prob(q, FEE, model)
        assert expected_value(p, q, FEE, model) == pytest.approx(0.0, abs=1e-9)


def test_fair_priced_market_is_a_loser_after_fees():
    # Buying at exactly fair value must lose money -- the fee has to bite.
    assert expected_value(0.5, 0.5, FEE, FeeModel.NOTIONAL) < 0


def test_marginal_edge_is_rejected():
    # 2 points of edge cannot survive a 2% fee plus a 2-point half-spread.
    s = evaluate(0.52, 0.50, 0.50, fee=FEE, half_spread=0.02, safety_margin=0.01)
    assert not s.act


def test_large_dislocation_is_taken():
    # The live-observed lag mispricing: model 0.911 against a stale 0.600 quote.
    s = evaluate(0.911, 0.600, 0.400, fee=FEE, half_spread=0.02, safety_margin=0.01)
    assert s.act and s.side == "Up" and s.ev_per_dollar > 0.4


def test_picks_the_down_side_when_up_is_rich():
    # Longshot bias: Up quoted 0.240 while the model says 0.147.
    s = evaluate(0.147, 0.240, 0.760, fee=FEE, half_spread=0.02, safety_margin=0.01)
    assert s.act and s.side == "Down"
    assert s.fair == pytest.approx(0.853)


def test_half_spread_can_veto_a_trade():
    args = dict(fee=FEE, safety_margin=0.0)
    assert evaluate(0.60, 0.50, 0.50, half_spread=0.0, **args).act
    assert not evaluate(0.60, 0.50, 0.50, half_spread=0.15, **args).act


def test_degenerate_quotes_are_refused():
    for q in (0.0, 1.0, -0.1, 1.5):
        s = evaluate(0.6, q, q, fee=FEE, half_spread=0.02, safety_margin=0.01)
        assert not s.act


def test_kelly_sizing():
    assert kelly_stake(0.45, 0.50, 100.0, 0.25, 0.0, 10.0) == 0.0     # no edge -> no bet
    assert kelly_stake(0.911, 0.60, 100.0, 0.25, 0.0, 10.0) == 10.0   # clamped to max
    assert kelly_stake(0.55, 0.50, 1.0, 0.25, 0.50, 1.0) == 0.0       # below min -> skip
    big = kelly_stake(0.90, 0.50, 100.0, 0.25, 0.0, 1000.0)
    small = kelly_stake(0.55, 0.50, 100.0, 0.25, 0.0, 1000.0)
    assert big > small                                                 # scales with edge


def test_deep_favourites_are_refused():
    """The live bot bought Down at 0.98 on a 'fair=1.000' reading.

    At q=0.98 a $1 stake wins $0.02 and needs ~98.6% accuracy to break even,
    while measured top-bucket reliability was 0.964 actual vs 0.973 predicted.
    The arithmetic edge looks positive; the trade is not.
    """
    s = evaluate(0.001, 0.02, 0.98, fee=FEE, half_spread=0.0, safety_margin=0.0,
                 max_quote=0.90, min_edge=0.0)
    assert not s.act
    assert s.side != "Down"          # the rich favourite must not be chosen

    # When the rich side is the only candidate, the reason names the cap.
    only = evaluate(0.999, 0.98, 1.5, fee=FEE, half_spread=0.0, safety_margin=0.0,
                    max_quote=0.90, min_edge=0.0)
    assert not only.act and "max_quote" in only.reason


def test_max_quote_still_allows_the_other_side():
    # Up is too rich, but Down at 0.30 is fine -- only the rich side is excluded.
    s = evaluate(0.70, 0.95, 0.30, fee=FEE, half_spread=0.0, safety_margin=0.0,
                 max_quote=0.90, min_edge=0.0)
    assert s.side == "Down"


def test_min_edge_floor():
    args = dict(fee=0.006, half_spread=0.0, safety_margin=0.0, max_quote=0.99)
    assert evaluate(0.75, 0.70, 0.30, min_edge=0.03, **args).act
    assert not evaluate(0.72, 0.70, 0.30, min_edge=0.03, **args).act


def test_missing_quote_is_rejected_not_raised():
    # One side of the book can be empty; feed data passes straight into evaluate.
    s = evaluate(0.6, None, 0.4, fee=FEE, half_spread=0.0, safety_margin=0.0)
    assert s.side == "Down"
    assert not evaluate(0.6, None, None, fee=FEE, half_spread=0.0,
                        safety_margin=0.0).act


def test_min_quote_floor_rejects_cheap_longshots():
    """Fee was 180 bps on a 0.27 quote vs 45-64 bps higher up, so the cheapest
    contracts carry the worst fee rate. The floor keeps us out of that zone."""
    args = dict(fee=FEE, half_spread=0.0, safety_margin=0.0, min_edge=0.0)
    assert evaluate(0.30, 0.10, 0.90, min_quote=0.0, **args).side == "Up"
    s = evaluate(0.30, 0.10, 0.90, min_quote=0.15, **args)
    assert s.side != "Up"


def test_fee_curve_matches_the_venue():
    """Regression lock re-measured 2026-09-12 on the 311 live fills that paid.

    Shape (1-px)/px, capped at the coefficient below px 0.50 -- confirmed. The
    LEVEL was 15-21% low in every band with the old 180 constant, so the
    coefficient is now the venue's own reported rate (fee_rate_bps=200, stated
    on 278,011 of 278,929 recorded ticks). The 0.90+ band has n=6 and both
    coefficients fall short of its 19.8 mean, so it is checked loosely.

    Passing fee_rate_bps/1e4 as a flat `fee=` would be a bug: that is the
    coefficient, not the charge.
    """
    from btcpredict.edge.signal import fee_bps_for_price
    for px, charged in ((0.45, 209.9), (0.55, 175.1), (0.65, 116.0),
                        (0.75, 72.7), (0.85, 38.1)):
        modelled = fee_bps_for_price(px)
        assert 0.75 * charged <= modelled <= 1.25 * charged, (px, modelled, charged)
        # never under-charge a taker by more than 15%
        assert modelled >= 0.85 * charged, (px, modelled, charged)
    assert fee_bps_for_price(0.50) == pytest.approx(200.0)
    assert fee_bps_for_price(0.90) == pytest.approx(22.2, abs=0.5)
    assert fee_bps_for_price(0.60) > fee_bps_for_price(0.80) > fee_bps_for_price(0.95)


def test_edge_haircut_shrinks_the_claim_toward_the_venue_price():
    """Only part of the model's disagreement with the venue is real edge.

    Measured on recorded btc-5m quotes, the shortfall grows WITH the claim, so
    the correction has to be proportional (a haircut), not a constant
    subtraction like safety_margin.
    """
    from btcpredict.edge.signal import FeeModel, evaluate

    kw = dict(fee=0.0, half_spread=0.0, safety_margin=0.0,
              fee_model=FeeModel.NOTIONAL, min_quote=0.0, max_quote=1.0)
    # model says Up is 0.80 against a 0.60 ask: a claimed edge of 0.20.
    full = evaluate(0.80, 0.60, 0.40, **kw)
    half = evaluate(0.80, 0.60, 0.40, edge_haircut=0.5, **kw)
    assert full.side == half.side == "Up"
    assert full.edge == pytest.approx(0.20)
    assert half.edge == pytest.approx(0.10)          # 0.60 + 0.5*(0.80-0.60)
    # the haircut must reach EV too -- Kelly sizes off it, and sizing on an
    # inflated probability overbets.
    assert half.ev_per_dollar < full.ev_per_dollar
    # and it gates: a claim that only clears min_edge before the haircut fails after.
    assert evaluate(0.80, 0.60, 0.40, min_edge=0.15, **kw).act
    assert not evaluate(0.80, 0.60, 0.40, min_edge=0.15, edge_haircut=0.5, **kw).act


def test_edge_haircut_of_one_is_a_no_op():
    """The default must not change behaviour for callers that do not pass it."""
    from btcpredict.edge.signal import FeeModel, evaluate

    kw = dict(fee=0.01, half_spread=0.0, safety_margin=0.01,
              fee_model=FeeModel.NOTIONAL, min_quote=0.25, max_quote=0.90)
    a = evaluate(0.72, 0.63, 0.37, **kw)
    b = evaluate(0.72, 0.63, 0.37, edge_haircut=1.0, **kw)
    assert (a.side, a.edge, a.ev_per_dollar, a.act) == (b.side, b.edge, b.ev_per_dollar, b.act)


KW = dict(fee=0.0, half_spread=0.0, safety_margin=0.0,
          fee_model=FeeModel.NOTIONAL, min_quote=0.0, max_quote=0.90,
          min_edge=0.10)


def test_hybrid_down_candidate_comes_from_down_model():
    # Down model sees a big Down edge; up model sees none.
    sig = evaluate_hybrid(0.30, 0.62, quote_up=0.60, quote_down=0.40, **KW)
    assert sig.act and sig.side == "Down"
    assert sig.fair_up_model == 0.30


def test_hybrid_up_candidate_comes_from_up_model():
    # Up model sees a big Up edge; down model sees none.
    sig = evaluate_hybrid(0.63, 0.71, quote_up=0.60, quote_down=0.40, **KW)
    assert sig.act and sig.side == "Up"
    assert sig.fair_up_model == 0.71


def test_hybrid_discards_wrong_side_from_each_model():
    # Down model's Up call and up model's Down call must be ignored, so the
    # side always matches the model that priced it.
    sig = evaluate_hybrid(0.30, 0.70, quote_up=0.60, quote_down=0.40, **KW)
    assert sig.act and sig.side in ("Up", "Down")
    if sig.side == "Up":
        assert sig.fair_up_model == 0.70
    else:
        assert sig.fair_up_model == 0.30


def test_hybrid_identical_probabilities_equals_evaluate():
    a = evaluate_hybrid(0.72, 0.72, quote_up=0.63, quote_down=0.37, **KW)
    b = evaluate(0.72, 0.63, 0.37, **KW)
    assert (a.side, a.edge, a.act) == (b.side, b.edge, b.act)


def test_hybrid_up_not_allowed_still_takes_down():
    # Both sides qualify inside one quote (q_up + q_down well below 1) and Up
    # wins the EV pick, but Up is gated: the Down candidate must still be taken
    # rather than thrown away with it.
    both = evaluate_hybrid(0.55, 0.55, quote_up=0.25, quote_down=0.34, **KW)
    assert both.act and both.side == "Up"
    gated = evaluate_hybrid(0.55, 0.55, quote_up=0.25, quote_down=0.34,
                            up_allowed=False, **KW)
    assert gated.act and gated.side == "Down"


def test_hybrid_up_not_allowed_blocks_up_entirely():
    # Even a hugely underpriced Up must not be tradeable when it is gated.
    sig = evaluate_hybrid(0.90, 0.90, quote_up=0.35, quote_down=0.65,
                          up_allowed=False, **KW)
    assert not sig.act


def test_stake_for_edge_spans_the_configured_band():
    kw = dict(min_edge=0.12, min_stake=5.0, max_stake=20.0, edge_at_max=0.22)
    assert stake_for_edge(0.12, **kw) == pytest.approx(5.0)     # weakest signal
    assert stake_for_edge(0.22, **kw) == pytest.approx(20.0)    # strongest
    assert stake_for_edge(0.17, **kw) == pytest.approx(12.5)    # linear between
    assert stake_for_edge(0.40, **kw) == pytest.approx(20.0)    # clamped high
    assert stake_for_edge(0.01, **kw) == pytest.approx(5.0)     # clamped low


def test_stake_for_fraction_tracks_the_bankroll():
    kw = dict(fraction=0.05, min_stake=1.0, cap=25.0)
    assert stake_for_fraction(68.0, **kw) == pytest.approx(3.40)
    assert stake_for_fraction(200.0, **kw) == pytest.approx(10.0)
    # a bigger bankroll stakes more -- that is the whole point of the mode
    assert stake_for_fraction(400.0, **kw) > stake_for_fraction(200.0, **kw)


def test_stake_for_fraction_shrinks_after_losses():
    """The behaviour that separates this from a dollar band: the same
    percentage of a smaller bankroll is a smaller stake."""
    kw = dict(fraction=0.05, min_stake=1.0, cap=25.0)
    before = stake_for_fraction(68.0, **kw)
    after = stake_for_fraction(68.0 - 12 * before, **kw)
    assert after < before


def test_stake_for_fraction_respects_min_and_cap():
    kw = dict(fraction=0.05, min_stake=1.0, cap=25.0)
    assert stake_for_fraction(1000.0, **kw) == pytest.approx(25.0)   # capped
    # 5% of $20 is exactly the $1 venue floor -- still expressible
    assert stake_for_fraction(20.0, **kw) == pytest.approx(1.0)
    # below the venue minimum the fraction is NOT rounded up: rounding up
    # silently raises the real risk fraction, the one thing this mode controls
    assert stake_for_fraction(19.0, **kw) == 0.0
    assert stake_for_fraction(10.0, **kw) == 0.0
    assert stake_for_fraction(0.0, **kw) == 0.0
    assert stake_for_fraction(-5.0, **kw) == 0.0
    assert stake_for_fraction(100.0, fraction=0.0, min_stake=1.0, cap=25.0) == 0.0


def test_stake_for_fraction_never_exceeds_the_bankroll():
    assert stake_for_fraction(30.0, fraction=0.9, min_stake=1.0,
                              cap=0.0) == pytest.approx(27.0)


def test_the_floor_is_opt_in_and_stakes_the_venue_minimum():
    """Below the minimum, SKIP is the default and the floor is a choice.

    The venue's floor is $1 at any account size, so a $28 account can still
    trade -- it just cannot trade 2%. The stake that comes back is $1 whatever
    the fraction says, which is the whole point and the whole cost.
    """
    kw = dict(fraction=0.02, min_stake=1.0, cap=25.0)
    assert stake_for_fraction(28.02, **kw) == 0.0                  # default
    assert stake_for_fraction(28.02, **kw,
                              floor_to_minimum=True) == pytest.approx(1.0)
    # still proportional once the fraction CAN be expressed
    assert stake_for_fraction(200.0, **kw,
                              floor_to_minimum=True) == pytest.approx(4.0)
    # the floor never reaches below the minimum, and it stops at a quarter of
    # the bankroll: $1 is fine at $4 (25%), not at $2 (50%)
    assert stake_for_fraction(4.0, **kw, floor_to_minimum=True) == pytest.approx(1.0)
    assert stake_for_fraction(3.99, **kw, floor_to_minimum=True) == 0.0
    assert stake_for_fraction(2.0, **kw, floor_to_minimum=True) == 0.0
    assert stake_for_fraction(0.0, **kw, floor_to_minimum=True) == 0.0
    assert stake_for_fraction(28.02, fraction=0.0, min_stake=1.0, cap=25.0,
                              floor_to_minimum=True) == 0.0


def test_tilt_factor_rewards_undecided_rounds():
    """Small |z| means the round is still open, which is where the recording
    says our edge is (+1.03 per $1 in the lowest quartile vs +0.06 in the third)."""
    kw = dict(reference=0.65, lo=0.5, hi=2.0)
    assert tilt_factor(0.65, **kw) == pytest.approx(1.0)     # at the reference
    assert tilt_factor(0.325, **kw) == pytest.approx(2.0)    # half the reference
    assert tilt_factor(1.3, **kw) == pytest.approx(0.5)      # twice the reference
    assert tilt_factor(0.01, **kw) == pytest.approx(2.0)     # clamped high
    assert tilt_factor(99.0, **kw) == pytest.approx(0.5)     # clamped low
    # monotone: more decided -> smaller stake
    assert (tilt_factor(0.2, **kw) > tilt_factor(0.5, **kw)
            > tilt_factor(1.0, **kw))


def test_tilt_factor_is_inert_without_information():
    kw = dict(reference=0.65, lo=0.5, hi=2.0)
    assert tilt_factor(0.0, **kw) == 1.0        # no |z| -> no tilt
    assert tilt_factor(-1.0, **kw) == 1.0
    assert tilt_factor(0.5, reference=0.0, lo=0.5, hi=2.0) == 1.0
    assert tilt_factor(0.5, reference=0.65, lo=0.5, hi=0.0) == 1.0


def test_tilt_redistributes_rather_than_raises():
    """At the reference |z| the tilt must be exactly neutral, so turning it on
    does not raise the average stake by itself."""
    kw = dict(reference=0.65, lo=0.5, hi=2.0)
    assert tilt_factor(0.65, **kw) * 5.0 == pytest.approx(5.0)


def test_stake_for_edge_is_monotone():
    kw = dict(min_edge=0.12, min_stake=5.0, max_stake=20.0, edge_at_max=0.22)
    stakes = [stake_for_edge(e, **kw) for e in
              (0.12, 0.14, 0.16, 0.18, 0.20, 0.22)]
    assert stakes == sorted(stakes)
    assert len(set(stakes)) == len(stakes)


def test_stake_for_edge_flat_band_returns_floor():
    assert stake_for_edge(0.20, min_edge=0.12, min_stake=10.0,
                          max_stake=10.0, edge_at_max=0.22) == pytest.approx(10.0)


def test_hybrid_discard_reason_names_the_discarded_side():
    # The discard path must not reuse the discarded candidate's own "ok" reason:
    # that made non-trades indistinguishable from trades in the decision log.
    # Here the down model acts on Up and the up model acts on Down, so nothing
    # survives the per-side rule.
    sig = evaluate_hybrid(0.80, 0.35, quote_up=0.50, quote_down=0.50, **KW)
    assert not sig.act and sig.side is None
    assert sig.reason != "ok"
    assert "discarded" in sig.reason and "Up" in sig.reason
