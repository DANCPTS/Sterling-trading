"""Disk-full commit failure on a real tiny filesystem."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from tests.gate2.support import anchor, clock, fresh_book, new_store, request

from sterling.gate2.admit import admit
from sterling.gate2.models import Outcome

pytestmark = pytest.mark.privileged


def test_full_filesystem_commit_is_not_success(tmp_path: Path) -> None:
    assert tmp_path.exists()
    if os.geteuid() != 0:
        pytest.fail("BLOCKED: a real disk-full filesystem requires root; this is not a pass")
    mount = Path("/tmp/sterling-g2-disk")
    if mount.exists():
        subprocess.call(["umount", "-l", str(mount)])
        subprocess.call(["rm", "-rf", str(mount)])
    mount.mkdir(mode=0o755)
    subprocess.check_call(["mount", "-t", "tmpfs", "-o", "size=8m", "tmpfs", str(mount)])
    try:
        store = new_store(mount)
        book = fresh_book(mount / "book.jsonl")
        anchor(store, book, mount / "book.jsonl")
        filler = mount / "filler"
        handle = filler.open("wb")
        block = b"x" * 65536
        try:
            while True:
                handle.write(block)
                handle.flush()
        except OSError:
            pass
        try:
            while True:
                handle.write(b"x")
                handle.flush()
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass
        import os as _os

        _os.system(f"df -h {mount} >&2")
        decision = admit(store, request("full"), clock())
        assert decision.outcome == Outcome.UNAVAILABLE
        assert decision.execution_permission_issued is False
        assert decision.reservation_id is None
    finally:
        subprocess.call(["umount", "-l", str(mount)])
