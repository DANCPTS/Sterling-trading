#!/usr/bin/env python3
"""One manual research day. No model, no network, no official evidence write.

The log must be a mock-lane path outside this repository. Opens are not invented.
A missing open does not become a fill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_invest_v2 import _cache_prints, _repository_evidence  # noqa: E402

from sterling.research.calendar import xlon  # noqa: E402
from sterling.research.fx.grok_forward import ProposalLog  # noqa: E402
from sterling.research.invest.benchmarks import benchmark_table  # noqa: E402
from sterling.research.invest.cash_rate import parse_sonia_csv  # noqa: E402
from sterling.research.invest.daily import run_daily  # noqa: E402
from sterling.research.invest.lanes import assert_lane, classify_log  # noqa: E402
from sterling.research.invest.schema import FUNDS  # noqa: E402
from sterling.research.money import quantize_money  # noqa: E402

CONTEXT = {
    "grok_raw_recommendation": "HOLD, CASH 1, ETFs 0, json model_id paper-v2-allocation",
    "hypothetical_replay": "explicit_hold only if model_id were grok-4.3; not paid-call evidence",
    "not_a_live_forward_result": "true",
    "recorded_gate": "model_id_mismatch fail-closed HOLD; not an accepted decision",
}


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if any(item == "--provider" or item.startswith("--provider=") or item == "xai" or "--fetch" in item for item in raw):
        print("manual research day does not call a provider and does not fetch", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Value one recorded research day. No model call.")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(raw)
    lane = os.environ.get("STERLING_EVIDENCE_LANE", "").strip()
    if lane != "mock":
        print("manual research day requires STERLING_EVIDENCE_LANE=mock", file=sys.stderr)
        return 2
    if classify_log(args.log) != "mock" or _repository_evidence(args.log) or "sterling-forward-evidence" in args.log.as_posix():
        print("refusing to write official or forward evidence", file=sys.stderr)
        return 2
    try:
        assert_lane(args.log, "mock")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    session = date.fromisoformat(args.session)
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        print("as-of needs a timezone", file=sys.stderr)
        return 2
    sonia_path = args.cache / "boe_sonia.csv"
    if not sonia_path.is_file():
        print("cache is missing boe_sonia.csv", file=sys.stderr)
        return 2
    sonia = parse_sonia_csv(sonia_path.read_text(encoding="utf-8"))
    prints = _cache_prints(args.cache, as_of)
    history: dict[str, list[tuple[date, Decimal]]] = {name: [] for name in FUNDS}
    observations = []
    for item in prints:
        if item.session > session:
            continue
        history[item.ticker].append((item.session, item.price))
    for name in FUNDS:
        history[name] = sorted(set(history[name]))
        latest = [item for item in prints if item.ticker == name and item.session <= session]
        if not latest:
            continue
        chosen = max(latest, key=lambda item: (item.session, item.available_at))
        identity = "|".join(
            [
                chosen.ticker,
                chosen.session.isoformat(),
                format(chosen.price, "f"),
                chosen.available_at.isoformat(),
                "" if chosen.retrieved_at is None else chosen.retrieved_at.isoformat(),
                chosen.source,
                chosen.availability_basis,
            ]
        )
        observations.append(
            {
                "availability_basis": chosen.availability_basis,
                "available_at": chosen.available_at.isoformat(),
                "exchange_close_at": chosen.exchange_close_at.isoformat(),
                "instrument_id": chosen.ticker,
                "observation_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "price_gbp": format(chosen.price, "f"),
                "provider_published_at": None if chosen.provider_published_at is None else chosen.provider_published_at.isoformat(),
                "retrieved_at": None if chosen.retrieved_at is None else chosen.retrieved_at.isoformat(),
                "session": chosen.session.isoformat(),
                "source": chosen.source,
            }
        )
    log = ProposalLog(args.log)
    if not args.log.exists():
        log.create(now=as_of - timedelta(seconds=2))
    log.append_event(
        timestamp_utc=as_of - timedelta(seconds=1),
        record_type="snapshot",
        fields={
            "as_of": as_of.isoformat(),
            "day_count": "ACT/365",
            "label": "retrospective-demonstration",
            "observations": observations,
            "research_context": CONTEXT,
            "session": session.isoformat(),
            "sonia_rule": "published IUDSOIA only; retail haircut 0.50pp; no look-ahead",
        },
    )
    closes = {name: tuple(points) for name, points in history.items()}
    result = run_daily(
        log_path=args.log,
        session=session,
        opens={},
        close_history=closes,
        sonia=sonia,
        recorded_at=as_of,
        open_source="recorded-cache",
        data_label="retrospective-demonstration",
    )
    known = sorted({day for points in closes.values() for day, _price in points if day <= session})
    # Month-ends plus the valuation date. A full daily replay of the frozen rules is not this demo.
    sample = sorted(set(xlon().month_ends(known[0], session)) | {known[0], session}) if known else [session]
    retrospective = benchmark_table(closes, sample, sonia=sonia, as_of=as_of)
    wanted = {"sterling_cash", "equal_weight", "sixty_forty"}
    arms = [arm for arm in retrospective["arms"] if isinstance(arm, dict) and arm.get("name") in wanted]
    report = {
        "arms": arms,
        "daily": result,
        "execution_equivalence": retrospective.get("execution_equivalence"),
        "execution_note": retrospective.get("execution_note"),
        "benchmark_sessions": "month-ends plus the valuation date, not every daily close",
        "label": "retrospective-demonstration-not-forward-evidence",
        "observations": observations,
        "research_context": CONTEXT,
    }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(
        f"manual status={result['status']} session={result['session']} "
        f"nav_gbp={result['nav_gbp']} provider_calls={result['provider_calls']} label=retrospective-demonstration"
    )
    interest = quantize_money(Decimal(str(result["nav_gbp"])) - Decimal("10000.00"))
    print(f"cash_accrual_vs_start_gbp={format(interest, 'f')} day_count=ACT/365")
    for arm in arms:
        print(
            f"benchmark {arm['name']} end_nav_gbp={arm['end_nav_gbp']} "
            f"cumulative_return={arm['cumulative_return']} costs_gbp={arm['costs_gbp']} trades={arm['trades']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
