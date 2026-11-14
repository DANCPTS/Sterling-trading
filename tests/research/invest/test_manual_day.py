"""Manual research day: recorded inputs, mock lane, no invented opens."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from scripts.run_manual_research_day import main

from sterling.research.invest.cash_rate import retail_annual_decimal
from sterling.research.money import quantize_money

AS_OF = "2026-11-12T16:30:00+00:00"
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def _cache(tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    for name in ("VWRP", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR"):
        (cache / f"{name}.csv").write_text(
            "date,close,published_at\n2026-11-11,100,2026-11-11T16:30:00+00:00\n2026-11-12,101,2026-11-12T16:30:00+00:00\n",
            encoding="utf-8",
        )
    (cache / "boe_sonia.csv").write_text(
        "DATE,IUDSOIA\n11 Nov 2026,3.9845\n12 Nov 2026,9.0000\n",
        encoding="utf-8",
    )
    return cache


def _log(tmp_path: Path) -> Path:
    return tmp_path / "evidence" / "invest-v2" / "mock" / "forward.jsonl"


def test_manual_day_accrues_published_sonia_and_does_not_fill(stamped: None, tmp_path: Path) -> None:
    del stamped
    log = _log(tmp_path)
    report = tmp_path / "retrospective.json"
    code = main(
        [
            "--log",
            str(log),
            "--cache",
            str(_cache(tmp_path)),
            "--session",
            "2026-11-12",
            "--as-of",
            AS_OF,
            "--report",
            str(report),
        ]
    )
    assert code == 0
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    kinds = [row["record_type"] for row in rows]
    assert "fill" not in kinds
    assert "decision" not in kinds
    snapshot = next(row for row in rows if row["record_type"] == "snapshot")
    context = snapshot["research_context"]
    assert "paper-v2-allocation" in context["grok_raw_recommendation"]
    assert "model_id_mismatch" in context["recorded_gate"]
    assert "explicit_hold" in context["hypothetical_replay"]
    valuation = next(row for row in rows if row["record_type"] == "valuation")
    rate = retail_annual_decimal(Decimal("3.9845"))
    expected = quantize_money(Decimal("10000.00") * rate / Decimal(365))
    assert valuation["cash_rate"]["published_on"] == "2026-11-11"
    assert valuation["cash_rate"]["sonia_percent"] == "3.9845"
    assert Decimal(valuation["nav_gbp"]) == Decimal("10000.00") + expected
    interest = next(row for row in rows if row["record_type"] == "cash_interest")
    assert interest["amount_gbp"] == format(expected, "f")
    assert interest["session"] == "2026-11-12"
    assert "9.0000" not in interest["sonia_percent"]
    body = json.loads(report.read_text(encoding="utf-8"))
    names = {arm["name"] for arm in body["arms"]}
    assert names == {"sterling_cash", "equal_weight", "sixty_forty"}
    assert body["label"] == "retrospective-demonstration-not-forward-evidence"
    assert body["execution_equivalence"] == "not_claimed"


def test_manual_day_refuses_a_provider_and_the_repository_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")
    assert (
        main(
            [
                "--provider",
                "xai",
                "--log",
                str(_log(tmp_path)),
                "--cache",
                str(tmp_path),
                "--session",
                "2026-11-12",
                "--as-of",
                AS_OF,
            ]
        )
        == 2
    )
    banned = ROOT / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    assert main(["--log", str(banned), "--cache", str(tmp_path), "--session", "2026-11-12", "--as-of", AS_OF]) == 2
    assert not banned.exists()
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "forward")
    assert main(["--log", str(_log(tmp_path)), "--cache", str(tmp_path), "--session", "2026-11-12", "--as-of", AS_OF]) == 2


def test_interest_day_is_act_365() -> None:
    amount = quantize_money(Decimal("10000.00") * retail_annual_decimal(Decimal("3.9845")) / Decimal(365))
    assert amount == Decimal("0.95")
    assert date(2026, 11, 12).weekday() == 3
