"""Pin the suite to a deterministic profile.

`settings` is pydantic-settings, so it reads `.env` from the working directory.
On the VPS that file is the LIVE trading profile -- and it is synced separately
from the code, so the tests there ran under `entry_mode=impulse` while the same
tests ran under `entry_mode=model` locally. Two of them failed on the server for
exactly that reason: they document MODEL-mode behaviour that the live profile
deliberately turns off.

A suite whose meaning depends on which profile happens to be deployed is not a
suite. These pins restore the config.py defaults; a test that wants the other
branch sets it explicitly (see test_impulse_fastpath.py), and its own
monkeypatch wins because it runs after this fixture.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_entry_mode(monkeypatch):
    """Test the model path unless a test says otherwise, whatever .env holds."""
    monkeypatch.setattr("btcpredict.config.settings.entry_mode", "model")
    monkeypatch.setattr("btcpredict.config.settings.impulse_on_tick", False)
