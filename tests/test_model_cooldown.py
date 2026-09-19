"""`model_cooldown_after_loss`: skip the model arm's next N signals after a loss.

Measured 2026-09-19 on 886 btc-updown-5m signals over 9 days, simulated
sequentially. Cooldown 1: 736 trades, EV/$ +0.0747 vs +0.0531, total $54.94 vs
$47.05. Calibrated null (both arms simulated) puts the EV/$ advantage at
+0.0215 against a null mean of -0.0000, p = 0.019; a temporal holdout gives
train +0.0755 / test +0.0736 against no-rule's +0.0570.
"""
import tempfile
from pathlib import Path

import pytest

import btcpredict.config as cfg
from btcpredict.main import Bot, OTHER_ARM_MODES, _is_main_arm_mode
from btcpredict.store.db import Store


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "t.db")
        yield s
        s.close()


class TestWhoseLossCounts:
    def test_the_model_arm_books_under_the_brokers_own_mode(self):
        assert _is_main_arm_mode("live")
        assert _is_main_arm_mode("paper")

    def test_the_other_arms_do_not_arm_it(self):
        """Their outcomes say nothing about this arm's regime, and they have
        their own risk budgets."""
        for m in ("cross", "train", "double", "maker", "stale_maker", "ticket"):
            assert m in OTHER_ARM_MODES
            assert not _is_main_arm_mode(m)


class TestItIsConsumedPerRound:
    """A round is evaluated many times a second. Consuming the cooldown on each
    poll would burn the whole thing inside one round and skip nothing."""

    def _bot(self):
        b = Bot.__new__(Bot)
        b._model_cooldown = 2
        b._cooldown_round = None
        return b

    def _consume(self, b, slug):
        # mirrors the gate in _record
        if b._model_cooldown > 0:
            if b._cooldown_round != slug:
                b._cooldown_round = slug
                b._model_cooldown -= 1
            return True
        return False

    def test_many_polls_on_one_round_consume_exactly_one(self):
        b = self._bot()
        for _ in range(50):
            assert self._consume(b, "btc-updown-5m-1")
        assert b._model_cooldown == 1, "one round must consume exactly one"

    def test_a_second_round_consumes_the_next(self):
        b = self._bot()
        self._consume(b, "btc-updown-5m-1")
        self._consume(b, "btc-updown-5m-2")
        assert b._model_cooldown == 0
        assert self._consume(b, "btc-updown-5m-3") is False, "cooldown is spent"


class TestItSurvivesARestart:
    def _bot(self, store):
        b = Bot.__new__(Bot)
        b.store = store
        b._model_cooldown = 0
        b._cooldown_round = None
        return b

    def _settled(self, store, mode, won):
        fid = store.insert("fills", {
            "decision_id": 1, "slug": "btc-updown-5m-1", "ts_ms": 1000,
            "mode": mode, "side": "Up", "requested_px": 0.6, "filled_px": 0.6,
            "shares": 1.0, "stake": 1.0, "fee_paid": 0.0, "order_id": "o",
            "raw": "",
        })
        store.insert("settlements", {
            "fill_id": fid, "slug": "btc-updown-5m-1", "ts_ms": 2000,
            "won": int(won), "payout": 0.0, "pnl": 1.0 if won else -1.0,
            "mode": mode, "void": 0,
        })

    def test_a_loss_re_arms_it(self, store, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_cooldown_after_loss", 1)
        self._settled(store, "live", won=False)
        b = self._bot(store); b._seed_model_cooldown()
        assert b._model_cooldown == 1, "a restart must not launder the cooldown"

    def test_a_win_does_not(self, store, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_cooldown_after_loss", 1)
        self._settled(store, "live", won=True)
        b = self._bot(store); b._seed_model_cooldown()
        assert b._model_cooldown == 0

    def test_another_arms_loss_does_not(self, store, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_cooldown_after_loss", 1)
        self._settled(store, "cross", won=False)
        b = self._bot(store); b._seed_model_cooldown()
        assert b._model_cooldown == 0

    def test_it_is_inert_when_the_knob_is_off(self, store, monkeypatch):
        monkeypatch.setattr(cfg.settings, "model_cooldown_after_loss", 0)
        self._settled(store, "live", won=False)
        b = self._bot(store); b._seed_model_cooldown()
        assert b._model_cooldown == 0, "0 must mean OFF, not a 0-length cooldown"


def test_the_gate_sits_after_the_risk_guard():
    """The cooldown must consume only signals that would OTHERWISE have traded.

    Placed before the guard it would burn on signals the guard was going to
    refuse anyway, making the live rule a different rule than the one the
    replay measured.
    """
    src = (Path(__file__).resolve().parents[1] / "btcpredict" / "main.py").read_text()
    guard = src.index("allowed, why = self.guard.check(r.slug)")
    gate = src.index("settings.model_cooldown_after_loss > 0")
    commit = src.index("acted = stake >= settings.min_order_usdt")
    assert guard < gate < commit, (
        "the cooldown gate must sit between the risk guard and the commit"
    )


def test_the_default_is_off():
    from btcpredict.config import Settings
    assert Settings.model_fields["model_cooldown_after_loss"].default == 0
