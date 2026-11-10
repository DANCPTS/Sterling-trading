"""Weekly paper book: one decision per week, replay, and quote holds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.errors import ResearchError
from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.context import PriceObservation
from sterling.research.grok.preregistration import APPROVED_UNIVERSE
from sterling.research.grok.quotes import CorporateAction, adjust_for_actions, assess_instrument, to_gbp
from sterling.research.grok.schema import InstrumentQuote, MarketSnapshot
from sterling.research.grok.timekeeping import SystemClock, TickingClock
from sterling.research.grok.verify import verify_modes
from sterling.research.grok.weekly import run_week, state_file


def _snapshot(moment: datetime) -> MarketSnapshot:
    quotes = tuple(InstrumentQuote(ticker, Decimal("100"), moment, "fixture") for ticker in APPROVED_UNIVERSE)
    return MarketSnapshot(as_of=moment, quotes=quotes, positions=(), nav_gbp=Decimal("10000"), universe=APPROVED_UNIVERSE)


def _provider(raw: str):
    calls = {"n": 0}

    def provide(snapshot: MarketSnapshot, prompt: str) -> tuple[str, str]:
        del snapshot, prompt
        calls["n"] += 1
        return raw, "mock-grok"

    return provide, calls


def _raw(weight: str = "0.10") -> str:
    import json

    return json.dumps(
        {
            "model_id": "mock-grok",
            "proposals": [
                {
                    "action": "BUY",
                    "confidence": "0.40",
                    "instrument_id": "VWRP",
                    "reasoning": "fixture only",
                    "target_weight": weight,
                }
            ],
        }
    )


def test_second_week_fills_and_a_rerun_does_not_call_again(tmp_path: Path) -> None:
    log = tmp_path / "forward.jsonl"
    first = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
    provide, calls = _provider(_raw())
    opened = run_week(
        log_path=log,
        state_path=state_file(log),
        snapshot=_snapshot(first),
        provider=provide,
        mode="simulated_clock",
        clock=TickingClock(first),
        recorder=SystemClock(),
    )
    assert opened.status == "decided"
    assert calls["n"] == 1
    assert not any(record.get("record_type") == "fill" for record in ProposalLog(log).records())
    second = datetime(2026, 11, 16, 16, 30, tzinfo=UTC)
    again, calls2 = _provider(_raw())
    filled = run_week(
        log_path=log,
        state_path=state_file(log),
        snapshot=_snapshot(second),
        provider=again,
        mode="simulated_clock",
        clock=TickingClock(second),
        recorder=SystemClock(),
        opens={"VWRP": ("101.00", "2026-11-03")},
    )
    assert filled.status == "decided"
    assert calls2["n"] == 1
    kinds = [record.get("record_type") for record in ProposalLog(log).records()]
    assert "fill" in kinds
    noop, calls3 = _provider(_raw())
    state_file(log).unlink()
    skipped = run_week(
        log_path=log,
        state_path=state_file(log),
        snapshot=_snapshot(second),
        provider=noop,
        mode="simulated_clock",
        clock=TickingClock(second + timedelta(hours=1)),
        recorder=SystemClock(),
    )
    assert skipped.status == "noop"
    assert calls3["n"] == 0
    assert state_file(log).is_file()
    verify_modes(ProposalLog(log).records(), now=datetime.now(UTC))


def test_a_reserved_week_is_not_billed_again_after_a_crash(tmp_path: Path) -> None:
    log_path = tmp_path / "forward.jsonl"
    moment = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
    provide, calls = _provider(_raw())
    run_week(
        log_path=log_path,
        state_path=state_file(log_path),
        snapshot=_snapshot(moment),
        provider=provide,
        mode="simulated_clock",
        clock=TickingClock(moment),
        recorder=SystemClock(),
    )
    text = log_path.read_text(encoding="utf-8").splitlines()
    reserve = next(line for line in text if "week_reserve" in line)
    log_path.write_text(text[0] + "\n" + reserve + "\n", encoding="utf-8")
    state_file(log_path).unlink()
    again, calls2 = _provider(_raw())
    result = run_week(
        log_path=log_path,
        state_path=state_file(log_path),
        snapshot=_snapshot(moment),
        provider=again,
        mode="simulated_clock",
        clock=TickingClock(moment + timedelta(minutes=5)),
        recorder=SystemClock(),
    )
    assert result.status == "noop"
    assert calls2["n"] == 0


def test_quotes_hold_on_stale_disagreement_and_pence() -> None:
    assert to_gbp(Decimal("10000"), "GBp", Decimal("100")) == Decimal("100")
    as_of = datetime(2026, 11, 10, 16, 30, tzinfo=UTC)
    sessions = [as_of - timedelta(days=offset) for offset in range(6, 0, -1)]
    fresh = PriceObservation("VWRP", as_of.date(), Decimal("100"), as_of - timedelta(hours=1), "yahoo")
    other = PriceObservation("VWRP", as_of.date(), Decimal("101"), as_of - timedelta(hours=1), "stooq")
    verdict = assess_instrument("VWRP", (fresh,), (other,), as_of=as_of, sessions=sessions, max_age_sessions=5)
    assert verdict.action == "USE"
    disagree = PriceObservation("VWRP", as_of.date(), Decimal("120"), as_of - timedelta(hours=1), "stooq")
    held = assess_instrument("VWRP", (fresh,), (disagree,), as_of=as_of, sessions=sessions, max_age_sessions=5)
    assert held.action == "HOLD"
    assert held.price is None
    stale = PriceObservation("VWRP", as_of.date(), Decimal("100"), as_of - timedelta(days=30), "yahoo")
    old = assess_instrument("VWRP", (stale,), (stale,), as_of=as_of, sessions=sessions, max_age_sessions=5)
    assert old.reason == "stale"
    assert old.price is None
    split = CorporateAction("VWRP", as_of, Decimal("2"), as_of, "split")
    before = PriceObservation("VWRP", as_of.date(), Decimal("200"), as_of - timedelta(days=1), "yahoo")
    adjusted = adjust_for_actions((before,), (split,))
    assert adjusted[0].price == Decimal("100")


def test_forward_mode_rejects_a_future_decision(tmp_path: Path) -> None:
    provide, _calls = _provider(_raw())
    with pytest.raises(ResearchError, match="future"):
        run_week(
            log_path=tmp_path / "forward.jsonl",
            state_path=state_file(tmp_path / "forward.jsonl"),
            snapshot=_snapshot(datetime.now(UTC)),
            provider=provide,
            mode="forward",
            clock=TickingClock(datetime.now(UTC) + timedelta(days=3)),
            recorder=SystemClock(),
        )


def test_mixed_modes_are_rejected(tmp_path: Path) -> None:
    log = tmp_path / "forward.jsonl"
    moment = datetime(2026, 11, 2, 16, 30, tzinfo=UTC)
    provide, _calls = _provider(_raw())
    run_week(
        log_path=log,
        state_path=state_file(log),
        snapshot=_snapshot(moment),
        provider=provide,
        mode="simulated_clock",
        clock=TickingClock(moment),
        recorder=SystemClock(),
    )
    records = list(ProposalLog(log).records())
    records[-1] = {**records[-1], "mode": "forward"}
    with pytest.raises(ResearchError, match="mix"):
        verify_modes(records, now=datetime.now(UTC))
