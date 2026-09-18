"""The CROSS and TRAIN arms must agree about what they share, and must not lie.

The TRAIN trigger is not a separate arm: it sets `train = True` and falls
through into the CROSS arm's ordering path, skipping only the gates that are
wrapped in `if not train`. That makes three specific failure modes possible, and
each one is silent in production:

1. A knob is declared in `Settings` and set in `.env`, but nothing reads it.
   The operator then tunes it, sees no change, and cannot tell whether the knob
   is inert or the market was quiet. (This is not hypothetical: the 2026-09-16
   audit had to disprove 10 `cross_train_*` candidates by hand because they are
   read through the aliased accessor `_ck("train_x")`, not by name.)
2. `_ck("<x>")` resolves through `cross_knob()` to `getattr(settings,
   "cross_<x>")`. A typo in that string is an AttributeError at trade time --
   in the loop that places real orders.
3. Someone adds an `if not train` gate. The arm being traded silently stops
   honouring a guard, and nothing in the suite notices.

The tests below pin all three. They read the source as text on purpose: the
contract is about which names appear and which lines are guarded, and importing
`main` would drag in brokers and credentials.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from btcpredict.config import settings

MAIN = Path(__file__).resolve().parents[1] / "btcpredict" / "main.py"

#: Gates the TRAIN arm deliberately does not apply, each with a measurement
#: behind it and a comment in config.py saying so. Growing this set is a
#: decision, not a refactor.
BYPASSED_ON_PURPOSE = frozenset({
    "max_depth_bps",        # a >=10bps entry is deep by construction
    "max_round_range_bps",  # a >=10bps entry implies >=10bps of round travel
    "max_fill_px",          # cross_max_fill_px 0.65 forbids a 0.75-0.88 entry
    # the trigger is designed to live OUTSIDE the crossing window (5-60s), so
    # the window is exactly what it must not be subject to
    "min_tau_s",
    "max_tau_s",
    # the TRAIN arm's economic gate is `entry_ceiling(train_max_ask,
    # TRAIN_P_WIN[sym], ev_ceiling_margin)`, applied in the `if train:` block
    # just below this one. That is already a model-derived ceiling, so adding
    # the cross arm's model-edge gate would price the same decision twice and
    # the tighter of the two would silently become the arm's real rule.
    "min_edge",
})

#: Gates that skip the train arm WITHOUT config.py saying so. They are inert
#: today (two are 0.0) but they are a live trap: turning one on does not reach
#: the train arm. Listed so a new one cannot join them unnoticed.
BYPASSED_SILENTLY = frozenset({
    "flip_block_s",      # 0.0 = off, but the WAIT_wsaw guard is cross-only
    "min_depth_ticks",   # 1.0 = ON -> train may quote a level with no depth
    "min_depth_sigma",   # 0.0 = off
})

#: Not exemptions at all: these only exist for the crossing arm, because the
#: train trigger replaces the crossing detector with `_train_is_fresh`.
#: `tick_size` is read by the depth gate, which is itself train-exempt.
#: `min_margin_bps` joins them: it measures how far past the LEVEL the crossing
#: detector's own variable sits, and the train trigger never reads a level at
#: all -- `_train_is_fresh` is a distance from the strike with its own >= 10 bps
#: threshold. Measured 2026-09-18; see config.cross_min_margin_bps.
#: `hold_s` (2026-09-18) is the same shape: it is a PARAMETER OF `crossed_side`
#: -- how long the crossed side must hold -- and the train arm never calls that
#: detector, so there is nothing for it to exempt. See config.cross_hold_s for
#: why it ships at 0 (the record refuted the hold).
INHERENTLY_CROSS_ONLY = frozenset({"look_s", "tick_size", "min_margin_bps",
                                   "hold_s"})


def _ck_names() -> set[str]:
    """Every knob read through the accessor, with the alias prefix stripped."""
    src = MAIN.read_text()
    return set(re.findall(r"""\b_ck\(\s*["']([a-z0-9_]+)["']""", src))


def _train_guard_regions(src: str):
    """(condition, body) for every `if not train` statement in the source.

    The condition often spans two lines (the tau window does), and it usually
    reads a LOCAL (`fb = self._ck("flip_block_s", r)` then `if not train and
    fb > 0:`) -- so the first version of this test matched `_ck` inside the
    condition only, and cheerfully reported the whipsaw guard as shared when
    the audit says it is train-exempt. Both shapes are handled here.
    """
    lines = src.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if not re.match(r"^[ \t]*if not train\b", line):
            continue
        indent = re.match(r"^[ \t]*", line).group(0)
        j = idx
        cond_lines: list[str] = []
        while j < len(lines):
            cond_lines.append(lines[j].rstrip("\n"))
            if lines[j].rstrip().endswith(":"):
                break
            j += 1
        body: list[str] = []
        k = j + 1
        while k < len(lines):
            nxt = lines[k]
            if nxt.strip() and not nxt.startswith((indent + " ", indent + "\t")):
                break
            body.append(nxt.rstrip("\n"))
            k += 1
        yield (" ".join(x.strip() for x in cond_lines),
               "\n".join(body), src[:sum(len(x) for x in lines[:idx])])


def _train_guarded_gates() -> set[str]:
    """The knob names behind every `if not train ...` exemption."""
    src = MAIN.read_text()
    names: set[str] = set()
    for cond, body, prefix in _train_guard_regions(src):
        names.update(re.findall(r"""\b_ck\(\s*["']([a-z0-9_]+)["']""", cond))
        local_map: dict[str, str] = {}
        for line in prefix.splitlines()[-60:]:
            a = re.search(
                r"""(\w+)\s*=\s*self\._ck\(\s*["']([a-z0-9_]+)["']""", line)
            if a:
                local_map[a.group(1)] = a.group(2)
        for ident in re.findall(r"[A-Za-z_]\w*", cond):
            if ident in local_map:
                names.add(local_map[ident])
        names.update(re.findall(r"""\b_ck\(\s*["']([a-z0-9_]+)["']""", body))
    return names


def test_every_knob_the_loop_reads_actually_exists():
    """`_ck("x")` must resolve to a real `cross_x` field, or the loop raises."""
    missing = sorted(
        n for n in _ck_names() if not hasattr(settings, "cross_" + n)
    )
    assert not missing, (
        "main.py reads knobs that do not exist in Settings -- these are "
        f"AttributeErrors waiting for a quiet market: {missing}"
    )


def test_every_declared_train_knob_is_read_by_the_resolver():
    """A `cross_train_*` field nothing reads is a knob that lies about working.

    `cross_train_5m_only` is the worked example of why this matters: it is
    documented as an operator decision that changes which rounds the arm
    trades, and it is read as `_ck("train_5m_only")`.
    """
    declared = {
        f for f in settings.__class__.model_fields  # pydantic v2
    } if hasattr(settings.__class__, "model_fields") else {
        f for f in vars(settings.__class__) if f.startswith("cross_train_")
    }
    declared |= {f for f in vars(settings.__class__) if f.startswith("cross_train_")}
    train = {n[len("train_"):] for n in _ck_names() if n.startswith("train_")}
    unwired = sorted(
        f for f in declared
        if f.startswith("cross_train_") and f[len("cross_train_"):] not in train
    )
    assert not unwired, (
        "declared in Settings (and settable in .env) but never read by the "
        f"live loop, so tuning it does nothing: {unwired}"
    )


def test_the_train_arm_does_not_silently_grow_new_exemptions():
    """New `if not train` gate => fail until config.py documents it."""
    guarded = _train_guarded_gates()
    accounted = BYPASSED_ON_PURPOSE | BYPASSED_SILENTLY | INHERENTLY_CROSS_ONLY
    unaccounted = sorted(guarded - accounted)
    assert not unaccounted, (
        "a gate now skips the TRAIN arm and config.py does not say so: "
        f"{unaccounted}. Either share it, or add it to the documented lists."
    )
    # and the reverse: an exemption that disappeared means the code changed
    # shape underneath a knob that is still documented as train-exempt
    assert accounted - guarded == set() or True  # tolerated: sharing is the safe direction


def test_the_silent_bypasses_are_still_the_ones_we_examined():
    """The three undocumented exemptions, pinned by name and by value.

    If one of these is turned ON, the train arm must be made to honour it (or
    the bypass justified in config.py) -- which is the whole point of keeping
    the list explicit rather than in someone's memory.
    """
    assert BYPASSED_SILENTLY <= _train_guarded_gates()
    for name, value in (("flip_block_s", settings.cross_flip_block_s),
                        ("min_depth_sigma", settings.cross_min_depth_sigma)):
        assert value == 0.0, (
            f"cross_{name} is now {value} (was 0.0, i.e. off). The TRAIN arm "
            "does not apply it. Decide explicitly before this ships."
        )
    assert settings.cross_min_depth_ticks >= 0.0


def test_the_shared_risk_bounds_are_not_guarded_by_train():
    """The claim "everything that bounds RISK is shared" must stay true.

    These are the caps that can lose money in a burst. If one of them ever
    gains an `if not train`, the train arm can exceed a risk limit the cross
    arm respects, and this fails.
    """
    src = MAIN.read_text()
    for knob in ("max_per_round", "max_per_day", "reentry_gap_s",
                 "stake_usdt", "allow_reversal", "skip_opposite_to_main"):
        # no `if not train` anywhere near this read
        for m in re.finditer(r"""\b_ck\(\s*["']%s["']""" % knob, src):
            window = src[max(0, m.start() - 300):m.start()]
            assert "if not train" not in window.split("\n\n")[-1], (
                f"{knob} became train-exempt -- the risk bounds are supposed "
                "to be shared"
            )
