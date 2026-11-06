# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Focused contract regressions for the optional prose-screen adapter."""

import asyncio
import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError

from nomos.config.settings import CouncilSettings, GuardrailsConfig
from nomos.guardrails import (
    GuardrailsEvaluationError,
    GuardrailsHttpClient,
    parse_screen_response,
)


def payload(text="safe prose", *, kind="input", stop=False):
    return {
        "guardrails": {
            "log": {
                "activated_rails": [
                    {
                        "type": kind,
                        "name": "synthetic input screen",
                        "stop": stop,
                        "decisions": ["stop"] if stop else [],
                        "executed_actions": [{"action_name": "synthetic_screen"}],
                    }
                ]
            }
        },
        "choices": [{"message": {"content": text}}],
    }


def test_defaults_and_independent_channels():
    a, b = CouncilSettings(), CouncilSettings()
    assert not a.guardrails.enabled
    assert a.guardrails.max_in_flight == 4
    a.guardrails.channels.clear()
    assert b.guardrails.channels


@pytest.mark.parametrize(
    "values",
    [
        {"mode": "invalid"},
        {"channels": []},
        {"channels": ["command"]},
        {"url": "http://"},
        # Synthetic credentials exercise URL rejection.
        {"url": "https://user:pass@example.test"},  # pragma: allowlist secret
        {"url": "https://example.test?token=opaque"},
        {"url": "https://example.test/#fragment"},
        {"timeout_ms": 0},
        {"max_input_bytes": 0},
        {"max_response_bytes": 4194305},
        {"max_in_flight": 65},
    ],
)
def test_invalid_settings(values):
    with pytest.raises(ValidationError):
        GuardrailsConfig(**values)


def test_clean_and_transformation():
    result = parse_screen_response(payload(), expected_text="safe prose")
    assert result.allow and not result.blocked and result.status == "clean"
    with pytest.raises(GuardrailsEvaluationError) as error:
        parse_screen_response(payload("changed"), expected_text="safe prose")
    assert error.value.reason_code == "transformed_response"


@pytest.mark.parametrize(
    "trace",
    [
        [],
        [{}],
        ["input"],
        [{"name": "unused"}],
        [
            {
                "type": "output",
                "name": "screen",
                "stop": False,
                "executed_actions": [{"action_name": "screen"}],
            }
        ],
        [
            {
                "type": "input",
                "name": "screen",
                "stop": "false",
                "executed_actions": [{"action_name": "screen"}],
            }
        ],
        [{"type": "input", "name": "screen", "stop": False, "executed_actions": []}],
    ],
)
def test_incomplete_trace_rejected(trace):
    data = payload()
    data["guardrails"]["log"]["activated_rails"] = trace
    with pytest.raises(GuardrailsEvaluationError) as error:
        parse_screen_response(data, expected_text="safe prose")
    assert error.value.reason_code == "invalid_response"


def test_block_is_fixed_local_text():
    marker = "USER_PRIVATE_MARKER"
    result = parse_screen_response(payload("NOMOS_SCREEN_BLOCKED " + marker))
    assert result.blocked and not result.allow and result.status == "blocked"
    assert marker not in str(result.findings) + str(result.metadata)


def test_error_message_does_not_echo_remote_data():
    error = GuardrailsEvaluationError("USER_PRIVATE_MARKER", reason_code="unknown_code")
    assert error.reason_code == "evaluation_error"
    assert "USER_PRIVATE_MARKER" not in str(error)


@pytest.fixture
def local_screen():
    state = {"mode": "clean", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(request)
            mode = state["mode"]
            text = request["messages"][0]["content"]
            if mode == "slow":
                time.sleep(0.3)
            if mode == "transformed":
                text = "a transformed message"
            response = json.dumps(payload(text)).encode()
            if mode == "large":
                response = b"x" * 512
            if mode == "invalid":
                response = b"USER_PRIVATE_MARKER"
            if mode == "encoded":
                response = gzip.compress(b"x" * 100000)
            self.send_response(503 if mode == "status" else 200)
            self.send_header("Content-Type", "application/json")
            if mode == "encoded":
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)


