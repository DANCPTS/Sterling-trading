"""Research-only unleveraged FX deposit book.

This package does not submit orders, connect to a broker, or call a model.
``execution_permission`` stays ``NOT_ISSUED``. It is not an operational allowlist
and it does not read the GBP 2.50 / GBP 5.00 risk caps as position-size limits.
"""

from sterling.research.fx.config import load_fx_config
from sterling.research.isolation import EXECUTION_PERMISSION, ORDERS_SUBMITTED, RESEARCH_MODE

__all__ = [
    "EXECUTION_PERMISSION",
    "ORDERS_SUBMITTED",
    "RESEARCH_MODE",
    "load_fx_config",
]
