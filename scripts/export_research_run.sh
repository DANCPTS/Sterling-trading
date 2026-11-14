#!/usr/bin/env bash
# Copy one mock-lane research log out of the temporary machine.
# Usage: scripts/export_research_run.sh LOG_PATH [EXPORT_NAME]
# The key scan reads XAI_API_KEY from the environment inside Python.
# The value is never placed on a command line.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: scripts/export_research_run.sh LOG_PATH [EXPORT_NAME]" >&2
  exit 2
fi

log=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1")
name=${2:-research_run_v2}
root=$(cd "$(dirname "$0")/.." && pwd)
stage=$(mktemp -d /tmp/research-export-XXXXXX)
trap 'rm -rf "$stage"' EXIT

if [[ ! -f "$log" ]]; then
  echo "research log is missing" >&2
  exit 2
fi

PYTHONPATH="${root}/src${PYTHONPATH:+:$PYTHONPATH}" python3 - "$log" "$stage" << 'PY'
import hashlib
import json
import sys
from pathlib import Path

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.verify import verify_evidence_lane
from sterling.research.invest.verify import verify_invest

log_path = Path(sys.argv[1])
stage = Path(sys.argv[2])
log = ProposalLog(log_path)
count = log.verify()
records = log.records()
verify_invest(records, lane="mock")
verify_evidence_lane(records, lane="mock")
(stage / "forward.jsonl").write_bytes(log_path.read_bytes())
decision = next((row for row in records if row.get("record_type") == "decision"), None)
raw = ""
if isinstance(decision, dict) and isinstance(decision.get("raw_reply"), str):
    raw = decision["raw_reply"]
(stage / "raw_response.txt").write_text(raw, encoding="utf-8")
usage = next((row for row in records if row.get("record_type") == "model_usage" and isinstance(row.get("http_body"), str)), None)
if isinstance(usage, dict):
    (stage / "http_body.json").write_text(str(usage["http_body"]), encoding="utf-8")
lines = [f"records {count}", f"source {log_path}"]
(stage / "export_note.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
manifest = []
for path in sorted(stage.iterdir()):
    if path.name == "MANIFEST.sha256":
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest.append(f"{digest}  {path.name}")
(stage / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8")
print(f"verified {count} records")
PY

python3 - "$stage" << 'PY'
import os
import sys
from pathlib import Path

stage = Path(sys.argv[1])
key = os.environ.get("XAI_API_KEY", "")
forbidden = [b"Authorization", b"Bearer"]
key_bytes = key.encode("utf-8") if key else b""
for path in sorted(stage.iterdir()):
    if not path.is_file():
        continue
    data = path.read_bytes()
    if key_bytes and key_bytes in data:
        print("export aborted: API key text is in the log", file=sys.stderr)
        sys.exit(3)
    if any(needle in data for needle in forbidden):
        print("export aborted: Authorization or Bearer is in the log", file=sys.stderr)
        sys.exit(3)
PY

copy_tree() {
  local dest=$1
  mkdir -p "$dest"
  cp -R "$stage"/. "$dest"/
  echo "exported $dest"
}

copy_tree "/opt/cursor/artifacts/${name}"
if [[ -d /cursor/stores/self ]]; then
  copy_tree "/cursor/stores/self/${name}"
fi
trap - EXIT
rm -rf "$stage"