@pytest.mark.asyncio
async def test_real_socket_wire_contract_and_snapshot(local_screen, monkeypatch):
    url, state = local_screen
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    config = GuardrailsConfig(enabled=True, url=url, config_id="profile-a")
    client = GuardrailsHttpClient(config)
    config.config_id = "changed-after-client-creation"
    decision = await client.evaluate("safe prose")
    assert decision.status == "clean" and decision.allow
    sent = state["requests"][0]
    assert sent["guardrails"]["config_id"] == "profile-a"
    assert sent["guardrails"]["options"]["rails"] == ["input"]
    assert sent["guardrails"]["options"]["log"]["activated_rails"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code",
    [
        ("status", "unavailable"),
        ("encoded", "invalid_response"),
        ("invalid", "invalid_response"),
        ("transformed", "transformed_response"),
        ("large", "response_too_large"),
    ],
)
async def test_http_failures_are_bounded(local_screen, mode, code):
    url, state = local_screen
    state["mode"] = mode
    client = GuardrailsHttpClient(GuardrailsConfig(enabled=True, url=url, max_response_bytes=400))
    with pytest.raises(GuardrailsEvaluationError) as error:
        await client.evaluate("safe prose")
    assert error.value.reason_code == code
    assert "USER_PRIVATE_MARKER" not in str(error.value)


@pytest.mark.asyncio
async def test_oversized_utf8_never_calls_server(local_screen):
    url, state = local_screen
    client = GuardrailsHttpClient(GuardrailsConfig(enabled=True, url=url, max_input_bytes=4))
    with pytest.raises(GuardrailsEvaluationError) as error:
        await client.evaluate("☃☃")
    assert error.value.reason_code == "input_too_large"
    assert not state["requests"]


@pytest.mark.asyncio
async def test_deadline_releases_slot(local_screen):
    url, state = local_screen
    client = GuardrailsHttpClient(
        GuardrailsConfig(enabled=True, url=url, timeout_ms=100, max_in_flight=1)
    )
    state["mode"] = "slow"
    with pytest.raises(GuardrailsEvaluationError) as error:
        await client.evaluate("safe prose")
    assert error.value.reason_code == "timeout"
    state["mode"] = "clean"
    assert (await client.evaluate("safe prose")).allow


@pytest.mark.asyncio
async def test_external_cancellation_releases_slot(local_screen):
    url, state = local_screen
    state["mode"] = "slow"
    client = GuardrailsHttpClient(GuardrailsConfig(enabled=True, url=url, max_in_flight=1))
    task = asyncio.create_task(client.evaluate("safe prose"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state["mode"] = "clean"
    assert (await client.evaluate("safe prose")).allow


@pytest.mark.parametrize(
    "outcome,code",
    [
        ({"decision": "allow", "failed": True}, "invalid_response"),
        ({"decision": "transform", "transforms": []}, "transformed_response"),
        ({"decision": "allow", "transforms": [{"type": "redact"}]}, "transformed_response"),
        ({"decision": "unknown"}, "invalid_response"),
        ({"decision": []}, "invalid_response"),
        ({"failed": "false"}, "invalid_response"),
    ],
)
def test_explicit_action_failure_or_transformation_is_not_clean(outcome, code):
    data = payload()
    data["guardrails"]["log"]["activated_rails"][0]["executed_actions"][0]["return_value"] = outcome
    with pytest.raises(GuardrailsEvaluationError) as error:
        parse_screen_response(data, expected_text="safe prose")
    assert error.value.reason_code == code


def test_reported_action_block_is_not_clean():
    data = payload()
    data["guardrails"]["log"]["activated_rails"][0]["executed_actions"][0]["return_value"] = {
        "decision": "block",
        "failed": False,
        "transforms": [],
    }
    result = parse_screen_response(data, expected_text="safe prose")
    assert result.status == "blocked" and result.blocked and not result.allow
