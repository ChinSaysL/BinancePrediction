"""Switch the live .env between saved strategy profiles.

    python -m tools.switch_env --status              # which profile is live?
    python -m tools.switch_env optimal               # apply, then verify
    python -m tools.switch_env aggressive --restart  # apply + systemctl restart
    python -m tools.switch_env optimal --dry-run     # show the diff, change nothing

Profiles live in `configs/*.env` and hold STRATEGY KNOBS ONLY -- sizing, risk
guards, pricing and the gate. They are overlays: keys that are not in the
profile are left exactly as they are, which is what keeps credentials and
endpoints out of a file that gets copied around. A profile key that is not a
real setting is refused rather than written (a typo would otherwise land in
`.env` and be quietly ignored by pydantic).

Every switch backs `.env` up to `.env.bak-<utc>-pre-<profile>`, and the new
file is load-checked through pydantic before anything restarts -- if it does
not load, the backup is put back and the switch fails.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "configs"
ENV_PATH = ROOT / ".env"
KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
# Fields that are not strategy: excluded from the "which profile is this?"
# comparison, because a profile is about how it trades, not where it connects.
IGNORED_KEYS = frozenset({"mode", "db_path", "log_level"})


def read_profile(path: Path) -> dict[str, str]:
    """KEY=VALUE pairs from a profile; comments and blank lines ignored."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = KEY_RE.match(s)
        if not m:
            raise SystemExit(f"{path}: not a KEY=VALUE line: {line!r}")
        out[m.group(1)] = s.split("=", 1)[1].strip()
    return out


def parse_env(lines: list[str]) -> dict[str, str]:
    """Every KEY=VALUE in an env file, comments and blanks skipped."""
    out: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = KEY_RE.match(s)
        if m:
            out[m.group(1)] = s.split("=", 1)[1].strip()
    return out


def check_keys(keys: dict[str, str], profile_name: str) -> None:
    """Refuse a profile key that is not a setting -- a typo must not be silent."""
    from btcpredict.config import Settings
    unknown = sorted(k for k in keys if k not in Settings.model_fields)
    if unknown:
        raise SystemExit(
            f"{profile_name}: not settings fields: {', '.join(unknown)} "
            f"(pydantic would ignore them silently)")


def restage(lines: list[str],
            profile: dict[str, str]) -> tuple[list[str], dict[str, tuple[str | None, str]]]:
    """Return the new file contents and the changes, leaving other lines alone.

    Existing keys are rewritten IN PLACE so the file keeps its shape and any
    unrelated line (credentials, endpoints, comments) survives byte-for-byte.
    """
    changes: dict[str, tuple[str | None, str]] = {}
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = KEY_RE.match(line.strip())
        if m and m.group(1) in profile:
            key = m.group(1)
            old = line.split("=", 1)[1].strip()
            seen.add(key)
            if old != profile[key]:
                changes[key] = (old, profile[key])
            out.append(f"{key}={profile[key]}")
        else:
            out.append(line)
    for key, value in profile.items():
        if key not in seen:
            changes[key] = (None, value)
            out.append(f"{key}={value}")
    return out, changes


def detect(env: dict[str, str]) -> list[str]:
    """Which saved profiles does this env file match? [] = custom."""
    hits = []
    for path in sorted(PROFILE_DIR.glob("*.env")):
        want = read_profile(path)
        if all(env.get(k) == v for k, v in want.items()
               if k not in IGNORED_KEYS):
            hits.append(path.stem)
    return hits


def load_check() -> tuple[bool, str]:
    """Import settings from the .env on disk, in a fresh interpreter."""
    r = subprocess.run(
        [sys.executable, "-c",
         "from btcpredict.config import settings as s;"
         "print(f'{s.sizing_mode} band={s.min_stake_usdt:g}-{s.max_stake_usdt:g} "
         "cap={s.stake_cap_usdt:g} frac={s.stake_fraction:g} tilt={s.stake_tilt} "
         "daily={s.daily_loss_cap_pct:g} streak={s.max_consecutive_losses}')"],
        cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout or r.stderr).strip().splitlines()[-1]


def describe(profile_name: str, changes: dict[str, tuple[str | None, str]]) -> None:
    if not changes:
        print(f"{profile_name}: already applied -- nothing to change")
        return
    print(f"{profile_name}: {len(changes)} key(s)")
    for key in sorted(changes):
        old, new = changes[key]
        print(f"  {key:<22} {old if old is not None else '(unset)':>10} -> {new}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("profile", nargs="?",
                    help="profile name from configs/, or a path to an .env file")
    ap.add_argument("--status", action="store_true",
                    help="report which profile the live .env matches")
    ap.add_argument("--list", action="store_true", help="list saved profiles")
    ap.add_argument("--env", type=Path, default=ENV_PATH,
                    help=f"env file to edit (default {ENV_PATH})")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the changes without writing or restarting")
    ap.add_argument("--restart", action="store_true",
                    help="systemctl restart btcpredict after a successful switch")
    args = ap.parse_args()

    if args.list or (args.status and not args.profile):
        for path in sorted(PROFILE_DIR.glob("*.env")):
            print(f"  {path.stem:<12} {path}")
        if not PROFILE_DIR.exists() or not list(PROFILE_DIR.glob("*.env")):
            print("  (no profiles yet)")
            return 1
        if args.status:
            env = parse_env(args.env.read_text().splitlines()) if args.env.exists() else {}
            hits = detect(env)
            print(f"\n{args.env}: matches "
                  f"{', '.join(hits) if hits else 'no saved profile (custom)'}")
            print(f"  live: mode={env.get('sizing_mode')} "
                  f"min={env.get('min_stake_usdt')} max={env.get('max_stake_usdt')} "
                  f"frac={env.get('stake_fraction')} tilt={env.get('stake_tilt')} "
                  f"daily={env.get('daily_loss_cap_pct')} "
                  f"streak={env.get('max_consecutive_losses')}")
        return 0

    if not args.profile:
        ap.error("give a profile name, or --status / --list")

    path = Path(args.profile)
    if not path.exists():
        path = PROFILE_DIR / (args.profile if path.suffix else args.profile + ".env")
    if not path.exists():
        raise SystemExit(f"no such profile: {args.profile} (see --list)")

    profile = read_profile(path)
    check_keys(profile, path.stem)
    if not args.env.exists():
        raise SystemExit(f"no env file at {args.env}")
    lines = args.env.read_text().splitlines()
    new_lines, changes = restage(lines, profile)
    describe(path.stem, changes)

    if args.dry_run or not changes:
        return 0

    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime())
    backup = args.env.with_name(f"{args.env.name}.bak-{stamp}-pre-{path.stem}")
    shutil.copy2(args.env, backup)
    tmp = args.env.with_suffix(".tmp")
    tmp.write_text("\n".join(new_lines) + "\n")
    os.replace(tmp, args.env)
    print(f"backup: {backup.name}")

    ok, detail = load_check()
    if not ok:
        shutil.copy2(backup, args.env)
        print(f"settings did not load -- restored {backup.name}\n  {detail}")
        return 1
    print(f"loaded: {detail}")

    if args.restart:
        r = subprocess.run(["systemctl", "restart", "btcpredict"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"restart failed ({r.returncode}): {r.stderr.strip()} "
                  f"-- .env is changed and takes effect on the next restart")
            return 1
        time.sleep(6)
        a = subprocess.run(["systemctl", "is-active", "btcpredict"],
                           capture_output=True, text=True)
        print(f"btcpredict: {a.stdout.strip()}")
    else:
        print("restart to apply:  systemctl restart btcpredict")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
