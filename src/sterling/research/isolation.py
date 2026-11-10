"""Keep the research simulator off the operational execution path."""

from __future__ import annotations

import inspect

from sterling.research.errors import ResearchIsolationError

RESEARCH_MODE = "research"
EXECUTION_PERMISSION = "NOT_ISSUED"
ORDERS_SUBMITTED = 0

# Modules that implement operational controls. Research code must not be
# entered from them, and it must not import them.
EXECUTION_PREFIXES = (
    "sterling.gate2",
    "sterling.placeholders",
    "sterling.risk",
    "sterling.rules",
    "sterling.ledger",
)


def assert_not_on_execution_path() -> None:
    """Raise if a live-control module is on the call stack."""
    for frame in inspect.stack():
        name = frame.frame.f_globals.get("__name__", "")
        if not isinstance(name, str):
            continue
        if name.startswith(EXECUTION_PREFIXES):
            raise ResearchIsolationError(
                f"Research mode cannot run from {name}. No order is submitted and execution permission stays NOT_ISSUED."
            )
