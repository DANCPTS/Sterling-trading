"""Draft universe, proposed limits, and the unchanged operational controls."""

from __future__ import annotations

import ast
import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.placeholders.broker import BrokerAdapter
from sterling.research.errors import ResearchError, ResearchIsolationError
from sterling.research.instruments import load_candidate_universe
from sterling.research.proposal import GrokProposalStub, Proposal
from sterling.research.risk_config import load_research_limits, parse_research_limits
from sterling.research.strategy import MarketView
from sterling.rules.loader import load_default_rules

ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = ROOT / "src/sterling/rules/data/desk-rules-v0.2.json"
POLICY_PATH = ROOT / "src/sterling/risk/data/stage2-gate1-policy-v1.json"
RULES_SHA256 = "ba938baa22dd74944c9fe2cc7e68182c06498b3bc5fbff388df84818f6ae24ba"
POLICY_SHA256 = "5b5c315e57ebf65c3cd7291af11fd7cbe0bc897b2ee7d57f7a8051030246a363"
RESEARCH_ROOT = ROOT / "src/sterling/research"
LIVE_ROOT = ROOT / "src/sterling"
FORBIDDEN_IMPORTS = (
    "sterling.gate2",
    "sterling.placeholders",
    "sterling.risk",
    "sterling.rules",
    "sterling.ledger",
    "urllib",
    "socket",
    "requests",
    "httpx",
    "http.client",
)


def test_operational_config_bytes_and_values_are_unchanged() -> None:
    rules_bytes = RULES_PATH.read_bytes()
    policy_bytes = POLICY_PATH.read_bytes()
    assert hashlib.sha256(rules_bytes).hexdigest() == RULES_SHA256
    assert hashlib.sha256(policy_bytes).hexdigest() == POLICY_SHA256
    rules_text = rules_bytes.decode("utf-8")
    policy_text = policy_bytes.decode("utf-8")
    assert '"absolute_ceiling": "2.50"' in rules_text
    assert '"absolute_ceiling": "5.00"' in rules_text
    assert '"instrument_allowlist": []' in rules_text
    assert '"min": 2' in rules_text
    assert '"max": 10' in rules_text
    assert '"provisional": true' in rules_text
    assert '"live_trading": false' in rules_text
    assert '"per_trade_absolute_ceiling_gbp": "2.50"' in policy_text
    assert '"daily_loss_absolute_ceiling_gbp": "5.00"' in policy_text
    assert '"instrument_allowlist": []' in policy_text
    assert '"default_decision": "DENY"' in policy_text
    assert '"execution_permission": "NOT_ISSUED"' in policy_text
    rules = load_default_rules()
    assert rules.per_trade_absolute_ceiling == Decimal("2.50")
    assert rules.daily_loss_absolute_ceiling == Decimal("5.00")
    assert rules.instrument_allowlist == ()
    assert rules.holding_period_provisional is True
    assert rules.live_trading is False


def test_research_package_cannot_reach_execution_modules() -> None:
    offenders: list[str] = []
    for path in RESEARCH_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.startswith(FORBIDDEN_IMPORTS) or name in FORBIDDEN_IMPORTS:
                    offenders.append(f"{path.name} imports {name}")
        text = path.read_text(encoding="utf-8")
        for token in ("urllib.request", "import socket", "import requests", "place_order", "yf.download"):
            if token in text:
                offenders.append(f"{path.name} contains {token}")
    assert offenders == []
    for path in LIVE_ROOT.rglob("*.py"):
        if RESEARCH_ROOT in path.parents or path.parent == RESEARCH_ROOT:
            continue
        text = path.read_text(encoding="utf-8")
        assert "sterling.research" not in text


def test_running_research_does_not_call_the_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(NotImplementedError):
        BrokerAdapter().place_order()
    calls: list[str] = []

    def _boom(self: object) -> None:
        del self
        calls.append("place_order")

    monkeypatch.setattr(BrokerAdapter, "place_order", _boom)
    from sterling.research.synthetic import run_illustration

    before_rules = RULES_PATH.read_bytes()
    before_policy = POLICY_PATH.read_bytes()
    result = run_illustration()
    assert calls == []
    assert result.execution_permission == "NOT_ISSUED"
    assert result.orders_submitted == 0
    assert result.mode == "research"
    assert RULES_PATH.read_bytes() == before_rules
    assert POLICY_PATH.read_bytes() == before_policy


