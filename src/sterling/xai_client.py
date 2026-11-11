"""Disabled-by-default xAI chat client.

A call needs an explicit enable flag and the ``XAI_API_KEY`` environment
variable. ``dry_run`` builds the JSON body and does not open a connection.
The key is never written into a usage record, an exception message, or a log
line. This module does not touch a ledger, a policy file, or an order path.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

API_HOST = "api.x.ai"
API_PATH = "/v1/chat/completions"
API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-4.3"
ENV_KEY = "XAI_API_KEY"
# Any HTTP response may already have been billed, including 429. It is never
# retried. Only a connection refusal before the request is sent may be
# retried, and that attempt is not counted as a sent request.
MAX_CONTENT_CHARS = 20000
INPUT_USD_PER_MILLION = Decimal("5")
OUTPUT_USD_PER_MILLION = Decimal("25")
_TICKS = Decimal("100000000")

PROPOSAL_SCHEMA: dict[str, object] = {
    "additionalProperties": False,
    "properties": {
        "model_id": {"type": "string"},
        "proposals": {
            "items": {
                "additionalProperties": False,
                "properties": {
                    "action": {"enum": ["BUY", "SELL", "HOLD"], "type": "string"},
                    "confidence": {"type": "string"},
                    "instrument_id": {"type": "string"},
                    "model_id": {"type": "string"},
                    "planned_risk_gbp": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "size_request": {"type": "string"},
                    "target_weight": {"type": "string"},
                },
                "required": ["instrument_id", "action", "reasoning", "confidence"],
                "type": "object",
            },
            "minItems": 1,
            "type": "array",
        },
    },
    "required": ["model_id", "proposals"],
    "type": "object",
}


class XaiError(Exception):
    """A call was refused or the provider failed. The message is redacted."""

    def __init__(self, message: str, usage: UsageRecord | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class XaiDisabled(XaiError):
    """The flag or the environment variable was missing."""


class XaiDryRun(XaiError):
    """The request body was built and not sent."""


class XaiRefused(XaiError):
    """The cap or the ceiling stopped the call before the network."""


class XaiTimeout(XaiError):
    """A read or connect timeout after the request may have been sent. Not retried."""


class NotSent(Exception):
    """The connection was refused before the request bytes were written."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


@dataclass(frozen=True, slots=True)
class UsageRecord:
    request_hash: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    cost_in_usd_ticks: int | None
    latency_ms: int
    http_status: int
    local_estimate_note: str = "pre-send local ceiling, not an xAI invoice"
    provider_limit_note: str = (
        "max_completion_tokens caps visible output only. Reasoning tokens are billed separately "
        "and are not inside that cap. A prepaid balance and a $0 invoiced limit are account settings."
    )
    provider_request_id: str | None = None

    def as_log(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "cached_tokens": self.cached_tokens,
            "cost_in_usd_ticks": self.cost_in_usd_ticks,
            "http_status": self.http_status,
            "input_tokens": self.input_tokens,
            "latency_ms": self.latency_ms,
            "local_estimate_note": self.local_estimate_note,
            "model": self.model,
            "output_tokens": self.output_tokens,
            "provider_limit_note": self.provider_limit_note,
            "reasoning_tokens": self.reasoning_tokens,
            "request_hash": self.request_hash,
        }
        if self.provider_request_id is not None:
            fields["provider_request_id"] = self.provider_request_id
        return fields


class Transport(Protocol):
    def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        connect_timeout: float,
        read_timeout: float,
    ) -> HttpResponse: ...


