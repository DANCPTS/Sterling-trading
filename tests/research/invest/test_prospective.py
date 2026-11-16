"""Prospective research mode. Fixtures only. No network and no real record."""

from __future__ import annotations

import hashlib
import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest
from scripts.run_prospective_research_day import main

from sterling.research.fx.grok_forward import GENESIS, ProposalLog
from sterling.research.invest.cash_rate import retail_annual_decimal
from sterling.research.invest.prospective import ProspectiveRefusal, _lock_handle
from sterling.research.invest.schema import FUNDS
from sterling.research.money import quantize_money

pytestmark = pytest.mark.filterwarnings("ignore")

FRIDAY = "2026-11-13T16:30:00+00:00"
MONDAY = "2026-11-16T16:30:00+00:00"


@pytest.fixture
def lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STERLING_SYSTEM", "sterling-invest")
    monkeypatch.setenv("STERLING_EXPERIMENT", "paper-v2")
    monkeypatch.setenv("STERLING_EVIDENCE_LANE", "mock")


def _store(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "research-store"
    log = root / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    return root, log


def _raw(cache: Path, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    directory = cache / "raw"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / digest).write_bytes(payload)
    return digest


def _cache(
    tmp_path: Path,
    *,
    session: str,
    published: str,
    retrieved: str,
    sonia: str,
    sonia_retrieved: str,
    name: str,
    http_date: str = "",
) -> Path:
    cache = tmp_path / name
    cache.mkdir()
    price_hash = _raw(cache, f"prices-{session}-{retrieved}".encode())
    for name in FUNDS:
        (cache / f"{name}.csv").write_text(
            "date,close,availability_basis,published_at,retrieved_at,source,raw_sha256,http_date\n"
            f"{session},100.00,source-verified,{published},{retrieved},fixture,{price_hash},{http_date}\n",
            encoding="utf-8",
        )
    sonia_bytes = f"DATE,IUDSOIA\n{sonia}\n".encode() if sonia else b"DATE,IUDSOIA\n"
    (cache / "boe_sonia.csv").write_bytes(sonia_bytes)
    sonia_hash = _raw(cache, sonia_bytes)
    (cache / "sonia_capture.json").write_text(
        json.dumps({"http_date": http_date, "raw_sha256": sonia_hash, "retrieved_at": sonia_retrieved}) + "\n",
        encoding="utf-8",
    )
    return cache


def _friday(tmp_path: Path) -> Path:
    return _cache(
        tmp_path,
        session="2026-11-13",
        published="2026-11-13T16:30:00+00:00",
        retrieved="2026-11-13T16:30:00+00:00",
        sonia="11 Nov 2026,3.9845",
        sonia_retrieved="2026-11-13T16:30:00+00:00",
        name="friday",
    )


def _monday(tmp_path: Path) -> Path:
    return _cache(
        tmp_path,
        session="2026-11-16",
        published="2026-11-16T16:20:00+00:00",
        retrieved="2026-11-16T16:25:00+00:00",
        sonia="12 Nov 2026,3.9845",
        sonia_retrieved="2026-11-16T16:25:00+00:00",
        name="monday",
    )


def _run(
    root: Path, log: Path, cache: Path, session: str, inception: str, as_of: str, expect_hash: str, expect_records: int
) -> int:
    return main(
        [
            "--root",
            str(root),
            "--log",
            str(log),
            "--cache",
            str(cache),
            "--session",
            session,
            "--inception-session",
            inception,
            "--as-of",
            as_of,
            "--expect-hash",
            expect_hash,
            "--expect-records",
            str(expect_records),
            "--clock-mode",
            "offline-fixture",
        ]
    )


def test_valid_clock_records_prospective_provenance(lane: None, tmp_path: Path) -> None:
    del lane
    root, log = _store(tmp_path)
    assert _run(root, log, _friday(tmp_path), "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 0
    rows = list(ProposalLog(log).records())
    snapshot = next(row for row in rows if row["record_type"] == "snapshot")
    assert snapshot["mode"] == "offline_fixture"
    assert snapshot["not_official_evidence"] == "true"
    assert snapshot["inception_session"] == "2026-11-13"
    assert "retrospective" not in json.dumps(snapshot)
    observations = snapshot["observations"]
    assert isinstance(observations, list) and len(observations) == 6
    valuation = next(row for row in rows if row["record_type"] == "valuation")
    assert valuation["mode"] == "offline_fixture"
    assert valuation["data_label"] == "offline_fixture"
    assert "prospective_research" not in log.read_text(encoding="utf-8")
    interest = next(row for row in rows if row["record_type"] == "cash_interest")
    rate = retail_annual_decimal(Decimal("3.9845"))
    assert interest["amount_gbp"] == format(quantize_money(Decimal("10000.00") * rate / Decimal(365)), "f")
    assert interest["published_on"] == "2026-11-11"
    assert interest["sonia_percent"] == "3.9845"
    manifest = (root / "manifests" / "LATEST").read_text(encoding="utf-8").strip()
    assert hashlib.sha256(log.read_bytes()).hexdigest() == manifest


def test_weekend_accrues_each_calendar_day_once(lane: None, tmp_path: Path) -> None:
    del lane
    root, log = _store(tmp_path)
    friday = _friday(tmp_path)
    monday = _monday(tmp_path)
    assert _run(root, log, friday, "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 0
    count, tip = _tip(log)
    assert _run(root, log, monday, "2026-11-16", "2026-11-13", MONDAY, tip, count) == 0
    interest = [row for row in ProposalLog(log).records() if row["record_type"] == "cash_interest"]
    sessions = [row["session"] for row in interest]
    assert sessions == ["2026-11-13", "2026-11-14", "2026-11-15", "2026-11-16"]
    assert sessions.count("2026-11-13") == 1
    rate = retail_annual_decimal(Decimal("3.9845"))
    cash = Decimal("10000.95")
    for row, day in zip(interest[1:], ("2026-11-14", "2026-11-15", "2026-11-16"), strict=True):
        amount = quantize_money(cash * rate / Decimal(365))
        assert row["session"] == day
        assert row["amount_gbp"] == format(amount, "f")
        assert row["published_on"] == "2026-11-12"
        cash = quantize_money(cash + amount)
    valuation = [row for row in ProposalLog(log).records() if row["record_type"] == "valuation"][-1]
    assert Decimal(str(valuation["nav_gbp"])) == cash
    count, tip = _tip(log)
    assert _run(root, log, monday, "2026-11-16", "2026-11-13", MONDAY, tip, count) == 2
    assert _tip(log) == (count, tip)


def test_future_and_backdated_captures_write_nothing(lane: None, tmp_path: Path) -> None:
    del lane
    root, log = _store(tmp_path)
    future = _cache(
        tmp_path,
        session="2026-11-13",
        published="2026-11-13T16:30:00+00:00",
        retrieved="2026-11-13T16:31:00+00:00",
        sonia="12 Nov 2026,3.9845",
        sonia_retrieved="2026-11-13T16:30:00+00:00",
        name="future",
    )
    assert _run(root, log, future, "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 2
    assert not log.exists()
    early = _cache(
        tmp_path,
        session="2026-11-13",
        published="2026-11-13T16:30:00+00:00",
        retrieved="2026-11-13T12:00:00+00:00",
        sonia="12 Nov 2026,3.9845",
        sonia_retrieved="2026-11-13T16:30:00+00:00",
        name="early",
    )
    assert _run(root, log, early, "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 2
    assert not log.exists()


def test_missing_price_and_missing_sonia_follow_the_stated_policy(lane: None, tmp_path: Path) -> None:
    del lane
    root, log = _store(tmp_path)
    cache = _friday(tmp_path)
    old = b"xstr-prior-session"
    old_hash = _raw(cache, old)
    (cache / "XSTR.csv").write_text(
        "date,close,availability_basis,published_at,retrieved_at,source,raw_sha256\n"
        f"2026-11-12,100.00,source-verified,2026-11-12T16:30:00+00:00,2026-11-12T16:30:00+00:00,fixture,{old_hash}\n",
        encoding="utf-8",
    )
    assert _run(root, log, cache, "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 2
    assert not log.exists()
    empty = _cache(
        tmp_path,
        session="2026-11-13",
        published="2026-11-13T16:30:00+00:00",
        retrieved="2026-11-13T16:30:00+00:00",
        sonia="",
        sonia_retrieved="2026-11-13T16:30:00+00:00",
        name="empty-sonia",
    )
    assert _run(root, log, empty, "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 0
    rows = list(ProposalLog(log).records())
    pending = next(row for row in rows if row["record_type"] == "interest_pending")
    assert pending["status"] == "pending"
    assert "amount_gbp" not in pending
    valuation = next(row for row in rows if row["record_type"] == "valuation")
    assert valuation["nav_gbp"] == "10000.00"
    assert valuation["cash_rate"]["status"] == "unavailable"


def test_wrong_state_and_official_paths_are_refused(lane: None, tmp_path: Path) -> None:
    del lane
    root, log = _store(tmp_path)
    assert _run(root, log, _friday(tmp_path), "2026-11-13", "2026-11-13", FRIDAY, "f" * 64, 0) == 2
    assert not log.exists()
    inside = Path(__file__).resolve().parents[3] / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    assert main(["--root", str(inside.parent), "--log", str(inside), "--recover"]) == 2
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "config").write_text("url = git@github.com:DANCPTS/sterling-forward-evidence.git\n", encoding="utf-8")
    banned = clone / "evidence" / "invest-v2" / "mock" / "forward.jsonl"
    assert main(["--root", str(clone), "--log", str(banned), "--recover"]) == 2
    assert main(["--provider", "xai", "--root", str(root), "--log", str(log)]) == 2


def test_torn_tail_restores_the_verified_prefix(lane: None, tmp_path: Path) -> None:
    del lane
    root, log = _store(tmp_path)
    assert _run(root, log, _friday(tmp_path), "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 0
    good = log.read_bytes()
    with log.open("ab") as handle:
        handle.write(b'{"record_type":')
    count, tip = _tip_of(good)
    assert _run(root, log, _monday(tmp_path), "2026-11-16", "2026-11-13", MONDAY, tip, count) == 2
    assert main(["--root", str(root), "--log", str(log), "--recover"]) == 0
    assert log.read_bytes() == good
    ProposalLog(log).verify()


def test_lock_contention_and_platforms(lane: None, tmp_path: Path) -> None:
    del lane
    import subprocess
    import sys

    root, log = _store(tmp_path)
    root.mkdir(parents=True)
    lock = root / ".research.lock"
    lock.touch()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time; h=open(sys.argv[1],'a+'); fcntl.flock(h.fileno(), fcntl.LOCK_EX); "
            "print('held', flush=True); time.sleep(30)",
            str(lock),
        ],
        stdout=subprocess.PIPE,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == b"held"
    try:
        assert _run(root, log, _friday(tmp_path), "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 2
    finally:
        holder.kill()
        holder.wait(timeout=5)
    assert not log.exists()


def test_unknown_platform_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sterling.research.invest.prospective.os.name", "java")

    class _Handle:
        def fileno(self) -> int:
            return 0

    with pytest.raises(ProspectiveRefusal, match="no exclusive lock"):
        _lock_handle(_Handle())  # type: ignore[arg-type]


def test_windows_lock_uses_msvcrt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, int]] = []

    class _Msvcrt:
        LK_NBLCK = 2

        @staticmethod
        def locking(fileno: int, mode: int, length: int) -> None:
            calls.append((fileno, mode, length))

    monkeypatch.setitem(__import__("sys").modules, "msvcrt", _Msvcrt)
    monkeypatch.setattr("sterling.research.invest.prospective.os.name", "nt")

    class _Handle:
        def fileno(self) -> int:
            return 7

    _lock_handle(_Handle())  # type: ignore[arg-type]
    assert calls == [(7, 2, 1)]


def test_command_does_not_open_a_socket(lane: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del lane

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    root, log = _store(tmp_path)
    assert _run(root, log, _friday(tmp_path), "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 0


def test_backdated_inception_is_refused(lane: None, tmp_path: Path) -> None:
    del lane
    root, log = _store(tmp_path)
    assert _run(root, log, _friday(tmp_path), "2026-11-13", "2026-11-01", FRIDAY, GENESIS, 0) == 2
    assert not log.exists()


def test_live_clock_can_be_prospective_and_cannot_be_relabelled(
    lane: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del lane
    frozen = "2026-11-18T10:05:00+00:00"
    monkeypatch.setattr(
        "sterling.research.invest.prospective._system_now",
        lambda: __import__("datetime").datetime.fromisoformat(frozen),
    )
    root, log = _store(tmp_path)
    cache = _cache(
        tmp_path,
        session="2026-11-16",
        published=frozen,
        retrieved=frozen,
        sonia="16 Nov 2026,3.9845",
        sonia_retrieved=frozen,
        name="live",
        http_date=frozen,
    )
    assert (
        main(
            [
                "--root",
                str(root),
                "--log",
                str(log),
                "--cache",
                str(cache),
                "--session",
                "2026-11-16",
                "--inception-session",
                "2026-11-16",
                "--as-of",
                frozen,
                "--expect-hash",
                GENESIS,
                "--expect-records",
                "0",
            ]
        )
        == 0
    )
    text = log.read_text(encoding="utf-8")
    assert '"mode":"prospective_research"' in text
    assert '"mode":"offline_fixture"' not in text
    original = log.read_bytes()
    count, tip = _tip(log)
    assert (
        main(
            [
                "--root",
                str(root),
                "--log",
                str(log),
                "--cache",
                str(cache),
                "--session",
                "2026-11-16",
                "--inception-session",
                "2026-11-16",
                "--as-of",
                frozen,
                "--expect-hash",
                tip,
                "--expect-records",
                str(count),
                "--clock-mode",
                "offline-fixture",
            ]
        )
        == 2
    )
    assert log.read_bytes() == original


def test_historical_clock_and_fixture_log_cannot_become_prospective(lane: None, tmp_path: Path) -> None:
    del lane
    root, log = _store(tmp_path)
    friday = _friday(tmp_path)
    assert (
        main(
            [
                "--root",
                str(root),
                "--log",
                str(log),
                "--cache",
                str(friday),
                "--session",
                "2026-11-13",
                "--inception-session",
                "2026-11-13",
                "--as-of",
                FRIDAY,
                "--expect-hash",
                GENESIS,
                "--expect-records",
                "0",
            ]
        )
        == 2
    )
    assert not log.exists()
    assert _run(root, log, friday, "2026-11-13", "2026-11-13", FRIDAY, GENESIS, 0) == 0
    original = log.read_bytes()
    count, tip = _tip(log)
    assert (
        main(
            [
                "--root",
                str(root),
                "--log",
                str(log),
                "--cache",
                str(friday),
                "--session",
                "2026-11-16",
                "--inception-session",
                "2026-11-13",
                "--as-of",
                MONDAY,
                "--expect-hash",
                tip,
                "--expect-records",
                str(count),
            ]
        )
        == 2
    )
    assert log.read_bytes() == original
    from scripts.run_manual_research_day import main as retrospective

    assert retrospective(["--log", str(log), "--cache", str(friday), "--session", "2026-11-16", "--as-of", MONDAY]) == 2
    assert log.read_bytes() == original


def test_capture_stays_offline_until_a_permitted_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.capture_research_inputs import main as capture_main

    destination = tmp_path / "out"
    assert capture_main(["--provider", "synthetic", "--destination", str(destination)]) == 2
    monkeypatch.setenv("STERLING_CAPTURE_APPROVED", "yes")
    assert capture_main(["--provider", "yahoo", "--destination", str(destination)]) == 2
    assert capture_main(["--provider", "boe-iadb", "--destination", str(destination)]) == 2
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    for name in FUNDS:
        (fixture / f"{name}.bin").write_bytes(f"raw-{name}".encode())
    (fixture / "boe_sonia.csv").write_bytes(b"DATE,IUDSOIA\n16 Nov 2026,3.9845\n")

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    moment = "2026-11-17T10:05:00+00:00"
    assert (
        capture_main(
            [
                "--provider",
                "synthetic",
                "--destination",
                str(destination),
                "--fixture-dir",
                str(fixture),
                "--session",
                "2026-11-16",
                "--published-at",
                moment,
                "--retrieved-at",
                moment,
                "--http-date",
                moment,
            ]
        )
        == 0
    )
    assert (destination / "VWRP.csv").is_file()
    assert (destination / "boe_sonia.csv").is_file()
    assert (destination / "sonia_capture.json").is_file()
    raw = list((destination / "raw").iterdir())
    assert raw
    assert hashlib.sha256(raw[0].read_bytes()).hexdigest() == raw[0].name


def _tip(path: Path) -> tuple[int, str]:
    rows = ProposalLog(path).records()
    tip = rows[-1]["record_hash"]
    assert isinstance(tip, str)
    return len(rows), tip


def _tip_of(raw: bytes) -> tuple[int, str]:
    lines = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    tip = lines[-1]["record_hash"]
    assert isinstance(tip, str)
    return len(lines), tip
