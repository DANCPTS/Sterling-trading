"""Ping Healthchecks without ever printing the URL."""

from __future__ import annotations

import os
import sys
import urllib.request
from collections.abc import Callable

ENV_URL = "HEALTHCHECKS_PING_URL"


def ping(url: str, *, failed: bool, opener: Callable[[str], object] | None = None) -> None:
    if not (url.startswith("https://") or url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")):
        raise RuntimeError("healthchecks URL is missing")
    target = url.rstrip("/") + ("/fail" if failed else "")
    send = opener or _open
    send(target)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    failed = "--fail" in args
    url = os.environ.get(ENV_URL, "")
    try:
        ping(url, failed=failed)
    except Exception:
        print("healthchecks ping failed", file=sys.stderr)
        return 1
    print("healthchecks ping fail" if failed else "healthchecks ping ok")
    return 0


def _open(url: str) -> None:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=20):
        return


if __name__ == "__main__":
    raise SystemExit(main())
