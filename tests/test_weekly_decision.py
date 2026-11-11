"""Weekly runner stays offline unless --fetch-free is asked for."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
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


def test_one_source_holds_and_two_sources_can_trade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _script()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    day = datetime.now(UTC).date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    cache = tmp_path / "cache"
    cache.mkdir()
    for ticker in ("VWRP", "CUKX", "IGLT", "AGBP", "SGLN", "XSTR"):
        (cache / f"{ticker}.csv").write_text(f"date,close,quoted_in\n{day.isoformat()},100,GBP\n", encoding="utf-8")
    held = tmp_path / "held.jsonl"
    assert module.main(["--log", str(held), "--cache", str(cache), "--provider", "mock"]) == 0
    held_text = held.read_text(encoding="utf-8")
    assert "missing_or_not_cross_checked" in held_text
    assert "provider_calls=0" in capsys.readouterr().out
    assert '"gate":"hold"' in held_text
    before = held_text.count("week_reserve")
    assert module.main(["--log", str(held), "--cache", str(cache), "--provider", "mock"]) == 0
    assert held.read_text(encoding="utf-8").count("week_reserve") == before
    assert "status=noop" in capsys.readouterr().out

    for ticker, close, unit in (
        ("VWRP", "100", "GBP"),
        ("CUKX", "10000", "GBp"),
        ("IGLT", "100", "GBP"),
        ("AGBP", "100", "GBP"),
        ("SGLN", "10000", "GBp"),
        ("XSTR", "10000", "GBp"),
    ):
        (cache / f"{ticker}.check.csv").write_text(
            f"date,close,quoted_in\n{day.isoformat()},{close if unit == 'GBP' else '100'},GBP\n",
            encoding="utf-8",
        )
        (cache / f"{ticker}.csv").write_text(
            f"date,close,quoted_in\n{day.isoformat()},{close},{unit}\n",
            encoding="utf-8",
        )
    traded = tmp_path / "traded.jsonl"
    assert module.main(["--log", str(traded), "--cache", str(cache), "--provider", "mock"]) == 0
    text = traded.read_text(encoding="utf-8")
    assert '"gate":"accepted"' in text or '"gate":"hold"' in text
    assert "mode" in text and "forward" in text


def test_rejected_xai_payload_is_logged_and_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_KEY, "xai-test-key-not-real")
    module = _script()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    snapshot, _holds, _prints, _opens = module.build_snapshot(
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
    result, usage = module.run_decision(
        log_path=log,
        snapshot=snapshot,
        provider_name="xai",
        enable_xai=True,
        dry_run=False,
        client=client,
    )
    assert result.provider_calls == 1
    assert result.gate != "accepted"
    assert usage is not None
    stored = log.read_text(encoding="utf-8")
    assert "xai-test-key-not-real" not in stored
    assert "unknown_instrument:TSLA" in stored or "malformed_json" in stored
    assert '"disposition":"HOLD"' in stored
    assert '"record_type":"model_usage"' in stored
    assert module.healthcheck(log) >= 2
    again, _second = module.run_decision(
        log_path=log,
        snapshot=snapshot,
        provider_name="xai",
        enable_xai=True,
        dry_run=False,
        client=client,
    )
    assert again.provider_calls == 0
    assert client.calls_sent == 1


def _demo(module: object, tmp_path: Path):
    snapshot, _holds, _prints, _opens = module.build_snapshot(  # type: ignore[attr-defined]
        as_of=datetime(2024, 6, 7, 16, 30, tzinfo=UTC),
        cache=None,
        fetch_free=False,
        demo_offline=True,
    )
    assert snapshot is not None
    return snapshot


def test_inconsistent_hold_is_logged_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    snapshot = _demo(module, tmp_path)
    log = tmp_path / "forward.jsonl"
    calls = {"n": 0}
    stages: list[str] = []

    def before() -> None:
        stages.append("published")
        assert "week_reserve" in log.read_text(encoding="utf-8")

    def provider(_snapshot: object, _prompt: str) -> tuple[str, str]:
        calls["n"] += 1
        assert stages == ["published"]
        return (
            json.dumps(
                {
                    "model_id": "grok-4.3",
                    "proposals": [
                        {
                            "action": "HOLD",
                            "confidence": "0.20",
                            "instrument_id": "VWRP",
                            "reasoning": "keep the current weight",
                            "target_weight": "0.10",
                        }
                    ],
                }
            ),
            "grok-4.3",
        )

    result = module.run_week(
        log_path=log,
        state_path=module.state_file(log),
        snapshot=snapshot,
        provider=provider,
        mode="forward",
        before_call=before,
    )
    assert result.provider_calls == 1
    assert result.gate == "inconsistent_action"
    text = log.read_text(encoding="utf-8")
    assert '"disposition":"HOLD"' in text
    assert "keep the current weight" in text
    assert calls["n"] == 1
    second = module.run_week(
        log_path=log,
        state_path=module.state_file(log),
        snapshot=snapshot,
        provider=provider,
        mode="forward",
        before_call=before,
    )
    assert second.provider_calls == 0
    assert calls["n"] == 1
    assert stages == ["published"]


def test_malformed_reply_is_a_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    snapshot = _demo(module, tmp_path)
    log = tmp_path / "bad.jsonl"

    def provider(_snapshot: object, _prompt: str) -> tuple[str, str]:
        return "not-json", "grok-4.3"

    result = module.run_week(
        log_path=log,
        state_path=module.state_file(log),
        snapshot=snapshot,
        provider=provider,
        mode="forward",
    )
    assert result.gate == "malformed_json"
    text = log.read_text(encoding="utf-8")
    assert '"disposition":"HOLD"' in text
    assert "not-json" in text
    assert module.healthcheck(log) >= 2


def test_crash_after_reserve_blocks_a_second_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    snapshot = _demo(module, tmp_path)
    log = tmp_path / "crash.jsonl"
    saved: list[str] = []
    calls = {"n": 0}

    def before() -> None:
        saved.append(log.read_text(encoding="utf-8"))

    def provider(_snapshot: object, _prompt: str) -> tuple[str, str]:
        calls["n"] += 1
        raise RuntimeError("runner died after the reply")

    with pytest.raises(RuntimeError, match="runner died"):
        module.run_week(
            log_path=log,
            state_path=module.state_file(log),
            snapshot=snapshot,
            provider=provider,
            mode="forward",
            before_call=before,
        )
    assert saved and "week_reserve" in saved[0]
    assert calls["n"] == 1

    def again(_snapshot: object, _prompt: str) -> tuple[str, str]:
        calls["n"] += 1
        return "{}", "grok-4.3"

    second = module.run_week(
        log_path=log,
        state_path=module.state_file(log),
        snapshot=snapshot,
        provider=again,
        mode="forward",
    )
    assert second.provider_calls == 0
    assert calls["n"] == 1
