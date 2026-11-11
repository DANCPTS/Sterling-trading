"""Which evidence file a lane is allowed to append to.

Mock runs write the mock paper log only. They cannot append the v1 forward
log or the genuine paper-v2 log. The forward lane cannot append a mock log.
"""

from __future__ import annotations

from pathlib import Path

_MOCK = (
    "evidence/invest-v2/mock/forward.jsonl",
    "evidence/mock/forward.jsonl",
)
_FORWARD = (
    "evidence/invest-v2/forward.jsonl",
    "evidence/forward.jsonl",
)


def classify_log(path: Path) -> str:
    """Return ``mock``, ``forward``, or ``""`` when the path is not an evidence log."""
    text = path.as_posix()
    for marker in _MOCK:
        if text.endswith(marker):
            return "mock"
    for marker in _FORWARD:
        if text.endswith(marker):
            return "forward"
    return ""


def assert_lane(path: Path, lane: str) -> None:
    """Raise when ``path`` is not the log for ``lane``."""
    kind = classify_log(path)
    if lane == "mock" and kind != "mock":
        raise RuntimeError("mock lane cannot write a genuine evidence log")
    if lane == "forward" and kind != "forward":
        raise RuntimeError("forward lane cannot write the mock evidence log")
    if lane not in {"mock", "forward"}:
        raise RuntimeError("lane must be mock or forward")
