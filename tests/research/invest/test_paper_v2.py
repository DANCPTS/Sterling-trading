"""Paper-v2 allocations, portfolio maths, and the closed v1 lane."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.preregistration import PreRegistration
from sterling.research.grok.providers import MockGrokProvider, ScriptStep
from sterling.research.grok.safety import POLICY_FILE_SHA256, RULES_FILE_SHA256
from sterling.research.invest.benchmarks import benchmark_table
from sterling.research.invest.book import Book, accrue_cash, declare_distribution, pay_distributions, settle_pending
from sterling.research.invest.decide import run_invest_week
from sterling.research.invest.identity import INVEST_V2, performance_document
from sterling.research.invest.registration import PaperV2Registration
from sterling.research.invest.schema import CASH, FUNDS, allocation_json, parse_allocation
from sterling.research.invest.verify import verify_invest, verify_v1_closed

ROOT = Path(__file__).resolve().parents[3]
WHEN = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
PRICES = {name: Decimal("100") for name in FUNDS}


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def test_operational_files_are_unchanged_and_unread() -> None:
    rules = (ROOT / "src/sterling/rules/data/desk-rules-v0.2.json").read_bytes()
    policy = (ROOT / "src/sterling/risk/data/stage2-gate1-policy-v1.json").read_bytes()
    assert hashlib.sha256(rules).hexdigest() == RULES_FILE_SHA256
    assert hashlib.sha256(policy).hexdigest() == POLICY_FILE_SHA256
    for path in (ROOT / "src/sterling/research/invest").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "desk-rules" not in text
        assert "stage2-gate1" not in text
        assert "sterling.rules" not in text
        assert "sterling.risk" not in text
        assert "PER_TRADE_CAP" not in text


def test_v1_registration_digest_is_unchanged() -> None:
    assert len(PreRegistration().digest()) == 64
    assert PaperV2Registration().as_log_fields()["status_of_v1"] == "closed after 1 week"
    assert PaperV2Registration().digest() != PreRegistration().digest()


def test_allocation_shapes(stamped: None) -> None:
    del stamped
    valid = parse_allocation(MockGrokProvider(seed=2, response_format="allocation").complete(_snap(), prompt="x").raw_response)
    assert not hasattr(valid, "reason")
    assert abs(sum(valid.weights.values()) - 1) < Decimal("0.0000001")
    assert parse_allocation(_weights({"VWRP": "1.20", **{name: "0" for name in FUNDS if name != "VWRP"}, CASH: "0"})).reason == (
        "weight_out_of_range"
    )
    assert parse_allocation(_weights({name: "0.20" for name in (*FUNDS, CASH)})).reason == "weight_sum"
    cash = {name: "0" for name in FUNDS}
    cash[CASH] = "1"
    assert parse_allocation(_weights(cash)).weights[CASH] == 1
    single = {name: "0" for name in (*FUNDS, CASH)}
    single["VWRP"] = "1"
    assert parse_allocation(_weights(single)).weights["VWRP"] == 1
    assert parse_allocation("not-json").reason == "malformed_json"


def test_invalid_reply_keeps_cash_and_blocks_a_second_call(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "mock.jsonl"
    book = Book()
    provider = MockGrokProvider(steps=(ScriptStep(raw_response="not-json"),), response_format="allocation")
    first = run_invest_week(
        log_path=log,
        book=book,
        prices=PRICES,
        tradable=set(FUNDS),
        as_of=WHEN,
        provider=provider,
        prompt="paper-v2",
    )
    assert first["provider_calls"] == 1
    assert first["gate"] == "hold"
    assert book.quantity("VWRP") == 0
    text = log.read_text(encoding="utf-8")
    assert '"disposition":"HOLD"' in text
    assert "not-json" in text
    assert '"execution_permission":"NOT_ISSUED"' in text
    second = run_invest_week(
        log_path=log,
        book=book,
        prices=PRICES,
        tradable=set(FUNDS),
        as_of=WHEN,
        provider=MockGrokProvider(response_format="allocation"),
        prompt="paper-v2",
    )
    assert second["provider_calls"] == 0
    verify_invest(ProposalLog(log).records(), lane="mock")
    with pytest.raises(ResearchError, match="another experiment"):
        verify_v1_closed(ProposalLog(log).records())


def test_hundred_percent_and_missing_price_and_two_weeks(tmp_path: Path, stamped: None) -> None:
    del stamped
    log = tmp_path / "weeks.jsonl"
    book = Book()
    single = {name: "0" for name in (*FUNDS, CASH)}
    single["VWRP"] = "1"
    provider = MockGrokProvider(steps=(ScriptStep(raw_response=_weights(single)),), response_format="allocation")
    tradable = set(FUNDS) - {"IGLT"}
    decided = run_invest_week(
        log_path=log,
        book=book,
        prices=PRICES,
        tradable=tradable,
        as_of=WHEN,
        provider=provider,
        prompt="paper-v2",
    )
    assert decided["gate"] == "accepted"
    assert decided["provider_calls"] == 1
    fills = settle_pending(book, PRICES, tradable, WHEN.date())
    assert book.quantity("IGLT") == 0
    assert book.quantity("VWRP") > 0
    assert book.cash >= 0
    assert fills and fills[0]["side"] == "BUY"
    accrue_cash(book, Decimal("0.04"))
    declare_distribution(book, "VWRP", Decimal("1.00"), WHEN.date() + timedelta(days=2))
    assert pay_distributions(book, WHEN.date() + timedelta(days=1)) == 0
    assert pay_distributions(book, WHEN.date() + timedelta(days=2)) == Decimal("1.00")
    later = WHEN + timedelta(days=7)
    again = run_invest_week(
        log_path=log,
        book=book,
        prices=PRICES,
        tradable=set(FUNDS),
        as_of=later,
        provider=MockGrokProvider(steps=(ScriptStep(raw_response=_weights({**{name: "0" for name in FUNDS}, CASH: "1"})),)),
        prompt="paper-v2",
    )
    assert again["provider_calls"] == 1
    sold = settle_pending(book, PRICES, set(FUNDS), later.date())
    assert any(row["side"] == "SELL" for row in sold)
    assert book.cash > 0
    sessions = [WHEN.date() + timedelta(days=offset) for offset in range(10)]
    closes = {name: tuple((day, Decimal("100") + day.day) for day in sessions) for name in FUNDS}
    table = benchmark_table(closes, sessions, sonia=((sessions[0], Decimal("3.50")),))
    assert table["schema"] == "sterling-performance-v1"
    assert table["system"] == "sterling-invest"
    assert {row["name"] for row in table["arms"]} == {
        "buy_and_hold_vwrp",
        "equal_weight",
        "sixty_forty",
        "sterling_cash",
        "etf_trend",
        "etf_dual_momentum",
    }
    assert performance_document(experiment=INVEST_V2, arms=[])["system"] == "sterling-invest"


def test_v1_sample_is_not_rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STERLING_SYSTEM", raising=False)
    monkeypatch.delenv("STERLING_EXPERIMENT", raising=False)
    path = tmp_path / "forward.jsonl"
    log = ProposalLog(path)
    log.create(now=WHEN)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    verify_v1_closed(log.records())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def _weights(weights: dict[str, str]) -> str:
    return allocation_json(weights, model_id="mock-grok")


def _snap():
    from sterling.research.grok.schema import InstrumentQuote, MarketSnapshot

    return MarketSnapshot(
        as_of=WHEN,
        quotes=tuple(InstrumentQuote(name, Decimal("100"), WHEN, "paper-v2") for name in FUNDS),
        positions=(),
        nav_gbp=Decimal("10000"),
        universe=FUNDS,
    )
