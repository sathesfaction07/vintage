"""Config. `SC_`-prefixed environment variables or a .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SC_", env_file=".env", extra="ignore")

    readings: Path = Path("data/readings.jsonl")
    labels: Path = Path("data/labels.json")
    truth: Path = Path("data/truth.jsonl")
    history: Path = Path("data/history.db")

    stale_after_seconds: int = Field(default=900, ge=0)
    """A reading whose age provably exceeds this is reported stale.

    Fifteen minutes is a judgement, not a derivation: it is short enough that a status decision
    made on it is still defensible and long enough not to flag every cache hit. It only affects
    what gets FLAGGED -- the interval arithmetic uses the measured age either way, so raising this
    hides warnings rather than changing any bound.
    """

    poll_interval_seconds: int = Field(default=14400, ge=1)
    """Expected gap between polls, used only to report how much of an interval's width is
    unavoidable. The floor on attribution width is the poll interval; anything above it is
    staleness.
    """

    # No list-typed fields anywhere in here on purpose: Pydantic Settings parses those as JSON, so
    # `SC_X=a,b` would fail with a decode error that reads like a bug in the tool rather than a bad
    # value. Every path is a single value.
