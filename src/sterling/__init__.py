"""Offline simulated GBP ledger, desk rules, and Stage 2 Gate 1 risk contracts."""

from sterling.config import StageConfig, parse_stage_config
from sterling.ledger.ledger import Ledger
from sterling.rules.loader import load_default_rules, load_rules
from sterling.rules.model import DeskRules

__version__ = "0.2.0"

__all__ = [
    "DeskRules",
    "Ledger",
    "StageConfig",
    "__version__",
    "load_default_rules",
    "load_rules",
    "parse_stage_config",
]
