"""Data validation, pence normalisation, FX, and total-return handling."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sterling.research.bars import Bar, prepare_series, quote_scale
from sterling.research.calendar import xlon_weekday
from sterling.research.errors import DataValidationError, NetworkDisabled, UnverifiedInput
from sterling.research.loaders import LocalCsvSource, assert_local_path, read_stooq_csv


def _bar(day: date, close: str, **kwargs: object) -> Bar:
    price = Decimal(close)
    return Bar(
        session=day,
        open=kwargs.get("open", price),  # type: ignore[arg-type]
        high=kwargs.get("high", price),  # type: ignore[arg-type]
        low=kwargs.get("low", price),  # type: ignore[arg-type]
        close=price,
        volume=Decimal("10"),
        adjusted_close=kwargs.get("adjusted_close"),  # type: ignore[arg-type]
        dividend=kwargs.get("dividend"),  # type: ignore[arg-type]
        quoted_in=str(kwargs.get("quoted_in", "GBP")),
    )


def test_quote_scale_distinguishes_pence_from_pounds() -> None:
    assert quote_scale("GBP") == ("GBP", Decimal("1"))
    assert quote_scale("gbp") == ("GBP", Decimal("1"))
    assert quote_scale("GBp") == ("GBP", Decimal("100"))
    assert quote_scale("GBp (pence)") == ("GBP", Decimal("100"))
    assert quote_scale("GBX") == ("GBP", Decimal("100"))


def test_pence_prices_and_dividends_normalise_to_pounds() -> None:
    day = date(2024, 1, 2)
    prepared = prepare_series(
        ticker="CUKX",
        distribution="accumulating",
        trading_currency="GBP",
        bars=[_bar(day, "22300", quoted_in="GBp", dividend=Decimal("357.04"))],
        calendar=xlon_weekday(),
    )
    assert prepared.bars[0].close == Decimal("223")
    assert prepared.bars[0].dividend == Decimal("3.5704")
    xstr = prepare_series(
        ticker="XSTR",
        distribution="distributing",
        trading_currency="GBP",
        bars=[_bar(day, "18131", quoted_in="GBX")],
        calendar=xlon_weekday(),
    )
    assert xstr.bars[0].close == Decimal("181.31")
    pounds = prepare_series(
        ticker="VWRP",
        distribution="accumulating",
        trading_currency="GBP",
        bars=[_bar(day, "139.8", quoted_in="GBP")],
        calendar=xlon_weekday(),
    )
    assert pounds.bars[0].close == Decimal("139.8")


def test_gbp_traded_line_is_not_fx_converted_again() -> None:
    day = date(2024, 1, 2)
    prepared = prepare_series(
        ticker="VWRP",
        distribution="accumulating",
        trading_currency="GBP",
        bars=[_bar(day, "100")],
        calendar=xlon_weekday(),
        fx_gbp_per_unit={day: Decimal("0.50")},
    )
    assert prepared.bars[0].close == Decimal("100")
    assert any("not converted twice" in note for note in prepared.caveats)
    with pytest.raises(DataValidationError, match="second time"):
        prepare_series(
            ticker="VWRP",
            distribution="accumulating",
            trading_currency="GBP",
            bars=[_bar(day, "100", quoted_in="USD")],
            calendar=xlon_weekday(),
        )


def test_rejects_bad_prices_duplicates_weekends_and_gaps() -> None:
    calendar = xlon_weekday()
    monday = date(2024, 1, 8)
    tuesday = date(2024, 1, 9)
    wednesday = date(2024, 1, 10)
    with pytest.raises(DataValidationError, match="positive"):
        prepare_series(
            ticker="A",
            distribution="accumulating",
            trading_currency="GBP",
            bars=[_bar(monday, "0")],
            calendar=calendar,
        )
    with pytest.raises(DataValidationError, match="duplicate"):
        prepare_series(
            ticker="A",
            distribution="accumulating",
            trading_currency="GBP",
            bars=[_bar(monday, "10"), _bar(monday, "11")],
            calendar=calendar,
        )
    with pytest.raises(DataValidationError, match="not a trading day"):
        prepare_series(
            ticker="A",
            distribution="accumulating",
            trading_currency="GBP",
            bars=[_bar(date(2024, 1, 6), "10")],
            calendar=calendar,
        )
    with pytest.raises(DataValidationError, match="missing sessions"):
        prepare_series(
            ticker="A",
            distribution="accumulating",
            trading_currency="GBP",
            bars=[_bar(monday, "10"), _bar(wednesday, "10")],
            calendar=calendar,
        )
    with pytest.raises(DataValidationError, match="low"):
        prepare_series(
            ticker="A",
            distribution="accumulating",
            trading_currency="GBP",
            bars=[_bar(monday, "10", low=Decimal("11"))],
            calendar=calendar,
        )
    assert tuesday.isoformat()


def test_adjusted_close_scales_ohlc_and_does_not_also_reinvest() -> None:
    day = date(2024, 1, 2)
    prepared = prepare_series(
        ticker="IGLT",
        distribution="distributing",
        trading_currency="GBP",
        bars=[
            _bar(
                day,
                "50",
                open=Decimal("40"),
                high=Decimal("50"),
                low=Decimal("40"),
                adjusted_close=Decimal("100"),
                dividend=Decimal("1"),
            )
        ],
        calendar=xlon_weekday(),
    )
    assert prepared.return_basis == "adjusted_close"
    assert prepared.dividend_mode == "ignore"
    assert prepared.bars[0].close == Decimal("100")
    assert prepared.bars[0].open == Decimal("80")
    assert prepared.bars[0].dividend == 0
    assert any("not also reinvested" in note for note in prepared.caveats)


def test_out_of_scale_adjusted_close_does_not_become_the_fill() -> None:
    day = date(2024, 1, 2)
    prepared = prepare_series(
        ticker="IGLT",
        distribution="distributing",
        trading_currency="GBP",
        bars=[_bar(day, "50", adjusted_close=Decimal("1000"))],
        calendar=xlon_weekday(),
    )
    assert prepared.return_basis == "adjusted_close_index"
    assert prepared.bars[0].open == Decimal("50")
    assert prepared.bars[0].signal_close == Decimal("1000")


def test_distributing_without_income_is_price_only() -> None:
    day = date(2024, 1, 2)
    prepared = prepare_series(
        ticker="IGLT",
        distribution="distributing",
        trading_currency="GBP",
        bars=[_bar(day, "10")],
        calendar=xlon_weekday(),
    )
    assert prepared.return_basis == "price_only"
    assert any("price-only" in note for note in prepared.caveats)


def test_explicit_distribution_mode_keeps_the_dividend() -> None:
    day = date(2024, 1, 2)
    prepared = prepare_series(
        ticker="IGLT",
        distribution="distributing",
        trading_currency="GBP",
        bars=[_bar(day, "10", dividend=Decimal("0.25"))],
        calendar=xlon_weekday(),
        return_method="distributions",
    )
    assert prepared.return_basis == "distributions"
    assert prepared.dividend_mode == "reinvest"
    assert prepared.bars[0].dividend == Decimal("0.25")


def test_local_and_stooq_loaders_do_not_guess_or_fetch(tmp_path: object) -> None:
    from pathlib import Path

    from sterling.research.instruments import load_candidate_universe

    directory = Path(str(tmp_path))
    universe = load_candidate_universe()
    (directory / "CUKX.csv").write_text(
        "date,open,high,low,close,volume,quoted_in\n2024-01-02,22300,22300,22300,22300,10,GBp\n",
        encoding="utf-8",
    )
    (directory / "VWRP.csv").write_text(
        "date,open,high,low,close,volume\n2024-01-02,139.8,140,139,139.8,10\n",
        encoding="utf-8",
    )
    cukx = LocalCsvSource(directory, universe).load("CUKX")
    assert cukx[0].close == Decimal("22300")
    assert cukx[0].quoted_in == "GBp"
    vwrp = LocalCsvSource(directory, universe).load("VWRP")
    assert vwrp[0].quoted_in == "GBP"
    (directory / "CUKX.csv").write_text(
        "date,open,high,low,close,quoted_in\n2024-01-02,223,223,223,223,GBP\n",
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="conflicts"):
        LocalCsvSource(directory, universe).load("CUKX")
    stooq = directory / "vwrp.uk.csv"
    stooq.write_text("Date,Open,High,Low,Close,Volume\n20240102,100,101,99,100,5\n", encoding="utf-8")
    with pytest.raises(UnverifiedInput, match="UNVERIFIED"):
        read_stooq_csv(stooq, quoted_in=None)
    parsed = read_stooq_csv(stooq, quoted_in="GBP")
    assert parsed[0].session == date(2024, 1, 2)
    assert parsed[0].close == Decimal("100")
    with pytest.raises(NetworkDisabled):
        assert_local_path(Path("https://example.invalid/vwrp.csv"))
