"""Deterministic fault injection for crash and I/O tests.

Points are named strings. The default is to do nothing. Setting
``STERLING_GATE2_FAULT`` to a point name crashes the process or raises
before a success response. Coverage is flushed before ``os._exit`` so a
killed worker can still record the lines it executed.
"""

from __future__ import annotations

import errno
import os
import sqlite3


def fault_point(name: str) -> None:
    """Stop at ``name`` when the environment selects it."""
    selected = os.environ.get("STERLING_GATE2_FAULT", "")
    if selected == "" or selected != name:
        return
    kind = os.environ.get("STERLING_GATE2_FAULT_KIND", "crash")
    if kind == "io":
        raise OSError(errno.ENOSPC, f"injected I/O failure at {name}")
    if kind == "busy":
        raise sqlite3.OperationalError("database is locked")
    if kind == "fsync":
        raise OSError(errno.EIO, f"injected fsync failure at {name}")
    _flush_coverage()
    os._exit(86)


def _flush_coverage() -> None:
    try:
        import coverage
    except ImportError:
        return
    cov = coverage.Coverage.current()
    if cov is not None:
        cov.save()
