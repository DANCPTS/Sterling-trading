"""Download the free FX cache. Not imported by the simulator.

Writes Bank of England spots, SONIA, Bank Rate, and BIS monthly policy rates
into ``.research-cache/fx/raw/``. That directory is gitignored. No API key.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

USER_AGENT = "SterlingResearch/1.0 (DANCPTS/Sterling-trading; public research; no-account)"
DESTINATION = Path(".research-cache/fx/raw")
BOE = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
BIS = (
    "https://stats.bis.org/api/v1/data/WS_CBPOL/M.US+XM+JP+CH+AU+CA+NZ+SE+NO+GB/all?startPeriod=1998-01&detail=dataonly&format=csv"
)


def _get(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        path.write_bytes(response.read())
    if path.stat().st_size < 1000 or path.read_text(encoding="utf-8", errors="replace").lstrip().startswith("<"):
        raise SystemExit(f"{path.name} does not look like a CSV")


def main() -> int:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    spots = (
        f"{BOE}?csv.x=yes&Datefrom=01/Jan/1999&Dateto=now"
        "&SeriesCodes=XUDLUSS,XUDLERS,XUDLJYS,XUDLSFS,XUDLADS,XUDLCDS,XUDLNDS,XUDLSKS,XUDLNKS"
        "&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"
    )
    rates = f"{BOE}?csv.x=yes&Datefrom=01/Jan/1997&Dateto=now&SeriesCodes=IUDSOIA,IUDBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"
    _get(spots, DESTINATION / "boe_spot_rates.csv")
    _get(rates, DESTINATION / "boe_rates.csv")
    _get(BIS, DESTINATION / "bis_cbpol.csv")
    print(f"wrote {DESTINATION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
