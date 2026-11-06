"""Stage configuration cannot turn live trading on."""

from __future__ import annotations

import pytest

from sterling.config import StageConfig, parse_stage_config
from sterling.errors import ConfigError, LiveTradingRejected


def test_simulated_config_is_accepted() -> None:
    config = parse_stage_config({"live": False, "mode": "simulated"})
    assert config == StageConfig(live=False, mode="simulated")


def test_live_true_is_rejected() -> None:
    with pytest.raises(LiveTradingRejected):
        parse_stage_config({"live": True, "mode": "simulated"})


def test_live_mode_is_rejected() -> None:
    with pytest.raises(LiveTradingRejected):
        parse_stage_config({"live": False, "mode": "live"})
    with pytest.raises(LiveTradingRejected):
        parse_stage_config({"live": False, "trading_mode": "live"})


def test_live_must_be_explicit() -> None:
    with pytest.raises(ConfigError):
        parse_stage_config({"mode": "simulated"})


def test_broker_settings_are_rejected() -> None:
    with pytest.raises(ConfigError, match="broker"):
        parse_stage_config({"live": False, "broker": "none", "api_key": "x"})


def test_stage_config_constructor_rejects_live() -> None:
    with pytest.raises(LiveTradingRejected):
        StageConfig(live=True, mode="simulated")
