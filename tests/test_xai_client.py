"""xAI client against a fake transport. No network."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sterling.research.fx.grok_forward import ProposalLog
from sterling.research.grok.schema import parse_response
from sterling.xai_client import (
    API_PATH,
    API_URL,
    ENV_KEY,
    RETRIES_AFTER_SEND,
    HttpResponse,
    HttpsTransport,
    NotSent,
    XaiClient,
    XaiDisabled,
    XaiDryRun,
    XaiError,
    XaiRefused,
    XaiTimeout,
)

ROOT = Path(__file__).resolve().parents[1]
KEY = "xai-test-key-not-real"


class _Fake:
    def __init__(self, responses: list[HttpResponse] | None = None, error: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
        connect_timeout: float,
        read_timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            {"url": url, "body": body, "headers": dict(headers), "connect_timeout": connect_timeout, "read_timeout": read_timeout}
        )
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def _client(fake: _Fake, **kwargs: object) -> XaiClient:
    values: dict[str, object] = {
        "enabled": True,
        "transport": fake,
        "sleeper": lambda _delay: None,
        "connect_timeout": 2.5,
        "read_timeout": 9.0,
    }
    values.update(kwargs)
    return XaiClient(**values)  # type: ignore[arg-type]


def _ok(content: str, usage: dict[str, object] | None = None) -> HttpResponse:
    body = {"choices": [{"message": {"content": content}}], "usage": usage or {"prompt_tokens": 20, "completion_tokens": 30}}
    return HttpResponse(status=200, body=json.dumps(body).encode("utf-8"))


def test_disabled_and_dry_run_do_not_send(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Fake()
    monkeypatch.delenv(ENV_KEY, raising=False)
    with pytest.raises(XaiDisabled, match="No request was sent"):
        _client(fake, enabled=False).complete("prompt")
    monkeypatch.setenv(ENV_KEY, KEY)
    with pytest.raises(XaiDisabled, match="No request was sent"):
        _client(fake, enabled=False).complete("prompt")
    with pytest.raises(XaiDryRun, match="not sent"):
        _client(fake, dry_run=True).complete("prompt")
    assert fake.calls == []
    request = _client(fake).last_request
    built = XaiClient(enabled=True, dry_run=True).build_request("only the snapshot")
    assert built["model"] == "grok-4.3"
    assert built["reasoning_effort"] == "low"
    format_block = built["response_format"]
    assert isinstance(format_block, dict)
    assert format_block["type"] == "json_schema"
    schema = format_block["json_schema"]["schema"]
    assert isinstance(schema, dict)
    proposals = schema["properties"]["proposals"]["items"]["properties"]
    assert "prompt_hash" not in proposals
    assert "response_hash" not in proposals
    assert "tools" not in built
    assert "web_search" not in built
    assert "x_search" not in built
    assert request is None or "tools" not in request


def test_model_is_not_required_to_echo_hashes() -> None:
    body = {
        "model_id": "grok-4.3",
        "proposals": [
            {
                "action": "HOLD",
                "confidence": "0.20",
                "instrument_id": "VWRP",
                "reasoning": "no new weight",
            }
        ],
    }
    parsed = parse_response(json.dumps(body), prompt_digest_value="a" * 64, response_digest="b" * 64)
    assert parsed.model_id == "grok-4.3"
    body["proposals"][0]["prompt_hash"] = "c" * 64
    forged = parse_response(json.dumps(body), prompt_digest_value="a" * 64, response_digest="b" * 64)
    assert forged.reason == "hash_mismatch"


def test_retries_timeout_malformed_oversized_and_redaction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert RETRIES_AFTER_SEND == 0
    monkeypatch.setenv(ENV_KEY, KEY)
    sleeps: list[float] = []
    denied_rate = _Fake([HttpResponse(status=429, body=f"slow {KEY}".encode()), _ok("{}")])
    with pytest.raises(XaiError, match="HTTP 429"):
        _client(denied_rate, sleeper=lambda delay: sleeps.append(delay), max_calls=3, max_attempts=3).complete("hello")
    assert sleeps == []
    assert len(denied_rate.calls) == 1
    content, usage = _client(
        _Fake([_ok('{"model_id":"grok-4.3","proposals":[]}')]),
        sleeper=lambda delay: sleeps.append(delay),
    ).complete("hello")
    assert usage.http_status == 200
    assert usage.input_tokens == 20
    assert KEY not in json.dumps(usage.as_log())
    assert content.startswith("{")
    failure = parse_response(content, prompt_digest_value="a" * 64, response_digest="b" * 64)
    assert failure.reason == "malformed_json"

    denied = _Fake([HttpResponse(status=500, body=KEY.encode())] * 3)
    with pytest.raises(XaiError, match="HTTP 500") as caught:
        client = _client(denied, sleeper=lambda delay: sleeps.append(delay), max_calls=3)
        client.complete("hello")
    assert KEY not in str(caught.value)
    assert len(denied.calls) == 1
    assert client.calls_sent == 1
    assert caught.value.usage is not None
    assert caught.value.usage.http_status == 500

    timed = _Fake(error=TimeoutError(f"timed out {KEY}"))
    with pytest.raises(XaiTimeout) as timeout:
        _client(timed).complete("hello")
    assert len(timed.calls) == 1
    assert timeout.value.sent_state == "possibly_sent_or_billed"
    assert KEY not in str(timeout.value)

    dropped = _Fake(error=ConnectionResetError(f"reset {KEY}"))
    with pytest.raises(XaiError) as reset:
        client = _client(dropped, max_attempts=3)
        client.complete("hello")
    assert len(dropped.calls) == 1
    assert client.calls_sent == 1
    assert reset.value.sent_state == "possibly_sent_or_billed"
    assert KEY not in str(reset.value)

    broken = _Fake([HttpResponse(status=200, body=b"not-json")])
    with pytest.raises(XaiError, match="not JSON"):
        _client(broken).complete("hello")

    huge = "x" * 20001
    oversized = _Fake([_ok(huge)])
    with pytest.raises(XaiError, match="size cap"):
        _client(oversized).complete("hello")

    reasoning = _ok(
        "{}",
        {
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 2},
            "cost_in_usd": "0.01000000",
        },
    )
    _content, rich = _client(_Fake([reasoning])).complete("hello")
    assert rich.reasoning_tokens == 2
    assert rich.cost_in_usd_ticks == 100_000_000
    from sterling.pilot_config import ticks_to_usd

    assert ticks_to_usd(67_656_000) == Decimal("0.0067656")

    log = ProposalLog(tmp_path / "forward.jsonl")
    log.create(now=datetime(2024, 6, 1, tzinfo=UTC))
    log.append_event(
        timestamp_utc=datetime(2024, 6, 2, tzinfo=UTC),
        record_type="model_usage",
        fields=rich.as_log(),
    )
    assert log.verify() == 2
    stored = (tmp_path / "forward.jsonl").read_text(encoding="utf-8")
    assert KEY not in stored
    assert "Authorization" not in stored
    with pytest.raises(Exception, match="not allowed"):
        log.append_event(
            timestamp_utc=datetime(2024, 6, 3, tzinfo=UTC),
            record_type="headers",
            fields={"authorization": KEY},
        )


def test_ceilings_call_cap_and_transport_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_KEY, KEY)
    fake = _Fake([_ok("{}"), _ok("{}")])
    client = _client(fake, max_calls=1)
    client.complete("hello")
    with pytest.raises(XaiRefused, match="call cap"):
        client.complete("hello")
    assert len(fake.calls) == 1

    cheap = _Fake()
    with pytest.raises(XaiRefused, match="cost"):
        _client(cheap, cost_ceiling_usd=Decimal("0")).complete("hello")
    assert cheap.calls == []
    with pytest.raises(XaiRefused, match="tokens"):
        _client(_Fake(), max_estimated_tokens=2, output_token_budget=2).complete(
            "a prompt that is definitely longer than two tokens"
        )
    assert fake.calls[0]["connect_timeout"] == 2.5
    assert fake.calls[0]["read_timeout"] == 9.0
    assert fake.calls[0]["url"] == API_URL
    header = fake.calls[0]["headers"]
    assert isinstance(header, dict)
    assert header["Authorization"] == f"Bearer {KEY}"
    body = json.loads(fake.calls[0]["body"])  # type: ignore[arg-type]
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] == 1500
    assert "tools" not in body
    refused = _Fake(error=NotSent("refused"))
    refused.responses = [_ok("{}")]
    # Connection refused is not a sent request, so one retry is allowed inside the cap.
    calls = {"n": 0}

    def _post(**kwargs: object) -> HttpResponse:
        del kwargs
        calls["n"] += 1
        if calls["n"] == 1:
            raise NotSent("refused")
        return _ok("{}")

    class _Retry:
        def post(self, **kwargs: object) -> HttpResponse:
            return _post(**kwargs)

    retried = _client(_Retry(), max_calls=1, sleeper=lambda _delay: None)  # type: ignore[arg-type]
    retried.complete("hello")
    assert calls["n"] == 2
    assert retried.calls_sent == 1

    made: list[object] = []

    class _Sock:
        def settimeout(self, value: float) -> None:
            self.timeout = value

    class _Conn:
        def __init__(self, host: str, timeout: float | None = None) -> None:
            self.host = host
            self.timeout = timeout
            self.sock = _Sock()
            self.path = ""
            made.append(self)

        def connect(self) -> None:
            return None

        def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> None:
            del method, body, headers
            self.path = path

        def getresponse(self) -> object:
            return type("Resp", (), {"status": 204, "read": lambda self: b""})()

        def close(self) -> None:
            return None

    transport = HttpsTransport(connection_factory=_Conn)
    response = transport.post(url=API_URL, body=b"{}", headers={}, connect_timeout=3.0, read_timeout=8.0)
    assert response.status == 204
    conn = made[0]
    assert isinstance(conn, _Conn)
    assert conn.timeout == 3.0
    assert conn.path == API_PATH
    assert conn.sock.timeout == 8.0


def test_research_package_does_not_import_the_client() -> None:
    research = ROOT / "src/sterling/research"
    client = (ROOT / "src/sterling/xai_client.py").read_text(encoding="utf-8")
    assert "sterling.research" not in client
    assert "urllib.request" not in client
    assert "place_order" not in client
    for path in research.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "xai_client" not in text
        assert "http.client" not in text
        tree = ast.parse(text)
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for name in modules:
                assert not name.startswith(("urllib", "http.client", "socket"))
