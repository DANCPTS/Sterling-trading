"""Read-only Sterling Invest page. Figures come from verified records only."""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sterling.research.invest.report import _decimal, _money, _pct

LONDON = ZoneInfo("Europe/London")
_YAHOO = "Yahoo only / not exchange verified"


def render_dashboard(records: Sequence[Mapping[str, object]]) -> str:
    """One static page. Logged strings are escaped. No order controls."""
    lane = _lane(records)
    latest = _last(records, "valuation")
    performance = _last(records, "performance")
    label = str(latest.get("data_label") or performance.get("data_label") or "unlabelled")
    as_of = _as_of(records)
    start = _start_cash(records)
    nav = latest.get("nav_gbp")
    cumulative = _cumulative_pnl(nav, start)
    realised, sells, unrealised = _pnl(records, latest)
    pending_interest = _pending_interest(records)
    accrued = _accrued_interest(records)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Sterling Invest</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #0e1116; color: #e7ecf3; font-family: "Segoe UI", system-ui, sans-serif; }}
header, main {{ padding: 1rem 1.1rem 0; }}
header {{ display: flex; flex-wrap: wrap; gap: 0.6rem 1rem; align-items: center; justify-content: space-between; }}
h1 {{ font-size: 1.35rem; font-weight: 600; margin: 0; }}
h2 {{ font-size: 1rem; font-weight: 600; margin: 0 0 0.6rem; }}
.badge {{ font-weight: 700; letter-spacing: 0.04em; border-radius: 999px; padding: 0.35rem 0.7rem; }}
.badge.mock {{ background: #3a2a12; color: #ffc14d; }}
.badge.forward {{ background: #12301f; color: #7dffa8; }}
.asof {{ color: #9aa3b2; font-size: 0.9rem; }}
.products {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 0.8rem 0; }}
.product {{ border: 1px solid #2a3140; border-radius: 0.4rem; padding: 0.35rem 0.6rem; }}
.product.on {{ background: #1c2433; }}
.product.off {{ color: #9aa3b2; }}
.banner, .warn {{ background: #241c12; color: #ffd18a; border-radius: 0.4rem; padding: 0.55rem 0.7rem; margin: 0.4rem 0; }}
.cards {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.6rem; margin: 0.8rem 0; }}
.card {{ background: #171b22; border-radius: 0.5rem; padding: 0.7rem 0.8rem; }}
.card span {{ display: block; color: #9aa3b2; font-size: 0.78rem; }}
.card strong {{ font-size: 1.15rem; }}
.up {{ color: #3dd68c; }}
.down {{ color: #ff6b6b; }}
section {{ margin: 1rem 0 1.4rem; }}
.scroll {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; min-width: 36rem; }}
th, td {{ border-bottom: 1px solid #2a3140; text-align: right; padding: 0.4rem 0.45rem; font-size: 0.92rem; }}
th:first-child, td:first-child {{ text-align: left; }}
.calendar {{ display: flex; flex-wrap: wrap; gap: 0.4rem; }}
.day {{ background: #171b22; border-radius: 0.4rem; min-width: 6.5rem; padding: 0.45rem 0.55rem; }}
.day span, .day strong {{ display: block; }}
.chart {{ width: 100%; height: 8rem; background: #171b22; border-radius: 0.4rem; }}
pre {{ white-space: pre-wrap; word-break: break-word; background: #12161c; padding: 0.6rem; border-radius: 0.4rem; }}
details {{ margin-top: 0.4rem; }}
@media (max-width: 720px) {{
  .cards {{ grid-template-columns: 1fr 1fr; }}
  header {{ align-items: flex-start; }}
  table {{ min-width: 28rem; }}
}}
</style>
</head>
<body>
<header>
<div>
<h1>Sterling Invest</h1>
<p class="asof">Data as of {_esc(_london(as_of))}. System {_esc(", ".join(_systems(records)) or "—")}</p>
</div>
<p class="badge {_esc(lane)}" data-lane="{_esc(lane)}">{_esc(_lane_label(lane))}</p>
</header>
<main>
<div class="products" aria-label="Portfolio">
<span class="product on">Sterling Invest</span>
<span class="product off">Sterling FX: not built yet</span>
</div>
<p class="banner">DATA LABEL: {_esc(label)}. Read only. This page cannot place an order.</p>
<p class="warn">{_esc(_YAHOO)}</p>
{_warnings(latest, pending_interest)}
<section>
<h2>Portfolio</h2>
<div class="cards">
{_card("Starting capital", _money(start) if start is not None else "—")}
{_card("NAV", _money(nav))}
{_card("Day P&L", _signed(latest.get("daily_pnl_gbp")))}
{_card("Cumulative P&L", _signed(cumulative))}
{_card("Cash", _money(latest.get("cash_gbp")))}
{_card("Cumulative costs", _money(latest.get("cumulative_costs_gbp")))}
{_card("Drawdown", _pct(latest.get("drawdown")))}
{_card("Cumulative return", _pct(latest.get("cumulative_return")))}
</div>
<p>{_pnl_line(realised, sells, unrealised, accrued, pending_interest)}</p>
</section>
<section>
<h2>Daily P&amp;L</h2>
{_chart(records)}
<div class="calendar">{_calendar(records)}</div>
</section>
<section>
<h2>Holdings</h2>
<div class="scroll"><table>
<thead><tr><th>Fund</th><th>Units</th><th>Price</th><th>Value</th><th>Status</th></tr></thead>
<tbody>{_holdings(latest)}</tbody>
</table></div>
</section>
<section>
<h2>Decisions</h2>
<p>A decision is not a fill. Times are the recorded timestamps.</p>
{_decisions(records)}
</section>
<section>
<h2>Fills</h2>
<div class="scroll"><table>
<thead><tr><th>Recorded</th><th>Session</th><th>Side</th><th>Fund</th><th>Units</th><th>Price</th><th>Cash</th></tr></thead>
<tbody>{_fills(records)}</tbody>
</table></div>
</section>
<section>
<h2>Pending and expired orders</h2>
{_orders(records)}
</section>
<section>
<h2>Benchmarks</h2>
<div class="scroll"><table>
<thead><tr><th>Book</th><th>End value</th><th>Return</th><th>Max drawdown</th><th>Trades</th><th>Costs</th></tr></thead>
<tbody>{_benchmarks(performance)}</tbody>
</table></div>
</section>
<section>
<h2>Grok</h2>
{_grok(records, lane)}
</section>
<section>
<h2>Model usage</h2>
{_usage(records)}
</section>
</main>
</body>
</html>
"""


def _systems(records: Sequence[Mapping[str, object]]) -> list[str]:
    return sorted({str(record.get("system")) for record in records if record.get("system")})


def _lane(records: Sequence[Mapping[str, object]]) -> str:
    lanes = {record.get("evidence_lane") for record in records}
    if lanes == {"mock"}:
        return "mock"
    return "forward"


def _lane_label(lane: str) -> str:
    if lane == "mock":
        return "MOCK / PAPER"
    return "GENUINE PAPER"


def _last(records: Sequence[Mapping[str, object]], kind: str) -> Mapping[str, object]:
    found = [record for record in records if record.get("record_type") == kind]
    return found[-1] if found else {}


def _as_of(records: Sequence[Mapping[str, object]]) -> str:
    stamps = [str(record.get("timestamp_utc")) for record in records if record.get("timestamp_utc")]
    return max(stamps) if stamps else ""


def _start_cash(records: Sequence[Mapping[str, object]]) -> Decimal | None:
    for record in records:
        if record.get("record_type") == "decision" and record.get("cash_gbp") not in (None, ""):
            return _decimal(record.get("cash_gbp"))
    return None


def _cumulative_pnl(nav: object, start: Decimal | None) -> Decimal | None:
    latest = _decimal(nav)
    if latest is None or start is None:
        return None
    return latest - start


def _pnl(
    records: Sequence[Mapping[str, object]],
    latest: Mapping[str, object],
) -> tuple[Decimal, int, Decimal | None]:
    quantity: dict[str, Decimal] = {}
    cost: dict[str, Decimal] = {}
    realised = Decimal(0)
    sells = 0
    supported = True
    for record in records:
        if record.get("record_type") != "fill":
            continue
        fund = str(record.get("fund"))
        units = _decimal(record.get("units"))
        cash_delta = _decimal(record.get("cash_delta_gbp"))
        if units is None or cash_delta is None or units <= 0:
            supported = False
            continue
        if record.get("side") == "BUY":
            quantity[fund] = quantity.get(fund, Decimal(0)) + units
            cost[fund] = cost.get(fund, Decimal(0)) + (-cash_delta)
        elif record.get("side") == "SELL":
            sells += 1
            held = quantity.get(fund, Decimal(0))
            booked = cost.get(fund, Decimal(0))
            released = (booked / held * units) if held > 0 else Decimal(0)
            realised += cash_delta - released
            quantity[fund] = held - units
            cost[fund] = booked - released
    prices = latest.get("prices")
    unrealised: Decimal | None = Decimal(0) if supported else None
    if not isinstance(prices, dict):
        return realised, sells, None if quantity else unrealised
    if unrealised is None:
        return realised, sells, None
    for fund, held in quantity.items():
        if held <= 0:
            continue
        info = prices.get(fund)
        price = _decimal(info.get("price_gbp")) if isinstance(info, dict) else None
        if price is None:
            return realised, sells, None
        unrealised += held * price - cost.get(fund, Decimal(0))
    return realised, sells, unrealised


def _pending_interest(records: Sequence[Mapping[str, object]]) -> list[str]:
    pending = [str(record.get("session")) for record in records if record.get("record_type") == "interest_pending"]
    accrued = {
        str(record.get("session"))
        for record in records
        if record.get("record_type") == "cash_interest" and str(record.get("status") or "accrued") != "pending"
    }
    return [day for day in pending if day not in accrued]


def _accrued_interest(records: Sequence[Mapping[str, object]]) -> Decimal | None:
    amounts = [
        _decimal(record.get("amount_gbp"))
        for record in records
        if record.get("record_type") == "cash_interest" and str(record.get("status") or "accrued") != "pending"
    ]
    numbers = [amount for amount in amounts if amount is not None]
    if not numbers:
        return None
    return sum(numbers, Decimal(0))


def _warnings(latest: Mapping[str, object], pending_interest: list[str]) -> str:
    notes = [_price_notes(latest)]
    if pending_interest:
        days = ", ".join(pending_interest)
        notes.append(f'<p class="warn">SONIA interest pending for {_esc(days)}. Not accrued and not zero.</p>')
    rate = latest.get("cash_rate")
    if isinstance(rate, dict) and rate.get("status") == "unavailable":
        notes.append(f'<p class="warn">SONIA print unavailable ({_esc(rate.get("reason"))}). Not accrued and not zero.</p>')
    elif isinstance(rate, dict) and rate.get("status") == "accrued":
        notes.append(
            "<p>Accrued SONIA "
            f"{_esc(rate.get('sonia_percent'))} published {_esc(rate.get('published_on'))}, "
            f"available {_esc(rate.get('available_at'))}.</p>"
        )
    return "\n".join(note for note in notes if note)


def _price_notes(latest: Mapping[str, object]) -> str:
    prices = latest.get("prices")
    if not isinstance(prices, dict):
        return ""
    bits = []
    for name, info in prices.items():
        if not isinstance(info, dict):
            continue
        status = str(info.get("status") or "")
        if status in {"stale", "missing"}:
            bits.append(f"{name} {status} ({info.get('cross_check')})")
    if not bits:
        return ""
    return f'<p class="warn">Price warnings: {_esc("; ".join(bits))}.</p>'


def _calendar(records: Sequence[Mapping[str, object]]) -> str:
    rows = [record for record in records if record.get("record_type") == "valuation"]
    if not rows:
        return "<p>No verified marks yet.</p>"
    cells = []
    for record in rows:
        amount = _decimal(record.get("daily_pnl_gbp"))
        tone = "up" if amount is not None and amount > 0 else "down" if amount is not None and amount < 0 else ""
        cells.append(
            f'<div class="day"><span>{_esc(record.get("session"))}</span>'
            f'<strong class="{tone}">{_esc(_signed(record.get("daily_pnl_gbp")))}</strong></div>'
        )
    return "".join(cells)


def _chart(records: Sequence[Mapping[str, object]]) -> str:
    rows = [record for record in records if record.get("record_type") == "valuation"]
    amounts = [_decimal(record.get("daily_pnl_gbp")) or Decimal(0) for record in rows]
    if not amounts:
        return ""
    peak = max((abs(amount) for amount in amounts), default=Decimal(1)) or Decimal(1)
    width = Decimal(100) / Decimal(len(amounts))
    bars = []
    for index, amount in enumerate(amounts):
        height = abs(amount) / peak * Decimal(40)
        y = Decimal(50) - height if amount >= 0 else Decimal(50)
        colour = "#3dd68c" if amount >= 0 else "#ff6b6b"
        bars.append(
            f'<rect x="{index * width + width * Decimal("0.15")}" y="{y}" '
            f'width="{width * Decimal("0.7")}" height="{height}" fill="{colour}"/>'
        )
    return f'<svg class="chart" viewBox="0 0 100 100" role="img" aria-label="Daily P&amp;L">{"".join(bars)}</svg>'


def _holdings(latest: Mapping[str, object]) -> str:
    held = latest.get("holdings")
    prices = latest.get("prices")
    if not isinstance(held, dict) or not held:
        return '<tr><td colspan="5">No holdings recorded.</td></tr>'
    rows = []
    nav = _decimal(latest.get("nav_gbp"))
    for name in sorted(held):
        units = _decimal(held.get(name))
        info = prices.get(name) if isinstance(prices, dict) else None
        price = _decimal(info.get("price_gbp")) if isinstance(info, dict) else None
        status = str(info.get("status")) if isinstance(info, dict) else ""
        value = units * price if units is not None and price is not None else None
        weight = f" ({_pct(value / nav)})" if value is not None and nav not in (None, 0) else ""
        rows.append(
            "<tr>"
            f"<td>{_esc(name)}</td><td>{_esc(held.get(name))}</td><td>{_esc(_money(price))}</td>"
            f"<td>{_esc(_money(value))}{_esc(weight)}</td><td>{_esc(status)}</td></tr>"
        )
    return "".join(rows)


def _decisions(records: Sequence[Mapping[str, object]]) -> str:
    rows = [record for record in records if record.get("record_type") == "decision"]
    if not rows:
        return "<p>No decision recorded.</p>"
    blocks = []
    for record in rows:
        blocks.append(
            "<article>"
            f"<p>{_esc(_london(str(record.get('timestamp_utc') or '')))} "
            f"gate {_esc(record.get('gate'))} confidence {_esc(record.get('confidence') or '—')}</p>"
            f"<p>Derived actions, not fills: {_esc(_actions(record.get('derived_actions')))}</p>"
            "</article>"
        )
    return "".join(blocks)


def _fills(records: Sequence[Mapping[str, object]]) -> str:
    rows = [record for record in records if record.get("record_type") == "fill"]
    if not rows:
        return '<tr><td colspan="7">No fills recorded.</td></tr>'
    body = []
    for record in rows:
        body.append(
            "<tr>"
            f"<td>{_esc(_london(str(record.get('timestamp_utc') or '')))}</td>"
            f"<td>{_esc(record.get('session'))}</td>"
            f"<td>{_esc(record.get('side'))}</td>"
            f"<td>{_esc(record.get('fund'))}</td>"
            f"<td>{_esc(record.get('units'))}</td>"
            f"<td>{_esc(_money(record.get('price_gbp')))}</td>"
            f"<td>{_esc(_money(record.get('cash_delta_gbp')))}</td>"
            "</tr>"
        )
    return "".join(body)


def _orders(records: Sequence[Mapping[str, object]]) -> str:
    pending: dict[str, Mapping[str, object]] = {}
    expired: list[Mapping[str, object]] = []
    for record in records:
        if record.get("record_type") == "decision" and record.get("gate") == "accepted":
            pending = {}
        elif record.get("record_type") == "fill":
            pending.pop(str(record.get("fund")), None)
        elif record.get("record_type") == "fill_skipped" and record.get("reason") == "EXPIRED":
            pending.pop(str(record.get("fund")), None)
            expired.append(record)
        elif record.get("record_type") == "fill_skipped" and record.get("reason") in {
            "opening_price_unavailable",
            "valuation_incomplete",
        }:
            pending[str(record.get("fund"))] = record
    lines = []
    if not pending:
        lines.append("<p>No pending order.</p>")
    for fund, record in sorted(pending.items()):
        lines.append(f"<p>Pending {_esc(fund)}: {_esc(record.get('reason'))} on {_esc(record.get('session'))}.</p>")
    if not expired:
        lines.append("<p>No EXPIRED order.</p>")
    for record in expired:
        lines.append(
            f"<p>EXPIRED {_esc(record.get('fund'))} on {_esc(record.get('session'))} "
            f"({_esc(_london(str(record.get('timestamp_utc') or '')))}).</p>"
        )
    return "".join(lines)


def _benchmarks(performance: Mapping[str, object]) -> str:
    raw = performance.get("arms")
    arms = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if not arms:
        return '<tr><td colspan="6">No benchmark recorded.</td></tr>'
    rows = []
    for arm in arms:
        rows.append(
            "<tr>"
            f"<td>{_esc(arm.get('name'))}</td>"
            f"<td>{_esc(_money(arm.get('end_nav_gbp')))}</td>"
            f"<td>{_esc(_pct(arm.get('cumulative_return')))}</td>"
            f"<td>{_esc(_pct(arm.get('max_drawdown')))}</td>"
            f"<td>{_esc(arm.get('trades'))}</td>"
            f"<td>{_esc(_money(arm.get('costs_gbp')))}</td>"
            "</tr>"
        )
    return "".join(rows)


def _grok(records: Sequence[Mapping[str, object]], lane: str) -> str:
    decisions = [record for record in records if record.get("record_type") == "decision"]
    if not decisions:
        return "<p>No AI explanation recorded.</p>"
    blocks = []
    mock = lane == "mock"
    for record in decisions:
        prompt = str(record.get("prompt_text") or "")
        reply = str(record.get("raw_reply") or "")
        reasons = record.get("reasons")
        label = '<p class="warn">MOCK</p>' if mock else ""
        response = _esc(reply) if reply else "No AI explanation recorded"
        reason_text = _esc(reasons) if reasons not in (None, "", []) else "No reasons recorded"
        prompt_block = f"<pre>{_esc(prompt)}</pre>" if prompt else "<p>No prompt stored.</p>"
        blocks.append(
            f"{label}<p>Response: {response}</p><p>Recorded reasons: {reason_text}</p>"
            f"<details><summary>Stored prompt</summary>{prompt_block}</details>"
        )
    return "".join(blocks)


def _usage(records: Sequence[Mapping[str, object]]) -> str:
    rows = [record for record in records if record.get("record_type") == "model_usage"]
    if not rows:
        return "<p>No model usage recorded.</p>"
    lines = []
    for record in rows:
        lines.append(
            "<p>"
            f"input {_esc(record.get('input_tokens'))}, cached {_esc(record.get('cached_tokens'))}, "
            f"output {_esc(record.get('output_tokens'))}, reasoning {_esc(record.get('reasoning_tokens'))}, "
            f"cost ticks {_esc(record.get('cost_in_usd_ticks'))}, request {_esc(record.get('provider_request_id'))}, "
            f"latency ms {_esc(record.get('latency_ms'))}."
            "</p>"
        )
    return "".join(lines)


def _card(label: str, value: str) -> str:
    tone = "up" if value.startswith("+") else "down" if value.startswith("-") else ""
    return f'<div class="card"><span>{_esc(label)}</span><strong class="{tone}">{_esc(value)}</strong></div>'


def _pnl_line(
    realised: Decimal,
    sells: int,
    unrealised: Decimal | None,
    accrued: Decimal | None,
    pending_interest: list[str],
) -> str:
    realised_text = _signed(realised) if sells else "No sell fills recorded"
    unrealised_text = _signed(unrealised) if unrealised is not None else "Not available from the ledger"
    if accrued is None and pending_interest:
        interest = "pending, not zero"
    elif accrued is None:
        interest = "No accrued interest"
    else:
        interest = _signed(accrued)
    return (
        f"Realised P&amp;L: {_esc(realised_text)}. Unrealised P&amp;L: {_esc(unrealised_text)}. Accrued interest: {_esc(interest)}."
    )


def _actions(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "—"
    return ", ".join(f"{name} {side}" for name, side in sorted(value.items()))


def _signed(value: object) -> str:
    amount = _decimal(value)
    if amount is None:
        return "—"
    text = _money(abs(amount))
    if amount > 0:
        return f"+{text}"
    if amount < 0:
        return f"-{text}"
    return text


def _london(value: str) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(LONDON)
    return local.strftime("%d %b %Y %H:%M %Z")


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)