def test_research_limits_stay_proposed_and_are_not_the_operational_file() -> None:
    limits = load_research_limits()
    assert limits.status == "APPROVED FOR RESEARCH SIMULATION ONLY by owner 2026-11-09; not operational"
    assert limits.owner_approved is False
    assert limits.max_gross == Decimal("1")
    assert limits.max_single_instrument_weight == Decimal("0.50")
    assert limits.min_trade_notional_gbp == Decimal("25.00")
    assert limits.max_one_way_turnover == Decimal("0.50")
    assert dict(limits.max_asset_class_weight)["commodity"] == Decimal("0.20")
    assert limits.starting_nav_gbp == Decimal("10000.00")
    with pytest.raises(ResearchError, match="operational"):
        parse_research_limits({"version_id": "desk-rules-v0.2", "status": limits.status, "owner_approved": False})
    with pytest.raises(ResearchError, match="owner-approved"):
        parse_research_limits(
            {
                "status": "APPROVED FOR RESEARCH SIMULATION ONLY by owner 2026-11-09; not operational",
                "owner_approved": True,
                "constraints": {},
                "account": {},
            }
        )


def test_grok_stub_does_not_call_a_model() -> None:
    view = MarketView(as_of=date(2024, 1, 31), closes={})
    with pytest.raises(ResearchIsolationError, match="No model"):
        GrokProposalStub().propose(
            view,
            (),
            decision_timestamp=datetime(2024, 1, 31, 16, 30, tzinfo=datetime.now().astimezone().tzinfo),
        )


def test_proposal_rejects_a_cutoff_after_the_signal() -> None:
    with pytest.raises(ResearchError, match="cutoff"):
        Proposal(
            proposal_id="x",
            decision_timestamp=datetime(2024, 1, 31, 16, 30, tzinfo=datetime.now().astimezone().tzinfo),
            signal_date=date(2024, 1, 31),
            data_cutoff=date(2024, 2, 1),
            source_type="rules",
            strategy_id="trend_filter",
            model_version=None,
            prompt_version=None,
            input_digest="abc",
            weights=(),
            raw_output="{}",
        )


def test_draft_universe_keeps_roles_structures_and_unverified_fields() -> None:
    universe = load_candidate_universe()
    assert universe.status == "DRAFT"
    assert universe.frozen is False
    assert universe.provisional_starting_set == ("VWRP", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR")
    assert universe.optional_comparison_only == ("CSP1", "ERNS")
    text = universe.fscs_general_information
    assert "FSCS does not protect against investment or market losses" in text
    assert "generally outside FSCS for fund or issuer failure" in text
    assert "FSCS may cover failure of a UK-authorised platform or broker" in text
    assert "£85,000 per person per firm" in text
    assert "Eligibility depends on the firm and the claim" in text
    assert "https://www.fscs.org.uk/" in text
    assert "Verify before reliance" in text
    assert "not covered by FSCS" not in text
    by_ticker = {item.ticker: item for item in universe.instruments}
    assert by_ticker["VWRP"].product_class == "ucits_etf_physical"
    assert by_ticker["VWRP"].ucits_fund is True
    assert by_ticker["SGLN"].product_class == "etc_physical_gold"
    assert by_ticker["SGLN"].ucits_fund is False
    assert by_ticker["SGLN"].instrument_type == "ETC"
    assert by_ticker["XSTR"].product_class == "ucits_etf_synthetic_swap"
    assert "swap" in by_ticker["XSTR"].product_class
    assert by_ticker["CSP1"].draft_role == "optional_comparison_only"
    assert by_ticker["ERNS"].draft_role == "optional_comparison_only"
    assert by_ticker["XSTR"].lse_listing_date.startswith("UNVERIFIED")
    for item in universe.instruments:
        assert item.stooq_history_start == "UNVERIFIED"
        assert item.isin
    assert by_ticker["VWRP"].isin == "IE00BK5BQT80"
    assert by_ticker["SGLN"].isin == "IE00B4ND3602"
    assert by_ticker["XSTR"].isin == "LU0321464652"
    assert by_ticker["CUKX"].yahoo_quote_currency == "GBp"
    assert by_ticker["XSTR"].yahoo_quote_currency == "GBp"
    assert by_ticker["VWRP"].price_divisor_to_gbp == Decimal("100") or by_ticker["VWRP"].yahoo_quote_currency == "GBP"
    vwrp = by_ticker["VWRP"]
    assert vwrp.fee_on(date(2025, 10, 6)).annual_rate == Decimal("0.0022")
    assert vwrp.fee_on(date(2025, 10, 7)).annual_rate == Decimal("0.0019")
    assert vwrp.fee_on(date(2026, 7, 27)).annual_rate == Decimal("0.0019")
    assert vwrp.fee_on(date(2026, 7, 28)).annual_rate == Decimal("0.0014")
    assert vwrp.fee_on(date(2024, 1, 2)).safe_to_apply_historically is False
    erns = by_ticker["ERNS"]
    assert erns.fee_on(date(2015, 3, 4)).annual_rate == Decimal("0.0020")
    assert erns.fee_on(date(2015, 3, 5)).annual_rate == Decimal("0.0009")
    assert by_ticker["CUKX"].fee_on(date(2020, 1, 2)).safe_to_apply_historically is False
    raw = (ROOT / "src/sterling/research/data/candidate-universe-draft.json").read_text(encoding="utf-8")
    assert "not covered by FSCS" not in raw
    assert "FSCS does not cover" not in raw
