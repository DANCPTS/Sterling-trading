"""Real Linux UID denials. Root running this file is not the evidence.

The evidence is a subprocess that has dropped to the AI uid and is denied
the journal, the owner socket, and raw SQL. Same-uid access is not claimed
to be isolation. Root can still read the files; that is recorded, not hidden.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from tests.gate2.support import MOMENT, anchor, fresh_book

from sterling.gate2.store import open_store, provision_store, write_owner_anchor

pytestmark = pytest.mark.privileged

_NAMES = ("sterling-g2-risk", "sterling-g2-ai", "sterling-g2-owner", "sterling-g2-ingress")


def test_ai_uid_cannot_read_journal_or_call_owner_socket(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.fail("BLOCKED: mandatory UID suite was not started as root; this is not a pass")
    accounts = _accounts()
    # pytest's private tmp is mode 0700 root, which the risk uid cannot traverse.
    base = Path("/tmp/sterling-g2-identity")
    if base.exists():
        subprocess.call(["rm", "-rf", str(base)])
    base.mkdir(mode=0o755)
    os.chmod(base, 0o755)
    root = base / "journal"
    store = provision_store(
        root,
        account_id="sim-primary",
        calendar_version="fixture-cal-v1",
        risk_uid=accounts["sterling-g2-risk"].pw_uid,
        owner_uid=accounts["sterling-g2-owner"].pw_uid,
        ingress_uid=accounts["sterling-g2-ingress"].pw_uid,
        ai_uid=accounts["sterling-g2-ai"].pw_uid,
        os_identity_separated=True,
        synthetic_fixture_scope=True,
        created_at="2026-06-01T08:00:00+00:00",
    )
    book = fresh_book(base / "book.jsonl")
    anchor(store, book, base / "book.jsonl")
    checkpoint = store._read_head_checkpoint()
    anchor_path = Path(accounts["sterling-g2-owner"].pw_dir) / "sterling-anchor.json"
    write_owner_anchor(anchor_path, checkpoint)
    os.chown(anchor_path, accounts["sterling-g2-owner"].pw_uid, accounts["sterling-g2-owner"].pw_gid)
    os.chmod(anchor_path, 0o640)
    os.chmod(Path(accounts["sterling-g2-owner"].pw_dir), 0o750)
    journal = store.root
    store.close()
    os.chown(journal, accounts["sterling-g2-risk"].pw_uid, accounts["sterling-g2-risk"].pw_gid)
    for path in journal.rglob("*"):
        os.chown(path, accounts["sterling-g2-risk"].pw_uid, accounts["sterling-g2-risk"].pw_gid)
        if path.is_dir():
            os.chmod(path, 0o700)
        else:
            os.chmod(path, 0o600)
    os.chmod(journal, 0o700)
    sockets = base / "sockets"
    sockets.mkdir()
    os.chown(sockets, accounts["sterling-g2-risk"].pw_uid, accounts["sterling-g2-risk"].pw_gid)
    os.chmod(sockets, 0o755)
    stop = base / "stop"
    service = subprocess.Popen(
        [
            "runuser",
            "-u",
            "sterling-g2-risk",
            "--",
            sys.executable,
            "-m",
            "sterling.gate2",
            "--root",
            str(journal),
            "--proposal-socket",
            str(sockets / "proposal.sock"),
            "--owner-socket",
            str(sockets / "owner.sock"),
            "--ingress-socket",
            str(sockets / "ingress.sock"),
            "--stop-file",
            str(stop),
            "--owner-anchor",
            str(anchor_path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        try:
            _wait_socket(sockets / "proposal.sock")
        except AssertionError:
            service.kill()
            out, err = service.communicate(timeout=5)
            raise AssertionError(err or out) from None
        report = _as_user(
            "sterling-g2-ai",
            base,
            {
                "database": str(journal / "journal.sqlite"),
                "wal": str(journal / "journal.sqlite-wal"),
                "shm": str(journal / "journal.sqlite-shm"),
                "checkpoint": str(journal / "checkpoint.json"),
                "policy": str(journal / "policy-pin.json"),
                "backup_dir": str(journal / "backups"),
                "owner_anchor": str(anchor_path),
                "proposal": str(sockets / "proposal.sock"),
                "owner": str(sockets / "owner.sock"),
                "ingress": str(sockets / "ingress.sock"),
                "clock": MOMENT.isoformat(),
            },
        )
        for key in ("database", "wal", "shm", "checkpoint", "policy", "backup_dir", "owner_anchor", "owner", "ingress"):
            assert report[key]["denied"] is True, report[key]
        assert "OPERATION_DENIED" in report["proposal_sql"]["reasons"]
        assert "PROPOSAL_CHANNEL_DENIED" in report["proposal_reset"]["reasons"]
        assert report["forged"]["execution_permission_issued"] is False
        assert "FORGED_AUTHORITY" in report["forged"]["reasons"]
        # Root can read the database. That is outside the threat boundary.
        assert os.access(journal / "journal.sqlite", os.R_OK)
        opened = open_store(journal, owner_anchor=anchor_path)
        assert opened.identity.os_identity_separated is True
        opened.close()
    finally:
        stop.write_text("stop", encoding="utf-8")
        service.wait(timeout=10)


def _accounts() -> dict[str, pwd.struct_passwd]:
    for name in _NAMES:
        try:
            pwd.getpwnam(name)
        except KeyError:
            subprocess.check_call(["useradd", "--system", "--create-home", "--user-group", name])
    risk = pwd.getpwnam("sterling-g2-risk")
    for name in ("sterling-g2-ai", "sterling-g2-owner", "sterling-g2-ingress"):
        group = pwd.getpwnam(name).pw_name
        subprocess.check_call(["usermod", "-aG", group, risk.pw_name])
    return {name: pwd.getpwnam(name) for name in _NAMES}


def _wait_socket(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        import time

        time.sleep(0.05)
    raise AssertionError("proposal socket was not created")


def _as_user(name: str, root: Path, payload: dict[str, str]) -> dict[str, object]:
    script = root / "probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, os, socket, sys
            spec = json.loads(sys.argv[1])
            report = {}
            def probe(label, path):
                try:
                    fd = os.open(path, os.O_RDWR)
                    os.close(fd)
                    report[label] = {"denied": False, "error": None}
                except FileNotFoundError:
                    parent = os.path.dirname(path)
                    try:
                        fd = os.open(os.path.join(parent, "ai-probe-" + label), os.O_CREAT | os.O_WRONLY, 0o600)
                        os.close(fd)
                        report[label] = {"denied": False, "error": "created"}
                    except OSError as exc:
                        report[label] = {"denied": True, "error": exc.errno}
                except OSError as exc:
                    report[label] = {"denied": True, "error": exc.errno}
            for label in ("database", "wal", "shm", "checkpoint", "policy", "backup_dir", "owner_anchor"):
                probe(label, spec[label])
            def send(path, message):
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(path)
                except OSError as exc:
                    return {"denied": True, "error": exc.errno}
                sock.sendall(json.dumps(message).encode() + b"\\n")
                data = sock.recv(65536)
                body = json.loads(data.decode())
                body["denied"] = False
                return body
            report["proposal_sql"] = send(spec["proposal"], {"op": "sql", "sql": "UPDATE meta SET synthetic_fixture_scope = 1"})
            report["proposal_reset"] = send(spec["proposal"], {"op": "reset", "reason": "no"})
            report["owner"] = send(spec["owner"], {"op": "status"})
            report["ingress"] = send(spec["ingress"], {"op": "status"})
            report["forged"] = send(spec["proposal"], {
                "op": "propose",
                "request_id": "forge",
                "principal": "ai",
                "account_id": "sim-primary",
                "environment": "simulated",
                "claimed_owner": True,
                "claimed_approved": True,
                "signature": "forged",
                "policy_override": {"per_trade": "999"},
                "clock": {"as_of": spec["clock"], "monotonic_ns": 1},
                "proposal": {
                    "proposal_id": "P", "episode_id": "E", "instrument_id": "SYNTH-EQ",
                    "instrument_currency": "GBP", "product": "LONG_EQUITY", "side": "BUY",
                    "borrowing": False, "leverage": "1", "opens_new_episode": True,
                    "quantity": "1", "quantity_precision": 0, "entry_price": "10",
                    "stop_price": "9", "stop_protection": "FULL", "covered_quantity": "1",
                    "fx_rate_to_gbp": "1", "conservative_price": "10",
                    "conservative_fx_rate_to_gbp": "1", "expected_entry_cost_gbp": "0",
                    "expected_exit_cost_gbp": "0", "adverse_allowance_gbp": "0",
                },
            })
            print(json.dumps(report))
            """
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["runuser", "-u", name, "--", sys.executable, str(script), json.dumps(payload)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    body = json.loads(completed.stdout)
    assert isinstance(body, dict)
    return body
