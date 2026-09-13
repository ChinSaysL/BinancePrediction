"""Profiles must be switchable in one command, and must not lie about it.

`make config PROFILE=optimal` is the only thing standing between a decision and
the live bot, so the pieces that can silently do the wrong thing are pinned
here: keys are rewritten in place (credentials and comments survive), a typo is
refused instead of written, detection matches the live file, and the shipped
profiles say what they claim to say.
"""
from pathlib import Path

import pytest

from tools.switch_env import PROFILE_DIR, check_keys, detect, parse_env, read_profile, restage

SAMPLE = """\
# live settings
mode=live
binance_api_key="secret-kept-here"
sizing_mode=fraction

# sizing
stake_fraction=0.02
daily_loss_cap_pct=0.20
"""


def test_profile_overlay_rewrites_in_place_and_appends_the_rest():
    lines = SAMPLE.splitlines()
    profile = {"sizing_mode": "edge", "stake_fraction": "0.12",
               "max_consecutive_losses": "0", "daily_loss_cap_pct": "0.0"}
    new, changes = restage(lines, profile)
    out = parse_env(new)
    assert out["sizing_mode"] == "edge"
    assert out["stake_fraction"] == "0.12"
    assert out["max_consecutive_losses"] == "0"
    assert changes["sizing_mode"] == ("fraction", "edge")
    assert changes["max_consecutive_losses"] == (None, "0")
    # untouched, and still byte-for-byte where it was
    assert out["binance_api_key"] == '"secret-kept-here"'
    assert new[1] == "mode=live"
    assert "# sizing" in new


def test_only_the_profiled_keys_change():
    lines = SAMPLE.splitlines()
    _new, changes = restage(lines, {"sizing_mode": "edge"})
    assert set(changes) == {"sizing_mode"}


def test_a_key_that_is_not_a_setting_is_refused():
    with pytest.raises(SystemExit, match="not settings fields"):
        check_keys({"sizing_modes": "edge"}, "typo.env")


def test_the_shipped_profiles_are_real_and_complete():
    """Every key must be a setting, and both profiles must state the knobs that
    decide how it trades -- an implicit value is one config.py edit away from
    silently changing a profile nobody touched."""
    import btcpredict.config as cfg

    for name in ("optimal", "optimal_guarded", "optimal_slip100",
                 "aggressive"):
        profile = read_profile(PROFILE_DIR / f"{name}.env")
        check_keys(profile, name)
        for required in ("sizing_mode", "daily_loss_cap_pct",
                         "max_consecutive_losses", "stake_tilt",
                         "min_stake_usdt", "max_stake_usdt", "stake_cap_usdt",
                         "stake_floor_to_minimum", "pricing_model", "up_model"):
            assert required in profile, f"{name} does not pin {required}"
    # the profiles differ exactly where they are meant to
    opt = read_profile(PROFILE_DIR / "optimal.env")
    grd = read_profile(PROFILE_DIR / "optimal_guarded.env")
    agg = read_profile(PROFILE_DIR / "aggressive.env")
    assert opt["sizing_mode"] == "fraction" and agg["sizing_mode"] == "edge"
    assert float(opt["stake_fraction"]) > 0 and opt["stake_tilt"] == "inv_z"
    assert agg["min_stake_usdt"] == agg["max_stake_usdt"]
    assert float(agg["daily_loss_cap_pct"]) == 0.0
    assert int(agg["max_consecutive_losses"]) == 0
    # optimal = the same sizing with the guards OFF; the guarded one is kept
    # separately, because that is the configuration its era's numbers describe
    for key in ("sizing_mode", "stake_fraction", "stake_tilt", "stake_cap_usdt",
                "stake_floor_to_minimum"):
        assert opt[key] == grd[key], key
    assert float(opt["daily_loss_cap_pct"]) == 0.0
    assert int(opt["max_consecutive_losses"]) == 0
    assert float(grd["daily_loss_cap_pct"]) > 0
    assert int(grd["max_consecutive_losses"]) == 5
    # the A/B arms differ in exactly one knob, so a switch between them is a
    # clean experiment rather than a confounded one
    slip = read_profile(PROFILE_DIR / "optimal_slip100.env")
    assert {k for k in opt if opt[k] != slip.get(k)} == {"slippage_bps"}
    assert int(slip["slippage_bps"]) < int(opt["slippage_bps"])
    assert cfg.Settings.model_fields["sizing_mode"]     # import stays wired


def test_detection_names_the_profile_a_file_matches():
    assert detect(parse_env(SAMPLE.splitlines())) == []
    # a file that contains the aggressive profile's keys matches it
    agg = read_profile(PROFILE_DIR / "aggressive.env")
    assert detect({**parse_env(SAMPLE.splitlines()), **agg}) == ["aggressive"]


def test_an_env_with_extra_keys_still_matches():
    """Detection is about the strategy knobs. A live .env carries credentials
    and endpoints that no profile mentions, and that must not break it."""
    opt = read_profile(PROFILE_DIR / "optimal.env")
    live = {**opt, "binance_api_key": '"x"', "mode": "live", "db_path": "data/x.db"}
    assert detect(live) == ["optimal"]


def test_read_profile_ignores_comments_and_rejects_junk(tmp_path: Path):
    p = tmp_path / "p.env"
    p.write_text("# c\n\nsizing_mode=edge\n")
    assert read_profile(p) == {"sizing_mode": "edge"}
    p.write_text("this is not a setting\n")
    with pytest.raises(SystemExit):
        read_profile(p)
