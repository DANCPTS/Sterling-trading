"""Research-only multi-market simulator.

This package is not an execution path. It does not submit orders, and the
operational desk rules and Stage 2 policy are not loaded or modified here.
Importing it from Gate 2, the placeholders, or the operational risk package
is rejected at run time.
"""

from sterling.research.engine import simulate
from sterling.research.instruments import load_candidate_universe
from sterling.research.isolation import EXECUTION_PERMISSION, RESEARCH_MODE
from sterling.research.risk_config import load_research_limits

__all__ = [
    "EXECUTION_PERMISSION",
    "RESEARCH_MODE",
    "load_candidate_universe",
    "load_research_limits",
    "simulate",
]
