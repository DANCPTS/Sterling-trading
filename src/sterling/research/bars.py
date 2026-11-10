"""Daily bars, pence normalisation, validation, and total-return handling.

Quote units:

- ``GBP`` is pounds and is left unchanged.
- ``GBp`` and ``GBX`` are pence. Prices and dividends are divided by 100.
- A lowercased ``gbp`` is pounds. Do not lowercase the Yahoo ``GBp`` code.

GBP-traded instruments are not FX-converted. Underlying currency exposure is
already in the sterling price. A supplied FX map is ignored for those lines.

Total return, in order:

- Commensurate adjusted closes scale that bar's OHLC. Dividends on the same
  bars are ignored so they are not counted twice. Fills use the adjusted open.
  This is a research approximation of the traded print.
- Otherwise an explicit dividend is kept for reinvestment and the fill uses
  the raw open.
- An accumulating share with neither input uses the price as published.
- A distributing share with neither input is marked price-only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sterling.research.calendar import WeekdayCalendar
from sterling.research.errors import DataValidationError, ResearchError
from sterling.research.money import ZERO, parse_decimal

_PENCE = Decimal("100")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class Bar:
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = ZERO
    adjusted_close: Decimal | None = None
    dividend: Decimal | None = None
    quoted_in: str = "GBP"
    pay_date: date | None = None
    signal_level: Decimal | None = None


@dataclass(frozen=True, slots=True)
class GbpBar:
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    signal_close: Decimal
    volume: Decimal
    dividend: Decimal
    pay_date: date | None = None


@dataclass(frozen=True, slots=True)
class PreparedSeries:
    ticker: str
    bars: tuple[GbpBar, ...]
    return_basis: str
    dividend_mode: str
    caveats: tuple[str, ...]


def quote_scale(quoted_in: str) -> tuple[str, Decimal]:
    """Return ``(major_currency, divisor)`` for a quote-unit label."""
    compact = "".join(quoted_in.strip().split())
    lowered = compact.lower()
    if lowered in {"gbx", "gbpence"} or "pence" in lowered or compact == "GBp":
        return "GBP", _PENCE
    if compact in {"GBP", "gbp", "£"}:
        return "GBP", _ONE
    if compact in {"USD", "usd"}:
        return "USD", _ONE
    if compact in {"EUR", "eur"}:
        return "EUR", _ONE
    raise DataValidationError(f"unsupported quote unit {quoted_in!r}")


def normalise_quote(amount: Decimal, quoted_in: str) -> tuple[Decimal, str]:
    major, divisor = quote_scale(quoted_in)
    return amount / divisor, major


def _require_positive(value: Decimal, label: str) -> None:
    if value <= 0:
        raise DataValidationError(f"{label} must be positive")


def _enclose_open_close(bar: Bar) -> tuple[Bar, bool]:
    high = max(bar.high, bar.open, bar.close)
    low = min(bar.low, bar.open, bar.close)
    if high == bar.high and low == bar.low:
        return bar, False
    return (
        Bar(
            session=bar.session,
            open=bar.open,
            high=high,
            low=low,
            close=bar.close,
            volume=bar.volume,
            adjusted_close=bar.adjusted_close,
            dividend=bar.dividend,
            quoted_in=bar.quoted_in,
            pay_date=bar.pay_date,
            signal_level=bar.signal_level,
        ),
        True,
    )


def validate_bar_shape(bar: Bar) -> None:
    _require_positive(bar.open, f"{bar.session.isoformat()} open")
    _require_positive(bar.high, f"{bar.session.isoformat()} high")
    _require_positive(bar.low, f"{bar.session.isoformat()} low")
    _require_positive(bar.close, f"{bar.session.isoformat()} close")
    if bar.high < bar.low or bar.high < bar.open or bar.high < bar.close:
        raise DataValidationError(f"{bar.session.isoformat()} high is below open, close, or low")
    if bar.low > bar.open or bar.low > bar.close:
        raise DataValidationError(f"{bar.session.isoformat()} low is above open or close")
    if bar.volume < 0:
        raise DataValidationError(f"{bar.session.isoformat()} volume is negative")
    if bar.adjusted_close is not None and bar.adjusted_close <= 0:
        raise DataValidationError(f"{bar.session.isoformat()} adjusted close must be positive")
    if bar.dividend is not None and bar.dividend < 0:
        raise DataValidationError(f"{bar.session.isoformat()} dividend is negative")


def validate_sessions(bars: tuple[Bar, ...], calendar: WeekdayCalendar) -> tuple[Bar, ...]:
    if not bars:
        raise DataValidationError("price series is empty")
    ordered = tuple(sorted(bars, key=lambda bar: bar.session))
    seen: set[date] = set()
    for bar in ordered:
        if bar.session in seen:
            raise DataValidationError(f"duplicate session {bar.session.isoformat()}")
        seen.add(bar.session)
        if not calendar.is_trading_day(bar.session):
            raise DataValidationError(f"{bar.session.isoformat()} is not a trading day on {calendar.calendar_id}")
    expected = calendar.trading_days(ordered[0].session, ordered[-1].session)
    missing = [day for day in expected if day not in seen]
    if missing:
        sample = ", ".join(day.isoformat() for day in missing[:5])
        raise DataValidationError(f"missing sessions: {sample}")
    return ordered


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _scale_bar(bar: Bar, major: str) -> Bar:
    def scale(amount: Decimal) -> Decimal:
        scaled, _ = normalise_quote(amount, bar.quoted_in)
        return scaled

    dividend = None if bar.dividend is None else scale(bar.dividend)
    adjusted = None if bar.adjusted_close is None else scale(bar.adjusted_close)
    return Bar(
        session=bar.session,
        open=scale(bar.open),
        high=scale(bar.high),
        low=scale(bar.low),
        close=scale(bar.close),
        volume=bar.volume,
        adjusted_close=adjusted,
        dividend=dividend,
        quoted_in=major,
        pay_date=bar.pay_date,
        signal_level=bar.signal_level,
    )


def prepare_series(
    *,
    ticker: str,
    distribution: str,
    trading_currency: str,
    bars: tuple[Bar, ...] | list[Bar],
    calendar: WeekdayCalendar,
    fx_gbp_per_unit: Mapping[date, Decimal] | None = None,
    return_method: str = "auto",
) -> PreparedSeries:
    if trading_currency != "GBP":
        raise ResearchError(f"{ticker} trades in {trading_currency}. This milestone prepares GBP-traded lines only.")
    scaled: list[Bar] = []
    range_repairs = 0
    for bar in bars:
        major, _divisor = quote_scale(bar.quoted_in)
        if major != "GBP":
            raise DataValidationError(
                f"{ticker} is GBP-traded. A {major} price would convert underlying FX a second time. "
                "Supply the sterling line and normalise pence to pounds instead."
            )
        converted, repaired = _enclose_open_close(_scale_bar(bar, major))
        range_repairs += int(repaired)
        validate_bar_shape(converted)
        scaled.append(converted)
    ordered = validate_sessions(tuple(scaled), calendar)
    caveats: list[str] = []
    if range_repairs:
        caveats.append(
            f"{ticker} high/low was expanded to include the open and close on {range_repairs} sessions. "
            "The vendor range did not contain those prints. Signals still use the close and fills still use the open."
        )
    if fx_gbp_per_unit is not None:
        caveats.append(f"{ticker} is GBP-traded. The supplied FX map was ignored so the sterling price is not converted twice.")
    return _apply_total_return(ticker, distribution, ordered, return_method, tuple(caveats))


def _apply_total_return(
    ticker: str,
    distribution: str,
    bars: tuple[Bar, ...],
    return_method: str,
    caveats: tuple[str, ...],
) -> PreparedSeries:
    has_adjusted = [bar.adjusted_close is not None for bar in bars]
    if any(has_adjusted) and not all(has_adjusted):
        raise DataValidationError(f"{ticker} adjusted close must be present on every bar or on none")
    has_dividend = any(bar.dividend not in (None, ZERO) for bar in bars)
    use_adjusted = False
    index_signal = False
    if return_method == "price_only":
        use_adjusted = False
    elif return_method == "distributions":
        use_adjusted = False
        if not has_dividend:
            raise DataValidationError(f"{ticker} has no dividend column for distribution reinvestment")
    elif return_method in {"auto", "adjusted_close"}:
        use_adjusted = all(has_adjusted)
        if return_method == "adjusted_close" and not use_adjusted:
            raise DataValidationError(f"{ticker} has no adjusted close")
    else:
        raise ResearchError(f"unknown return method {return_method}")

    notes = list(caveats)
    if use_adjusted:
        ratios: list[Decimal] = []
        for bar in bars:
            assert bar.adjusted_close is not None
            ratios.append(bar.adjusted_close / bar.close)
        median = _median(ratios)
        commensurate = Decimal("0.2") <= median <= Decimal("5")
        # A series whose adjusted close never leaves the raw close is not a
        # total-return adjustment. Yahoo does this for some distributing LSE
        # lines, including XSTR. Reinvest the dividend column instead.
        adjustment_is_material = any(abs(ratio - 1) > Decimal("0.005") for ratio in ratios)
        if has_dividend and not adjustment_is_material and return_method == "auto":
            notes.append(
                f"{ticker} adjusted close stays within 0.5% of the raw close, so it is not used as a "
                "total-return series. Explicit distributions are reinvested instead."
            )
        else:
            if not commensurate:
                index_signal = True
                notes.append(
                    f"{ticker} adjusted close is not on the traded-price scale, so fills stay on the raw open "
                    "and the signal uses the index level."
                )
            else:
                notes.append(f"{ticker} uses adjusted OHLC for signals and fills. Dividends are not also reinvested.")
            if has_dividend:
                notes.append(f"{ticker} dividend column was ignored to avoid double-counting adjusted close.")
            prepared = []
            for bar in bars:
                assert bar.adjusted_close is not None
                if commensurate:
                    ratio = bar.adjusted_close / bar.close
                    prepared.append(
                        GbpBar(
                            session=bar.session,
                            open=bar.open * ratio,
                            high=bar.high * ratio,
                            low=bar.low * ratio,
                            close=bar.adjusted_close,
                            signal_close=bar.adjusted_close,
                            volume=bar.volume,
                            dividend=ZERO,
                        )
                    )
                else:
                    prepared.append(
                        GbpBar(
                            session=bar.session,
                            open=bar.open,
                            high=bar.high,
                            low=bar.low,
                            close=bar.close,
                            signal_close=bar.adjusted_close,
                            volume=bar.volume,
                            dividend=ZERO,
                        )
                    )
            basis = "adjusted_close_index" if index_signal else "adjusted_close"
            return PreparedSeries(ticker, tuple(prepared), basis, "ignore", tuple(notes))

    if has_dividend or return_method == "distributions":
        notes.append(
            f"{ticker} total return reinvests explicit distributions at the same session close. "
            "Fills use the raw open. Do not also apply an adjusted close."
        )
        basis = "distributions"
        mode = "reinvest"
    elif distribution == "accumulating":
        notes.append(
            f"{ticker} is accumulating. The published price is the reinvesting share price, net of fees. "
            "No extra dividend is added."
        )
        basis = "accumulating_price"
        mode = "ignore"
    elif distribution == "none":
        notes.append(f"{ticker} has no distribution. The price series is the traded line.")
        basis = "price"
        mode = "ignore"
    else:
        notes.append(
            f"{ticker} is distributing and this series is price-only. Total return is understated "
            "until an adjusted close or a distribution column is supplied."
        )
        basis = "price_only"
        mode = "ignore"
    prepared = [
        GbpBar(
            session=bar.session,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            signal_close=bar.signal_level if bar.signal_level is not None else bar.close,
            volume=bar.volume,
            dividend=bar.dividend or ZERO,
            pay_date=bar.pay_date,
        )
        for bar in bars
    ]
    return PreparedSeries(ticker, tuple(prepared), basis, mode, tuple(notes))


def parse_decimal_cell(text: str, *, label: str) -> Decimal:
    return parse_decimal(text, label=label)