class HttpsTransport:
    """POST helper. Tests substitute this so nothing here has to dial out."""

    def __init__(self, connection_factory: Callable[..., http.client.HTTPConnection] | None = None) -> None:
        self._factory = connection_factory or http.client.HTTPSConnection

    def post(
        self,
        *,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        connect_timeout: float,
        read_timeout: float,
    ) -> HttpResponse:
        if url != API_URL:
            raise XaiError("xAI client only posts to its configured https endpoint. No request was sent.")
        connection = self._factory(API_HOST, timeout=connect_timeout)
        try:
            try:
                connection.connect()
            except ConnectionRefusedError as exc:
                raise NotSent("connection refused before send") from exc
            sock = getattr(connection, "sock", None)
            if sock is not None:
                sock.settimeout(read_timeout)
            connection.request("POST", API_PATH, body=body, headers=dict(headers))
            response = connection.getresponse()
            payload = response.read()
            return HttpResponse(status=response.status, body=payload)
        except TimeoutError as exc:
            raise XaiTimeout("xAI request timed out. No proposal was accepted.") from exc
        finally:
            connection.close()


class XaiClient:
    def __init__(
        self,
        *,
        enabled: bool = False,
        dry_run: bool = False,
        model: str = DEFAULT_MODEL,
        max_calls: int = 1,
        max_estimated_tokens: int = 8000,
        output_token_budget: int = 1500,
        cost_ceiling_usd: Decimal = Decimal("1.00"),
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_attempts: int = 3,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if max_calls < 1 or max_attempts < 1 or output_token_budget < 1 or max_estimated_tokens < 1:
            raise XaiRefused("xAI limits must be positive. No request was sent.")
        if connect_timeout <= 0 or read_timeout <= 0 or cost_ceiling_usd < 0:
            raise XaiRefused("xAI timeouts and the cost ceiling must be non-negative. No request was sent.")
        self.enabled = enabled
        self.dry_run = dry_run
        self.model = model
        self.max_calls = max_calls
        self.max_estimated_tokens = max_estimated_tokens
        self.output_token_budget = output_token_budget
        self.cost_ceiling_usd = cost_ceiling_usd
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_attempts = max_attempts
        self.transport: Transport = transport or HttpsTransport()
        self._sleep = sleeper or time.sleep
        self.calls_sent = 0
        self.last_request: dict[str, object] | None = None

    def build_request(self, prompt: str) -> dict[str, object]:
        return {
            "messages": [
                {
                    "content": (
                        "Reply with the JSON schema only. Use the supplied snapshot. "
                        "Do not request tools, web search, or a broker order."
                    ),
                    "role": "system",
                },
                {"content": prompt, "role": "user"},
            ],
            "max_completion_tokens": self.output_token_budget,
            "model": self.model,
            "reasoning_effort": "low",
            "response_format": {
                "json_schema": {"name": "sterling_proposals", "schema": PROPOSAL_SCHEMA, "strict": True},
                "type": "json_schema",
            },
            "temperature": 0,
        }

    def estimated_cost_usd(self, prompt: str) -> Decimal:
        prompt_tokens = _token_estimate(prompt)
        total = Decimal(prompt_tokens) * INPUT_USD_PER_MILLION + Decimal(self.output_token_budget) * OUTPUT_USD_PER_MILLION
        return total / Decimal(1_000_000)

    def complete(self, prompt: str) -> tuple[str, UsageRecord]:
        key = os.environ.get(ENV_KEY) or ""
        if not self.enabled or not key:
            raise XaiDisabled("xAI client refuses to run without an explicit config flag and XAI_API_KEY. No request was sent.")
        if self.calls_sent >= self.max_calls:
            raise XaiRefused("per-run call cap reached. No further request was sent.")
        request = self.build_request(prompt)
        self.last_request = request
        self._refuse_tools(request)
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        prompt_tokens = _token_estimate(prompt)
        if prompt_tokens + self.output_token_budget > self.max_estimated_tokens:
            raise XaiRefused("estimated tokens exceed the per-run ceiling. No request was sent.")
        if self.estimated_cost_usd(prompt) > self.cost_ceiling_usd:
            raise XaiRefused("estimated cost exceeds the per-run ceiling. No request was sent.")
        if self.dry_run:
            raise XaiDryRun("dry_run: request built and not sent.")
        request_hash = hashlib.sha256(encoded).hexdigest()
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "SterlingResearch/1.0",
        }
        started = time.monotonic()
        response = self._send(encoded, headers, key)
        latency_ms = int((time.monotonic() - started) * 1000)
        return _parse_success(response, key=key, request_hash=request_hash, model=self.model, latency_ms=latency_ms)

    def _send(self, body: bytes, headers: Mapping[str, str], key: str) -> HttpResponse:
        """One billed response. A connection refusal before send may be retried."""
        delay = 0.5
        for attempt in range(self.max_attempts):
            try:
                response = self.transport.post(
                    url=API_URL,
                    body=body,
                    headers=headers,
                    connect_timeout=self.connect_timeout,
                    read_timeout=self.read_timeout,
                )
            except NotSent:
                if attempt + 1 < self.max_attempts:
                    self._sleep(delay)
                    delay *= 2
                    continue
                raise XaiError("xAI connection was refused before the request was sent.") from None
            except (XaiTimeout, TimeoutError) as exc:
                self.calls_sent += 1
                raise XaiTimeout(_redact("xAI read timed out after send. Not retried.", key)) from exc
            self.calls_sent += 1
            return response
        raise XaiError("xAI connection was refused before the request was sent.")

    def _refuse_tools(self, request: Mapping[str, object]) -> None:
        banned = {"tools", "tool_choice", "web_search", "search_parameters", "x_search"}
        if banned & set(request):
            raise XaiRefused("xAI request refuses tools and search. No request was sent.")


