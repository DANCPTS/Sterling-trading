"""Synthetic test fixtures. See tests/fixtures/README.md."""

from __future__ import annotations

import pytest

from sterling.rules.loader import load_default_rules
from sterling.rules.model import DeskRules


@pytest.fixture(scope="session")
def rules() -> DeskRules:
    return load_default_rules()
