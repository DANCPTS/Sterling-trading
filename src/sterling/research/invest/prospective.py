"""Prospective research valuation. No network, no model, no official evidence.

A run is prospective only when the captures justify it. A caller cannot rename
the retrospective command and keep the same files. Each close must be for the
valuation session, available at or before the clock, and retrieved at or
before the clock and not materially before that availability. An older close
is not substituted. SONIA uses the same publication rule as ``cash_quote``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from sterling.research.calendar import LONDON
from sterling.research.errors import ResearchError, ResearchIsolationError
from sterling.research.fx.grok_forward import GENESIS, ProposalLog
from sterling.research.invest.availability import BASIS_ASSUMED, BASIS_VERIFIED, assumed_available_at, exchange_close_at
from sterling.research.invest.cash_rate import parse_sonia_csv, sonia_available_at
from sterling.research.invest.daily import run_daily
from sterling.research.invest.lanes import assert_lane, classify_log
from sterling.research.invest.schema import FUNDS

APP_ROOT = Path(__file__).resolve().parents[4]
MOCK_SUFFIX = "evidence/invest-v2/mock/forward.jsonl"
EVIDENCE_REMOTE = "sterling-forward-evidence"
MODE_PROSPECTIVE = "prospective_research"
MODE_FIXTURE = "offline_fixture"
CAPTURE_SKEW = timedelta(seconds=120)
CREATE_OFFSET = timedelta(seconds=2)
CLOCK_TOLERANCE = timedelta(minutes=5)
PROVIDER_TOKENS = ("--provider", "--fetch", "--broker", "--schedule", "xai")


class ProspectiveRefusal(ResearchError):
    """The captures do not justify a prospective research record."""


@dataclass(frozen=True, slots=True)
class CapturedClose:
    ticker: str
    session: date
    price: Decimal
    available_at: datetime
    exchange_close_at: datetime
    provider_published_at: datetime | None
    retrieved_at: datetime
    availability_basis: str
    source: str
    raw_sha256: str
    http_date: datetime | None = None

    def as_log(self) -> dict[str, str | None]:
        return {
            "availability_basis": self.availability_basis,
            "available_at": self.available_at.isoformat(),
            "exchange_close_at": self.exchange_close_at.isoformat(),
            "instrument_id": self.ticker,
            "price_gbp": format(self.price, "f"),
            "http_date": None if self.http_date is None else self.http_date.isoformat(),
            "provider_published_at": None if self.provider_published_at is None else self.provider_published_at.isoformat(),
            "raw_sha256": self.raw_sha256,
            "retrieved_at": self.retrieved_at.isoformat(),
            "session": self.session.isoformat(),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class SoniaCapture:
    rows: tuple[tuple[date, Decimal], ...]
    retrieved_at: datetime
    raw_sha256: str
    http_date: datetime | None = None

    def as_log(self) -> dict[str, str | None]:
        return {
            "http_date": None if self.http_date is None else self.http_date.isoformat(),
            "raw_sha256": self.raw_sha256,
            "retrieved_at": self.retrieved_at.isoformat(),
            "rows": str(len(self.rows)),
            "rule": "IADB by 10:00 London on the XLON day after the 09:00 distributor publication",
        }


def refuse_provider_request(argv: Sequence[str]) -> None:
    """This mode cannot reach a model, a download, a broker, or a schedule."""
    for item in argv:
        lowered = item.lower()
        if item in PROVIDER_TOKENS or lowered.startswith("--provider=") or "xai" in lowered or "broker" in lowered:
            raise ResearchIsolationError("prospective research does not call a provider, fetch, broker, or schedule")


def run_prospective(
    *,
    root: Path,
    log_path: Path,
    cache: Path,
    session: date,
    inception_session: date,
    as_of: datetime,
    expect_hash: str,
    expect_records: int,
    report: Path | None = None,
    clock_mode: str = "prospective",
) -> dict[str, object]:
    """Value one session into an isolated mock log. Refusals write nothing."""
    if as_of.tzinfo is None:
        raise ProspectiveRefusal("as-of needs a timezone")
    clock = as_of.astimezone(UTC)
    if os.environ.get("STERLING_EVIDENCE_LANE", "").strip() != "mock":
        raise ProspectiveRefusal("prospective research requires STERLING_EVIDENCE_LANE=mock")
    _assert_store(root, log_path)
    with _StoreLock(root):
        _assert_store(root, log_path)
        if _torn(log_path):
            raise ProspectiveRefusal("log tail is torn; restore the last verified manifest before appending")
        count, tip = tip_state(log_path)
        if count != expect_records or tip != expect_hash:
            raise ProspectiveRefusal("log state does not match the expected hash and record count")
        if _session_valued(log_path, session):
            raise ProspectiveRefusal(f"{session.isoformat()} is already valued")
        closes = _session_closes(cache, session, clock)
        sonia = _sonia_capture(cache, clock)
        record_mode = _record_mode(clock_mode, clock, closes, sonia, log_path)
        _assert_inception(log_path, session, inception_session, clock, closes)
        before = log_path.read_bytes() if log_path.is_file() else b""
        fresh = not log_path.exists()
        stamp = _stamp(inception_session, clock, closes, sonia, record_mode)
        if fresh:
            ProposalLog(log_path).create(now=clock - CREATE_OFFSET)
        created = ProposalLog(log_path).created_at()
        if created > clock or (fresh and clock - created > timedelta(seconds=5)):
            raise ProspectiveRefusal("log creation is not the valuation clock")
        ProposalLog(log_path).append_event(
            timestamp_utc=clock - timedelta(seconds=1),
            record_type="snapshot",
            fields={
                "as_of": clock.isoformat(),
                "day_count": "ACT/365",
                "inception_clock": clock.isoformat(),
                "inception_session": inception_session.isoformat(),
                "mode": record_mode,
                "not_official_evidence": "true",
                "observations": [item.as_log() for item in closes],
                "price_policy": "session close required; an older close is not substituted",
                "session": session.isoformat(),
                "sonia_capture": sonia.as_log(),
                "sonia_policy": "missing print is interest_pending, not zero and not a guessed rate",
            },
        )
        history = {item.ticker: ((item.session, item.price),) for item in closes}
        result = run_daily(
            log_path=log_path,
            session=session,
            opens={},
            close_history=history,
            sonia=sonia.rows,
            recorded_at=clock,
            open_source="captured-research",
            data_label=record_mode,
            record_stamp=stamp,
        )
        _assert_mode_intact(log_path, record_mode)
        after = log_path.read_bytes()
        if not after.startswith(before):
            raise ProspectiveRefusal("append changed earlier log bytes")
        manifest = _publish_manifest(root, log_path)
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps({"daily": result, "manifest_sha256": manifest, "mode": record_mode}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {
        "manifest_sha256": manifest,
        "mode": record_mode,
        "status": result["status"],
        "nav_gbp": result.get("nav_gbp", ""),
        "session": session.isoformat(),
    }


def recover_torn_tail(root: Path, log_path: Path) -> str:
    """Restore a torn tail from the longest verified prefix. A sound log is left unchanged."""
    _assert_store(root, log_path)
    with _StoreLock(root):
        if not log_path.is_file():
            raise ProspectiveRefusal("there is no log to recover")
        raw = log_path.read_bytes()
        if not _torn_bytes(raw):
            ProposalLog(log_path).verify()
            return "already_verified"
        prefix = _intact_prefix(raw)
        match = _matching_verified(root, prefix)
        if match is None:
            raise ProspectiveRefusal("no verified manifest matches the intact prefix")
        temporary = log_path.with_suffix(".restore")
        temporary.write_bytes(prefix)
        os.replace(temporary, log_path)
        ProposalLog(log_path).verify()
        return "restored"


def tip_state(path: Path) -> tuple[int, str]:
    if not path.is_file():
        return 0, GENESIS
    log = ProposalLog(path)
    count = log.verify()
    tip = log.records()[-1].get("record_hash")
    if not isinstance(tip, str):
        raise ProspectiveRefusal("log tip hash is missing")
    return count, tip


def _system_now() -> datetime:
    return datetime.now(UTC)


def _record_mode(
    clock_mode: str,
    clock: datetime,
    closes: Sequence[CapturedClose],
    sonia: SoniaCapture,
    log_path: Path,
) -> str:
    """Fixture replays never receive the prospective label. A live clock can."""
    existing = _existing_mode(log_path)
    if clock_mode == "offline-fixture":
        if existing == MODE_PROSPECTIVE:
            raise ProspectiveRefusal("a prospective log cannot be relabelled as an offline fixture")
        return MODE_FIXTURE
    if clock_mode != "prospective":
        raise ProspectiveRefusal("clock mode must be prospective or offline-fixture")
    if existing == MODE_FIXTURE:
        raise ProspectiveRefusal("an offline fixture log cannot be relabelled as prospective")
    if existing == "retrospective-demonstration":
        raise ProspectiveRefusal("a retrospective log cannot be relabelled as prospective")
    _require_live_clock(clock, closes, sonia)
    return MODE_PROSPECTIVE


def _require_live_clock(clock: datetime, closes: Sequence[CapturedClose], sonia: SoniaCapture) -> None:
    """``--as-of`` must match the system clock and the capture HTTP Date.

    The HTTP Date is the provider clock stored with the raw response. It is
    not a new download. If that header is missing, or it disagrees with the
    system clock by more than five minutes, no prospective record is written.
    """
    system = _system_now()
    observed: list[datetime] = []
    for item in closes:
        if item.http_date is None:
            raise ProspectiveRefusal(f"{item.ticker} has no HTTP Date; refusing a prospective record")
        observed.append(item.http_date)
    if sonia.http_date is None:
        raise ProspectiveRefusal("SONIA capture has no HTTP Date; refusing a prospective record")
    observed.append(sonia.http_date)
    for moment in (system, *observed):
        if abs(moment - clock) > CLOCK_TOLERANCE or abs(moment - system) > CLOCK_TOLERANCE:
            raise ProspectiveRefusal("as-of is outside the system clock and the capture HTTP Date")
    if clock > system:
        raise ProspectiveRefusal("as-of is after the system clock")


def _existing_mode(path: Path) -> str:
    if not path.is_file():
        return ""
    for row in ProposalLog(path).records():
        if row.get("record_type") != "snapshot":
            continue
        mode = row.get("mode")
        if isinstance(mode, str):
            return mode
        if row.get("label") == "retrospective-demonstration":
            return "retrospective-demonstration"
    return ""


def _assert_mode_intact(path: Path, record_mode: str) -> None:
    for row in ProposalLog(path).records():
        mode = row.get("mode")
        if record_mode == MODE_FIXTURE and mode == MODE_PROSPECTIVE:
            raise ProspectiveRefusal("offline fixture wrote a prospective record")
        if isinstance(mode, str) and mode not in {record_mode, ""}:
            raise ProspectiveRefusal("log mixes research modes")


def _stamp(
    inception: date,
    clock: datetime,
    closes: Sequence[CapturedClose],
    sonia: SoniaCapture,
    record_mode: str,
) -> dict[str, str]:
    body = {
        "closes": [item.as_log() for item in closes],
        "inception_session": inception.isoformat(),
        "sonia": sonia.as_log(),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {
        "inception_clock": clock.isoformat(),
        "inception_session": inception.isoformat(),
        "mode": record_mode,
        "not_official_evidence": "true",
        "provenance_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _assert_inception(
    log_path: Path,
    session: date,
    inception_session: date,
    clock: datetime,
    closes: Sequence[CapturedClose],
) -> None:
    if session > clock.astimezone(LONDON).date():
        raise ProspectiveRefusal("valuation session is after the clock")
    observed = {item.session for item in closes}
    if session not in observed:
        raise ProspectiveRefusal("valuation session has no captured close")
    if not log_path.exists():
        if inception_session != session:
            raise ProspectiveRefusal("the first session is the inception session")
        return
    recorded = _recorded_inception(log_path)
    if recorded != inception_session:
        raise ProspectiveRefusal("inception session does not match the log")
    if session < recorded:
        raise ProspectiveRefusal("session precedes the recorded inception")


def _recorded_inception(path: Path) -> date:
    for row in ProposalLog(path).records():
        if row.get("record_type") == "snapshot" and isinstance(row.get("inception_session"), str):
            return date.fromisoformat(str(row["inception_session"]))
    raise ProspectiveRefusal("log has no inception session")


def _session_valued(path: Path, session: date) -> bool:
    if not path.is_file():
        return False
    token = session.isoformat()
    return any(row.get("record_type") == "valuation" and row.get("session") == token for row in ProposalLog(path).records())


def _session_closes(cache: Path, session: date, clock: datetime) -> tuple[CapturedClose, ...]:
    chosen: list[CapturedClose] = []
    for ticker in FUNDS:
        path = cache / f"{ticker}.csv"
        if not path.is_file():
            raise ProspectiveRefusal(f"{ticker} has no capture file")
        rows = _read_closes(path, ticker, cache)
        future = [item for item in rows if item.session > session]
        if future:
            raise ProspectiveRefusal(f"{ticker} has an observation after the valuation session")
        matched = [item for item in rows if item.session == session]
        if not matched:
            raise ProspectiveRefusal(f"{ticker} has no close for {session.isoformat()}; an older close is not substituted")
        for item in matched:
            _check_close_clock(item, clock)
        chosen.append(max(matched, key=lambda item: item.retrieved_at))
    return tuple(chosen)


def _check_close_clock(item: CapturedClose, clock: datetime) -> None:
    if item.retrieved_at > clock:
        raise ProspectiveRefusal(f"{item.ticker} was retrieved after the valuation clock")
    if item.available_at > clock:
        raise ProspectiveRefusal(f"{item.ticker} is not available at the valuation clock")
    if item.retrieved_at + CAPTURE_SKEW < item.available_at:
        raise ProspectiveRefusal(f"{item.ticker} was retrieved materially before it was available")


def _read_closes(path: Path, ticker: str, cache: Path) -> list[CapturedClose]:
    found: list[CapturedClose] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            found.append(_one_close(ticker, record, cache))
    if not found:
        raise ProspectiveRefusal(f"{ticker} capture file has no rows")
    return found


def _one_close(ticker: str, record: Mapping[str, str], cache: Path) -> CapturedClose:
    session_text = (record.get("date") or "").strip()
    if not session_text:
        raise ProspectiveRefusal(f"{ticker} row has no session date")
    session = date.fromisoformat(session_text[:10])
    basis = (record.get("availability_basis") or "").strip()
    published = (record.get("provider_published_at") or record.get("published_at") or "").strip()
    retrieved_text = (record.get("retrieved_at") or "").strip()
    if not retrieved_text:
        raise ProspectiveRefusal(f"{ticker} row has no retrieved_at")
    raw_hash = (record.get("raw_sha256") or "").strip()
    if len(raw_hash) != 64:
        raise ProspectiveRefusal(f"{ticker} row has no raw response hash")
    _require_raw(cache, raw_hash)
    retrieved = _aware(retrieved_text, f"{ticker} retrieved_at")
    if basis == BASIS_ASSUMED:
        available = assumed_available_at(session)
        provider_time = _aware(published, f"{ticker} provider_published_at") if published else None
        basis_name = BASIS_ASSUMED
    elif basis in {"", BASIS_VERIFIED} and published:
        provider_time = _aware(published, f"{ticker} published_at")
        available = provider_time
        basis_name = BASIS_VERIFIED
    else:
        raise ProspectiveRefusal(f"{ticker} needs a verified publication time or availability_basis=assumed-next-session")
    close_text = (record.get("exchange_close_at") or "").strip()
    close_at = _aware(close_text, f"{ticker} exchange_close_at") if close_text else exchange_close_at(session)
    http_text = (record.get("http_date") or "").strip()
    return CapturedClose(
        ticker=ticker,
        session=session,
        price=Decimal((record.get("close") or "").strip()),
        available_at=available,
        exchange_close_at=close_at,
        provider_published_at=provider_time,
        retrieved_at=retrieved,
        availability_basis=basis_name,
        source=(record.get("source") or "").strip() or "local-capture",
        raw_sha256=raw_hash,
        http_date=_aware(http_text, f"{ticker} http_date") if http_text else None,
    )


def _sonia_capture(cache: Path, clock: datetime) -> SoniaCapture:
    path = cache / "boe_sonia.csv"
    meta_path = cache / "sonia_capture.json"
    if not path.is_file() or not meta_path.is_file():
        raise ProspectiveRefusal("SONIA capture is missing its file or retrieval record")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ProspectiveRefusal("SONIA retrieval record is not an object")
    retrieved_text = meta.get("retrieved_at")
    raw_hash = meta.get("raw_sha256")
    if not isinstance(retrieved_text, str) or not isinstance(raw_hash, str):
        raise ProspectiveRefusal("SONIA retrieval record needs retrieved_at and raw_sha256")
    if raw_hash != digest:
        raise ProspectiveRefusal("SONIA file hash does not match the retrieval record")
    _require_raw(cache, raw_hash)
    retrieved = _aware(retrieved_text, "SONIA retrieved_at")
    http_value = meta.get("http_date")
    http_date = _aware(http_value, "SONIA http_date") if isinstance(http_value, str) and http_value.strip() else None
    if retrieved > clock:
        raise ProspectiveRefusal("SONIA file was retrieved after the valuation clock")
    rows = parse_sonia_csv(raw.decode("utf-8"))
    for reference, _rate in rows:
        published = sonia_available_at(reference).astimezone(UTC)
        if retrieved + CAPTURE_SKEW < published:
            raise ProspectiveRefusal(f"SONIA {reference.isoformat()} was retrieved before its publication")
    return SoniaCapture(rows=rows, retrieved_at=retrieved, raw_sha256=digest, http_date=http_date)


def _require_raw(cache: Path, digest: str) -> None:
    path = cache / "raw" / digest
    if not path.is_file():
        raise ProspectiveRefusal(f"raw provider response {digest} is missing")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ProspectiveRefusal(f"raw provider response {digest} does not match its name")


def _assert_store(root: Path, log_path: Path) -> None:
    if not root.is_absolute() or not log_path.is_absolute():
        raise ProspectiveRefusal("research root and log must be absolute paths")
    resolved_root = root.resolve()
    resolved_log = log_path.resolve()
    if not _inside(resolved_log, resolved_root):
        raise ProspectiveRefusal("log is outside the research root")
    if classify_log(resolved_log) != "mock":
        raise ProspectiveRefusal("prospective research writes only a mock-lane log")
    try:
        assert_lane(resolved_log, "mock")
    except RuntimeError as exc:
        raise ProspectiveRefusal(str(exc)) from exc
    for path in (resolved_root, resolved_log):
        if _inside(path, APP_ROOT):
            raise ProspectiveRefusal("research store must be outside this application repository")
        if EVIDENCE_REMOTE in path.as_posix():
            raise ProspectiveRefusal("research store must not be the official evidence repository")
        if _has_evidence_remote(path):
            raise ProspectiveRefusal("research store is inside a clone of the official evidence repository")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _has_evidence_remote(path: Path) -> bool:
    for parent in (path, *path.parents):
        git = parent / ".git"
        config = git / "config" if git.is_dir() else None
        if git.is_file():
            pointer = git.read_text(encoding="utf-8", errors="replace")
            for line in pointer.splitlines():
                if line.startswith("gitdir:"):
                    config = Path(line.split(":", 1)[1].strip()) / "config"
        if config is not None and config.is_file() and EVIDENCE_REMOTE in config.read_text(encoding="utf-8", errors="replace"):
            return True
    return False


def _publish_manifest(root: Path, log_path: Path) -> str:
    raw = log_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    verified = root / "verified" / f"{digest}.jsonl"
    verified.parent.mkdir(parents=True, exist_ok=True)
    if verified.exists() and verified.read_bytes() != raw:
        raise ProspectiveRefusal("verified copy already exists with different bytes")
    if not verified.exists():
        verified.write_bytes(raw)
    manifest = root / "manifests" / f"{digest}.sha256"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    body = f"{digest}  {MOCK_SUFFIX}\n{digest}  verified/{digest}.jsonl\n"
    manifest.write_text(body, encoding="utf-8")
    (root / "manifests" / "LATEST").write_text(digest + "\n", encoding="utf-8")
    if hashlib.sha256(log_path.read_bytes()).hexdigest() != digest or verified.read_bytes() != raw:
        raise ProspectiveRefusal("manifest verification failed after export")
    ProposalLog(log_path).verify()
    return digest


def _torn(path: Path) -> bool:
    if not path.is_file():
        return False
    return _torn_bytes(path.read_bytes())


def _torn_bytes(raw: bytes) -> bool:
    if not raw:
        return False
    if not raw.endswith(b"\n"):
        return True
    last = raw.splitlines()[-1]
    try:
        json.loads(last)
    except json.JSONDecodeError:
        return True
    return False


def _intact_prefix(raw: bytes) -> bytes:
    if not raw.endswith(b"\n"):
        head, separator, _tail = raw.rpartition(b"\n")
        if not separator:
            raise ProspectiveRefusal("log has no intact record to restore")
        return head + separator
    lines = raw.splitlines(keepends=True)
    prefix = b"".join(lines[:-1])
    if not prefix:
        raise ProspectiveRefusal("log has no intact record to restore")
    return prefix


def _matching_verified(root: Path, prefix: bytes) -> Path | None:
    directory = root / "verified"
    if not directory.is_dir():
        return None
    matches = [path for path in directory.glob("*.jsonl") if path.read_bytes() == prefix]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_size)


def _aware(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProspectiveRefusal(f"{label} is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise ProspectiveRefusal(f"{label} needs a timezone")
    return parsed.astimezone(UTC)


class _StoreLock:
    """One exclusive lock for this machine. It does not bind any other machine."""

    def __init__(self, root: Path) -> None:
        self.path = root / ".research.lock"
        self.handle: TextIO | None = None

    def __enter__(self) -> _StoreLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            _lock_handle(handle)
        except Exception:
            handle.close()
            raise
        self.handle = handle
        return self

    def __exit__(self, *_exc: object) -> None:
        handle = self.handle
        self.handle = None
        if handle is not None:
            handle.close()


def _lock_handle(handle: TextIO) -> None:
    fileno = handle.fileno()
    if os.name == "nt":
        import msvcrt

        windows: Any = msvcrt
        try:
            windows.locking(fileno, windows.LK_NBLCK, 1)
        except OSError as exc:
            raise ProspectiveRefusal("research lock is held on this machine") from exc
        return
    if os.name != "posix":
        raise ProspectiveRefusal("no exclusive lock on this platform; refusing to append")
    import fcntl

    try:
        fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise ProspectiveRefusal("research lock is held on this machine") from exc
