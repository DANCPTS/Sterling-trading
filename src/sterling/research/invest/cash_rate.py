"""Retail sterling cash rate from published SONIA, without look-ahead.

The Bank of England series IUDSOIA is the Sterling Overnight Index Average,
published as an annual percent. Easy-access retail deposits do not pay that
wholesale rate. The research haircut is half a percentage point, floored at
zero. It is a disclosed assumption, not a bank quote and not an operational cap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sterling.research.money import ZERO

SONIA_SERIES = "IUDSOIA"
SONIA_SOURCE = "Bank of England Statistical Interactive Database, series IUDSOIA (SONIA)"
HAIRCUT_PERCENTAGE_POINTS = Decimal("0.50")
HAIRCUT_RATIONALE = (
    "Easy-access retail sterling deposits are assumed to pay half a percentage point "
    "less than published SONIA, and never less than zero. The figure is not taken from a bank."
)


@dataclass(frozen=True, slots=True)
class CashRateQuote:
    series: str
    source: str
    published_on: date
    sonia_percent: Decimal
    haircut_percentage_points: Decimal
    annual_decimal: Decimal

    def as_log(self) -> dict[str, str]:
        return {
            "annual_decimal": format(self.annual_decimal, "f"),
            "haircut_percentage_points": format(self.haircut_percentage_points, "f"),
            "published_on": self.published_on.isoformat(),
            "series": self.series,
            "sonia_percent": format(self.sonia_percent, "f"),
            "source": self.source,
        }


def retail_annual_decimal(sonia_percent: Decimal) -> Decimal:
    """SONIA annual percent minus the haircut, as a decimal rate per year."""
    net_percent = sonia_percent - HAIRCUT_PERCENTAGE_POINTS
    if net_percent < 0:
        net_percent = ZERO
    return net_percent / Decimal(100)


def cash_quote(observations: Sequence[tuple[date, Decimal]], day: date) -> CashRateQuote | None:
    """Latest SONIA whose date is on or before ``day``. Later prints are ignored."""
    known = [(published, rate) for published, rate in observations if published <= day]
    if not known:
        return None
    published, percent = max(known)
    return CashRateQuote(
        series=SONIA_SERIES,
        source=SONIA_SOURCE,
        published_on=published,
        sonia_percent=percent,
        haircut_percentage_points=HAIRCUT_PERCENTAGE_POINTS,
        annual_decimal=retail_annual_decimal(percent),
    )


def parse_sonia_csv(text: str) -> tuple[tuple[date, Decimal], ...]:
    """Parse a Bank of England IUDSOIA CSV. Dates are ``02 Nov 2026`` or ISO."""
    rows: list[tuple[date, Decimal]] = []
    for line in text.splitlines():
        if not line.strip() or line.lower().startswith("date"):
            continue
        left, _, right = line.partition(",")
        if not right.strip():
            continue
        rows.append((_parse_date(left.strip()), Decimal(right.strip())))
    return tuple(sorted(rows))


def _parse_date(value: str) -> date:
    if len(value) == 10 and value[4] == "-":
        return date.fromisoformat(value)
    return datetime.strptime(value, "%d %b %Y").date()
