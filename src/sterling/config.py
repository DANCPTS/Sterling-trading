"""Stage 1 process configuration.

There is no setting that turns live trading on. ``live=true`` and ``mode=live``
are rejected, and brokerage connection keys are rejected.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sterling.errors import ConfigError, LiveTradingRejected

_BANNED_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "base_url",
        "broker",
        "endpoint",
        "password",
        "secret",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class StageConfig:
    """A validated Stage 1 configuration. ``live`` is always false."""

    live: bool
    mode: str

    def __post_init__(self) -> None:
        if self.live is not False or self.mode != "simulated":
            raise LiveTradingRejected("Live trading cannot be enabled in Stage 1. Config validation rejects live=true.")


def parse_stage_config(data: Mapping[str, object]) -> StageConfig:
    present = set(data)
    banned = sorted(present & _BANNED_KEYS)
    if banned:
        raise ConfigError("Stage 1 config cannot contain brokerage or credential settings: " + ", ".join(banned))
    if data.get("live") is True or data.get("mode") == "live" or data.get("trading_mode") == "live":
        raise LiveTradingRejected("Live trading cannot be enabled in Stage 1. Config validation rejects live=true.")
    if "live" not in data:
        raise ConfigError("config must set live to false explicitly")
    if data.get("live") is not False:
        raise ConfigError("live must be boolean false")
    mode = data.get("mode", "simulated")
    if mode != "simulated":
        raise LiveTradingRejected("Stage 1 mode must be simulated. Live trading cannot be enabled.")
    if not isinstance(mode, str):
        raise ConfigError("mode must be the string 'simulated'")
    return StageConfig(live=False, mode="simulated")