def _parse_success(response: HttpResponse, *, key: str, request_hash: str, model: str, latency_ms: int) -> tuple[str, UsageRecord]:
    text = response.body.decode("utf-8", errors="replace")
    usage = UsageRecord(
        request_hash=request_hash,
        model=model,
        input_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        cached_tokens=None,
        cost_in_usd_ticks=None,
        latency_ms=latency_ms,
        http_status=response.status,
    )
    if response.status != 200:
        raise XaiError(_redact(f"xAI HTTP {response.status}. {_snippet(text, key)}", key), usage)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise XaiError(_redact("xAI response was not JSON.", key), usage) from exc
    if not isinstance(payload, dict):
        raise XaiError("xAI response was not an object.", usage)
    raw_usage = payload.get("usage")
    request_id = _request_id(payload, key)
    if isinstance(raw_usage, dict):
        usage = UsageRecord(
            request_hash=request_hash,
            model=model,
            input_tokens=_int_field(raw_usage.get("prompt_tokens")),
            output_tokens=_int_field(raw_usage.get("completion_tokens")),
            reasoning_tokens=_reasoning_tokens(raw_usage),
            cached_tokens=_cached_tokens(raw_usage),
            cost_in_usd_ticks=_cost_ticks(raw_usage),
            latency_ms=latency_ms,
            http_status=response.status,
            provider_request_id=request_id,
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise XaiError("xAI response had no choice.", usage)
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise XaiError("xAI response had no message content.", usage)
    content = message["content"]
    if len(content) > MAX_CONTENT_CHARS:
        raise XaiError("xAI response exceeded the size cap.", usage)
    return content, usage


def _cached_tokens(usage: Mapping[str, object]) -> int | None:
    direct = _int_field(usage.get("cached_tokens"))
    if direct is not None:
        return direct
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        return _int_field(details.get("cached_tokens"))
    return None


def _reasoning_tokens(usage: Mapping[str, object]) -> int | None:
    direct = _int_field(usage.get("reasoning_tokens"))
    if direct is not None:
        return direct
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        return _int_field(details.get("reasoning_tokens"))
    return None


def _cost_ticks(usage: Mapping[str, object]) -> int | None:
    if "cost_in_usd_ticks" in usage:
        return _int_field(usage.get("cost_in_usd_ticks"))
    raw = usage.get("cost_in_usd")
    if not isinstance(raw, str):
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * _TICKS).to_integral_value())


def _int_field(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _token_estimate(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def _request_id(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get("id")
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        return None
    return _redact(value.strip(), key)


def _snippet(text: str, key: str) -> str:
    return _redact(text[:180].replace("\n", " "), key)


def _redact(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "[redacted]")
    return text.replace("Bearer ", "Bearer [redacted-prefix] ")
