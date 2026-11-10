"""Errors for the research-only simulator.

These are not operational risk exceptions and they do not authorise a trade.
"""

from __future__ import annotations


class ResearchError(Exception):
    """A research simulator input or result cannot be used."""


class DataValidationError(ResearchError):
    """A price, FX, or calendar series failed validation."""


class ResearchIsolationError(ResearchError):
    """Research mode was asked to reach execution, the network, or a model."""


class NetworkDisabled(ResearchIsolationError):
    """A loader was asked to fetch a URL. Research data stays on local disk."""


class UnverifiedInput(ResearchError):
    """A required field is still marked unverified, so the loader will not guess."""
