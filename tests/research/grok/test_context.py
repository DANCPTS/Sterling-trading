"""Point-in-time features ignore anything published after the decision."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from sterling.research.errors import ResearchError
from sterling.research.grok.context import (
    DistributionObservation,
    PriceObservation,
    SoniaObservation,
    build_market_context,
)
from sterling.research.grok.preregistration import APPROVED_UNIVERSE
from sterling.research.grok.schema import InstrumentQuote, MarketFeature, MarketSnapshot, PaperPosition, estimate_tokens, prompt_for

AS_OF = datetime(2024, 6, 7, 16, 30, tzinfo=UTC)


def _stamp(day: date, hour: int = 16, minute: int = 30) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _path(
    ticker: str, start: date, count: int, *, price: Decimal = Decimal("100"), step: Decimal = Decimal("0.10")
) -> list[PriceObservation]:
    rows: list[PriceObservation] = []
    level = price
    day = start
    for _ in range(count):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        rows.append(PriceObservation(ticker, day, level, _stamp(day)))
        level += step
        day += timedelta(days=1)
    return rows


def _book(as_of: datetime, observations: list[PriceObservation]) -> MarketSnapshot:
    context = build_market_context(as_of=as_of, observations=observations)
    closes = {}
    for item in observations:
        if item.published_at <= as_of:
            closes[item.instrument_id] = item.price
    quotes = tuple(InstrumentQuote(ticker, closes.get(ticker, Decimal("100")), as_of, "close") for ticker in APPROVED_UNIVERSE)
    return MarketSnapshot(
        as_of=as_of,
        quotes=quotes,
        positions=(),
        nav_gbp=Decimal("10000"),
        universe=APPROVED_UNIVERSE,
        features=context.features,
        distributions=context.distributions,
        cash_rate=context.cash_rate,
    )


def test_future_prices_and_shuffles_do_not_change_the_snapshot() -> None:
    start = date(2023, 1, 2)
    history = [row for ticker in APPROVED_UNIVERSE for row in _path(ticker, start, 320, step=Decimal("0.05"))]
    original = build_market_context(as_of=AS_OF, observations=history)
    future_day = date(2024, 6, 10)
    perturbed = list(history)
    perturbed.append(PriceObservation("VWRP", future_day, Decimal("1000"), _stamp(future_day)))
    perturbed.append(PriceObservation("CUKX", future_day, Decimal("1"), _stamp(future_day, hour=18)))
    again = build_market_context(as_of=AS_OF, observations=perturbed)
    assert again.features == original.features
    unpublished = list(history)
    unpublished.append(PriceObservation("VWRP", date(2024, 6, 7), Decimal("999"), _stamp(future_day)))
    held = build_market_context(as_of=AS_OF, observations=unpublished)
    assert held.features == original.features
    left = _book(AS_OF, history)
    right = _book(AS_OF, perturbed)
    assert prompt_for(left) == prompt_for(right)
    estimate = estimate_tokens(prompt_for(left))
    assert estimate == estimate_tokens(prompt_for(right))
    assert 500 < estimate < 8000
    named = {feature.name: feature for feature in original.features}
    assert named["VWRP.return_1w"].value is not None
    assert named["VWRP.return_12m"].value is not None
    assert named["VWRP.trend_vs_10m"].value is not None
    assert named["VWRP.realised_vol_63d"].value is not None
    assert named["VWRP.drawdown"].value is not None
    assert named["bench.equal_weight.return_12m"].value is not None
    assert named["bench.sixty_forty.return_1m"].value is not None
    for feature in original.features:
        assert feature.as_of == AS_OF
        assert all(source <= AS_OF for source in feature.sources)


def test_sonia_uses_the_publication_lag_and_distributions_are_filtered() -> None:
    thursday = SoniaObservation(date(2024, 6, 6), Decimal("0.05"), datetime(2024, 6, 7, 8, 0, tzinfo=UTC))
    friday = SoniaObservation(date(2024, 6, 7), Decimal("0.09"), datetime(2024, 6, 10, 8, 0, tzinfo=UTC))
    early = build_market_context(as_of=AS_OF, observations=(), sonia=(thursday, friday))
    assert early.cash_rate is not None
    assert early.cash_rate.annual_rate == Decimal("0.05")
    assert early.cash_rate.observed_on == date(2024, 6, 6)
    assert early.cash_rate.published_at <= AS_OF
    later = build_market_context(
        as_of=datetime(2024, 6, 10, 9, 0, tzinfo=UTC),
        observations=(),
        sonia=(thursday, friday),
    )
    assert later.cash_rate is not None
    assert later.cash_rate.annual_rate == Decimal("0.09")
    published = DistributionObservation(
        "XSTR", date(2024, 3, 1), date(2024, 3, 20), Decimal("0.12"), datetime(2024, 3, 1, 16, 30, tzinfo=UTC)
    )
    secret = DistributionObservation(
        "XSTR", date(2024, 6, 7), date(2024, 6, 28), Decimal("9"), datetime(2024, 6, 8, 9, 0, tzinfo=UTC)
    )
    context = build_market_context(as_of=AS_OF, observations=(), distributions=(published, secret))
    assert [note.amount for note in context.distributions] == [Decimal("0.12")]
    assert context.distributions[0].pay_date == date(2024, 3, 20)


def test_context_rejects_names_outside_the_six_and_future_sources() -> None:
    with pytest.raises(ResearchError, match="six-ETF"):
        build_market_context(
            as_of=AS_OF,
            observations=(PriceObservation("TSLA", date(2024, 6, 7), Decimal("1"), AS_OF),),
        )
    with pytest.raises(ResearchError, match="after the as-of"):
        MarketSnapshot(
            as_of=AS_OF,
            quotes=(InstrumentQuote("VWRP", Decimal("1"), AS_OF, "close"),),
            positions=(PaperPosition("VWRP", Decimal("0"), Decimal("0")),)[:0],
            nav_gbp=Decimal("10"),
            universe=("VWRP",),
            features=(MarketFeature("VWRP.return_1w", Decimal("1"), AS_OF, (AS_OF + timedelta(minutes=1),), "VWRP"),),
        )


def test_prompt_feature_block_is_compact() -> None:
    observations = _path("VWRP", date(2024, 5, 31), 6)
    context = build_market_context(as_of=AS_OF, observations=observations)
    snapshot = _book(AS_OF, observations)
    text = prompt_for(snapshot)
    assert "sources_hash" in text
    assert text.count("2024-06-03T16:30:00+00:00") < 30
    feature = next(item for item in context.features if item.name == "VWRP.return_1w")
    assert feature.value is not None
    assert len(feature.sources) == 2
