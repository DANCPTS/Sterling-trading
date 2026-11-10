"""Pre-registered rules, publication lags, and no look-ahead in the deposit book."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.errors import ResearchError, ResearchIsolationError
from sterling.research.fx.config import CostScenario, DepositScenario, FxResearchConfig, load_fx_config, parse_fx_config
from sterling.research.fx.series import (
    CurrencySpec,
    Market,
    StepSeries,
    build_step_series,
    load_market,
    monthly_available_from,
)
from sterling.research.fx.simulate import FxResult, decision_schedule, make_view, run_path
from sterling.research.fx.stats import advantage_failures, bootstrap_mean_excess, oos_start
from sterling.research.fx.strategies import Carry, LegScore, Trend, combined_blockers
from sterling.research.isolation import EXECUTION_PERMISSION

ROOT = Path(__file__).resolve().parents[3]


def _config() -> FxResearchConfig:
    base = load_fx_config()
    zeros = tuple((code, Decimal(0)) for code, _spread in base.retail_half_spread)
    return replace(base, retail_half_spread=zeros, institutional_half_spread=zeros)


def _deposit(rate_floor: bool = False) -> DepositScenario:
    return DepositScenario("flat", "sonia", Decimal(0), Decimal(0), rate_floor, None, "test")


def _zero_cost() -> CostScenario:
    return CostScenario("zero", Decimal(0), "retail")


def _market(panel: list[date], spots: list[Decimal], sonia: StepSeries, policy: StepSeries) -> Market:
    spec = CurrencySpec("USD", "US dollar", "XUDLUSS", "US")
    return Market(
        codes=("USD",),
        spots=(("USD", tuple((day, spot) for day, spot in zip(panel, spots, strict=True))),),
        panel=tuple(panel),
        sonia=sonia,
        bank_rate=sonia,
        policy=(("USD", policy),),
        currencies=(spec,),
    )


def _zero_rate(available: date = date(2020, 1, 4)) -> StepSeries:
    return build_step_series([(available, date(2020, 1, 1), Decimal(0))], stale_days=10000)


class _Spy:
    def __init__(self, inner: Trend | Carry) -> None:
        self.inner = inner
        self.latest: date | None = None
        self.decision: date | None = None

    def target(self, view: object) -> dict[str, Decimal]:
        from sterling.research.fx.strategies import FxView

        if not isinstance(view, FxView):
            raise AssertionError("view type")
        self.decision = view.decision
        dates = [day for _code, levels in view.gbp_per_unit for day, _level in levels]
        self.latest = max(dates)
        return self.inner.target(view)


def test_primary_rules_are_the_preregistered_ones() -> None:
    config = load_fx_config()
    assert config.execution_permission == EXECUTION_PERMISSION
    assert config.orders_submitted == 0
    assert config.owner_approved is False
    assert config.leverage == "none"
    assert config.short_selling == "prohibited"
    assert config.gross_exposure_max == Decimal(1)
    assert config.trend_months == 10
    assert config.carry_k == 3
    assert config.carry_threshold == Decimal("0.005")
    assert config.min_month_ends == 13
    assert config.oos_fraction == Decimal("0.40")
    assert config.bootstrap_seed == 20261109
    assert config.bootstrap_block_months == 6
    assert config.bootstrap_replications == 2000
    assert config.cost("retail_primary").platform_fee_round_trip == Decimal("0.0050")
    assert config.cost("retail_primary").fee_one_way == Decimal("0.0025")
    headline = config.deposit("retail_headline")
    assert headline.gbp_reference == "sonia"
    assert headline.gbp_haircut == Decimal("0.01")
    assert headline.foreign_haircut == Decimal("0.015")
    assert headline.floor_at_zero is True
    assert config.deposit("foreign_zero").foreign_forced_rate == Decimal(0)
    assert [item.code for item in config.currencies] == ["USD", "EUR", "JPY", "CHF", "AUD", "CAD", "NZD", "SEK", "NOK"]
    assert config.half_spread("USD", date(2010, 6, 1), config.cost("retail_primary")) == Decimal("0.0004")
    assert config.half_spread("USD", date(2001, 6, 1), config.cost("retail_primary")) == Decimal("0.0008")
    assert config.half_spread("USD", date(2008, 10, 1), config.cost("retail_primary")) == Decimal("0.0012")


def test_config_refuses_operational_and_leveraged_settings() -> None:
    raw = json.loads((ROOT / "src/sterling/research/fx/data/fx-research-v1.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    with pytest.raises(ResearchError, match="owner-approved"):
        parse_fx_config({**raw, "owner_approved": True})
    with pytest.raises(ResearchError, match="live"):
        parse_fx_config({**raw, "live_trading": True})
    with pytest.raises(ResearchError, match="leverage"):
        parse_fx_config({**raw, "leverage": "2"})
    with pytest.raises(ResearchError, match="short"):
        parse_fx_config({**raw, "short_selling": "allowed"})
    with pytest.raises(ResearchError, match="operational"):
        parse_fx_config({**raw, "version_id": "desk-rules-v0.2"})
    with pytest.raises(ResearchError, match="order"):
        parse_fx_config({**raw, "orders_submitted": 1})


def test_monthly_and_daily_rates_are_lagged() -> None:
    assert monthly_available_from(date(1999, 1, 31)) == date(1999, 3, 15)
    series = build_step_series(
        [
            (date(1999, 3, 15), date(1999, 1, 31), Decimal("0.01")),
            (date(1999, 4, 15), date(1999, 2, 28), Decimal("0.50")),
        ],
        stale_days=100,
    )
    assert series.as_of(date(1999, 3, 14)) is None
    assert series.as_of(date(1999, 3, 15)) == (date(1999, 1, 31), Decimal("0.01"))
    assert series.as_of(date(1999, 4, 14)) == (date(1999, 1, 31), Decimal("0.01"))
    assert series.as_of(date(1999, 8, 1)) is None


def test_signal_ignores_the_execution_fix_and_the_fill_uses_it() -> None:
    history = [date(2023, month, 15) for month in range(1, 13)] + [date(2024, 1, 15)]
    panel = history + [date(2024, 2, 1), date(2024, 2, 2)]
    spots = [Decimal(2) - Decimal(index) / Decimal(100) for index in range(13)]
    spots.extend([Decimal(9), Decimal("4.5")])
    market = _market(panel, spots, _zero_rate(), _zero_rate())
    schedule = decision_schedule(panel, 13)
    assert schedule == ((date(2024, 1, 15), date(2024, 2, 1)),)
    spy = _Spy(Trend(months=10))
    result = run_path(
        market=market,
        schedule=schedule,
        strategy=spy,
        deposit=_deposit(),
        cost=_zero_cost(),
        config=_config(),
        mode="signal",
        name="trend",
        group="headline",
    )
    assert spy.decision == date(2024, 2, 1)
    assert spy.latest == date(2024, 1, 15)
    assert result.rows[-1].session == date(2024, 2, 2)
    assert result.rows[-1].nav_gbp == Decimal(20000)
    assert result.rows[-1].gross_exposure == Decimal(1)
    assert result.rows[-1].cash_gbp == Decimal(0)
    assert result.execution_permission == "NOT_ISSUED"
    assert result.orders_submitted == 0
    assert result.mode == "research"
    assert result.trades == 1


def test_carry_does_not_see_a_rate_published_later() -> None:
    panel = [date(2023, month, 15) for month in range(1, 13)] + [date(2024, 1, 15), date(2024, 2, 3)]
    spots = [Decimal(2)] * len(panel)
    early = (date(2023, 1, 2), date(2022, 11, 30), Decimal("0.02"))
    future = (date(2024, 3, 1), date(2024, 2, 3), Decimal("0.50"))
    market = _market(panel, spots, _zero_rate(), build_step_series([early, future], stale_days=10000))
    without = _market(panel, spots, _zero_rate(), build_step_series([early], stale_days=10000))
    schedule = decision_schedule(panel, 13)
    from sterling.research.fx.series import month_end_dates

    ends = month_end_dates(panel)
    left = make_view(market, month_end=schedule[0][0], decision=schedule[0][1], month_ends=ends, deposit=_deposit())
    right = make_view(without, month_end=schedule[0][0], decision=schedule[0][1], month_ends=ends, deposit=_deposit())
    assert left.decision == date(2024, 2, 3)
    assert left.rate("USD") == Decimal("0.02")
    assert left.rate("USD") == right.rate("USD")
    assert max(day for _code, levels in left.gbp_per_unit for day, _level in levels) < left.decision
    assert Carry(k=1, threshold=Decimal("0.005")).target(left) == {"USD": Decimal(1)}


def test_interest_uses_the_rate_known_at_the_start_of_the_interval() -> None:
    panel = [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11)]
    sonia = build_step_series(
        [
            (date(2024, 1, 8), date(2024, 1, 5), Decimal("0.01")),
            (date(2024, 1, 9), date(2024, 1, 8), Decimal("0.01")),
            (date(2024, 1, 10), date(2024, 1, 9), Decimal(1)),
            (date(2024, 1, 11), date(2024, 1, 10), Decimal("0.01")),
        ],
        stale_days=100,
    )
    market = _market(panel, [Decimal(2)] * 4, sonia, _zero_rate())
    result = run_path(
        market=market,
        schedule=((date(2024, 1, 8), date(2024, 1, 9)),),
        strategy=None,
        deposit=_deposit(),
        cost=_zero_cost(),
        config=_config(),
        mode="cash",
        name="gbp_cash",
        group="benchmark",
    )
    nav = Decimal(10000)
    for rate in (Decimal("0.01"), Decimal("0.01"), Decimal(1)):
        nav *= Decimal(1) + rate / Decimal(365)
    assert result.rows[-1].nav_gbp == nav
    assert result.trades == 0


def test_costs_are_paid_from_cash_and_cannot_lever_the_book() -> None:
    history = [date(2023, month, 15) for month in range(1, 13)] + [date(2024, 1, 15), date(2024, 2, 1)]
    spots = [Decimal(2) - Decimal(index) / Decimal(100) for index in range(13)] + [Decimal(2)]
    market = _market(history, spots, _zero_rate(), _zero_rate())
    schedule = decision_schedule(history, 13)
    result = run_path(
        market=market,
        schedule=schedule,
        strategy=Trend(months=10),
        deposit=_deposit(),
        cost=CostScenario("fee", Decimal("0.50"), "retail"),
        config=_config(),
        mode="signal",
        name="trend",
        group="sensitivity",
    )
    assert result.total_cost_gbp == Decimal(2000)
    assert result.rows[-1].nav_gbp == Decimal(8000)
    assert result.rows[-1].cash_gbp == Decimal(0)
    assert result.rows[-1].gross_exposure <= Decimal(1)
    assert all(weight >= 0 for _code, weight in result.rows[-1].holdings)


def test_short_and_gross_above_one_are_rejected() -> None:
    panel = [date(2024, 1, 8), date(2024, 1, 9)]
    market = _market(panel, [Decimal(2), Decimal(2)], _zero_rate(), _zero_rate())

    class Short:
        def target(self, view: object) -> dict[str, Decimal]:
            del view
            return {"USD": Decimal("-1")}

    class Levered:
        def target(self, view: object) -> dict[str, Decimal]:
            del view
            return {"USD": Decimal("1.1")}

    with pytest.raises(ResearchError, match="short"):
        run_path(
            market=market,
            schedule=((panel[0], panel[1]),),
            strategy=Short(),
            deposit=_deposit(),
            cost=_zero_cost(),
            config=_config(),
            mode="signal",
            name="bad",
            group="sensitivity",
        )
    with pytest.raises(ResearchError, match="gross"):
        run_path(
            market=market,
            schedule=((panel[0], panel[1]),),
            strategy=Levered(),
            deposit=_deposit(),
            cost=_zero_cost(),
            config=_config(),
            mode="signal",
            name="bad",
            group="sensitivity",
        )


def test_result_cannot_claim_an_order(monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    with pytest.raises(ResearchIsolationError):
        FxResult(
            name="x",
            group="headline",
            rows=(),
            performance=None,  # type: ignore[arg-type]
            trades=0,
            one_way_turnover=Decimal(0),
            total_cost_gbp=Decimal(0),
            stale_foreign_days=(),
            orders_submitted=1,
        )


def test_split_bootstrap_and_combination_gate() -> None:
    decisions = [date(2020, 1, index) for index in range(1, 11)]
    assert oos_start(decisions, Decimal("0.40")) == date(2020, 1, 7)
    flat = tuple(Decimal("0.01") for _ in range(24))
    cash = tuple(Decimal(0) for _ in range(24))
    first = bootstrap_mean_excess(flat, cash, block=6, replications=40, seed=7, ci=Decimal("0.90"))
    second = bootstrap_mean_excess(flat, cash, block=6, replications=40, seed=7, ci=Decimal("0.90"))
    assert first == second
    assert first.low == first.point == first.high == Decimal("0.12")
    blocked = combined_blockers(
        (LegScore("trend", Decimal("-0.01"), Decimal("0.2")), LegScore("carry", Decimal("0.01"), Decimal("0.2"))),
        (LegScore("trend", Decimal("0.01"), Decimal("0.2")), LegScore("carry", Decimal("0.01"), Decimal("0.2"))),
    )
    assert blocked
    assert (
        combined_blockers(
            (LegScore("trend", Decimal("0.01"), Decimal("0.2")), LegScore("carry", Decimal("0.01"), Decimal("0.2"))),
            (LegScore("trend", Decimal("0.01"), Decimal("0.2")), LegScore("carry", Decimal("0.01"), Decimal("0.2"))),
        )
        == ()
    )
    assert advantage_failures(
        cagr=Decimal("0.01"),
        excess=Decimal("-0.01"),
        sharpe_vs_cash=Decimal("0.2"),
        oos_excess=Decimal("0.01"),
        excess_ci_low=Decimal("0.01"),
    )


def test_loader_does_not_download_and_the_runner_does_not_backfill(tmp_path: Path) -> None:
    with pytest.raises(ResearchError, match="does not download"):
        load_market(tmp_path, load_fx_config())
    source = (ROOT / "src/sterling/research/fx/run.py").read_text(encoding="utf-8")
    assert "append_proposal" not in source
    assert "OfflineStubSource" not in source
    assert "GrokApiHook" not in source
