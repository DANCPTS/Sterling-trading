"""Weekly runner stays offline unless --fetch-free is asked for."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sterling.xai_client import ENV_KEY, HttpResponse, XaiClient

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2024-06-07T16:30:00+00:00"


def _script():
    spec = importlib.util.spec_from_file_location("run_weekly_decision", ROOT / "scripts" / "run_weekly_decision.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_healthcheck_and_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(module.urllib.request, "urlopen", _boom)
    log = tmp_path / "forward.jsonl"
    assert module.main(["--demo-offline", "--log", str(log), "--as-of", AS_OF]) == 0
    text = log.read_text(encoding="utf-8")
    assert "technical pilot" not in text or "prompt_token_estimate" in text
    assert "prompt_token_estimate" in text
    assert module.main(["--healthcheck", "--log", str(log)]) == 0
    log.write_text(text.replace("log_created", "log_creatad", 1), encoding="utf-8")
    assert module.main(["--healthcheck", "--log", str(log)]) == 1
    assert module.main(["--healthcheck", "--log", str(tmp_path / "missing.jsonl")]) == 1


def test_rejected_xai_payload_is_logged_and_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_KEY, "xai-test-key-not-real")
    module = _script()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    snapshot = module.build_snapshot(
        as_of=datetime(2024, 6, 7, 16, 30, tzinfo=UTC),
        cache=None,
        fetch_free=False,
        demo_offline=True,
    )

    class _Fake:
        def post(self, **kwargs: object) -> HttpResponse:
            del kwargs
            content = json.dumps(
                {
                    "model_id": "grok-4.3",
                    "proposals": [
                        {
                            "action": "BUY",
                            "confidence": "0.2",
                            "instrument_id": "TSLA",
                            "reasoning": "no",
                            "target_weight": "0.10",
                        }
                    ],
                }
            )
            body = json.dumps({"choices": [{"message": {"content": content}}]})
            return HttpResponse(status=200, body=body.encode("utf-8"))

    client = XaiClient(enabled=True, transport=_Fake(), sleeper=lambda _delay: None)  # type: ignore[arg-type]
    log = tmp_path / "forward.jsonl"
    with pytest.raises(module.ResearchError, match="rejected"):
        module.run_decision(
            log_path=log,
            snapshot=snapshot,
            provider_name="xai",
            enable_xai=True,
            dry_run=False,
            client=client,
        )
    stored = log.read_text(encoding="utf-8")
    assert "xai-test-key-not-real" not in stored
    assert "unknown_instrument:TSLA" in stored or "malformed_json" in stored
    assert module.healthcheck(log) >= 2
