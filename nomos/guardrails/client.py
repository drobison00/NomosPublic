# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bounded screen-only NeMo adapter; a trace is not independent policy attestation.

Recovered from a06b9d4/93b4e8c. Input prose is assessed without rewriting the
Intent. Commands, packaged code and raw structured arguments are excluded.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from nomos.config.settings import GuardrailsConfig
from nomos.level_0.types import Intent

_ERROR_MESSAGES = {
    "evaluation_error": "Guardrails evaluation could not be completed.",
    "unavailable": "Guardrails service is unavailable.",
    "dependency_unavailable": "The optional Guardrails HTTP dependency is unavailable.",
    "timeout": "Guardrails evaluation exceeded its deadline.",
    "invalid_response": "Guardrails returned an invalid screening response.",
    "input_too_large": "Guardrails input exceeds its configured byte budget.",
    "response_too_large": "Guardrails response exceeds its configured byte budget.",
    "transformed_response": "Guardrails transformed the prose; screen-only evaluation is required.",
}


class GuardrailsEvaluationError(RuntimeError):
    """Fixed local reason; arbitrary caller or server text is never reflected."""

    def __init__(self, message: str = "", *, reason_code: str = "evaluation_error"):
        self.reason_code = reason_code if reason_code in _ERROR_MESSAGES else "evaluation_error"
        super().__init__(_ERROR_MESSAGES[self.reason_code])


@dataclass(frozen=True)
class GuardrailsScreenDecision:
    """Raw status is distinct from the runtime's effective allow decision."""

    allow: bool
    blocked: bool = False
    findings: list[str] = field(default_factory=list)
    guidance: list[str] = field(default_factory=list)
    mode: str = "enforce"
    channels: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "clean"
    reason_code: str = ""


def build_screen_text(intent: Intent, channels: Iterable[str]) -> tuple[str, list[str]]:
    """Return a stable labelled bundle of selected nonempty prose channels."""
    sections: list[tuple[str, str]] = []
    for channel in channels:
        if channel in {"description", "justification", "context", "blast_radius"}:
            value = getattr(intent, channel) or ""
        elif channel == "evidence":
            value = "\n\n".join(item for item in intent.evidence if item.strip())
        elif channel == "preceding_commands":
            value = "\n".join(item for item in intent.preceding_commands if item.strip())
        elif channel == "rollback_steps":
            steps = intent.rollback_plan.steps if intent.rollback_plan else []
            value = "\n".join(step for step in steps if step.strip())
        else:
            continue  # Config validation rejects unknown/executable channels.
        if value.strip():
            sections.append((channel, value))
    return (
        "\n\n".join(f"[channel: {name}]\n{value}" for name, value in sections),
        [name for name, _ in sections],
    )


def _response_content(payload: Mapping[str, Any]) -> str | None:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                return content
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, Mapping):
            content = last.get("content")
            if isinstance(content, str):
                return content
    return None


def _blocked_decision() -> GuardrailsScreenDecision:
    return GuardrailsScreenDecision(
        allow=False,
        blocked=True,
        status="blocked",
        reason_code="blocked",
        findings=["An input rail blocked the submitted prose."],
    )


def _action_reports_block(action: Mapping[str, Any]) -> bool:
    """Honor explicitly reported RailOutcome failures/transforms without echoing data."""
    outcome = action.get("return_value")
    if not isinstance(outcome, Mapping):
        return False
    decision = outcome.get("decision")
    if decision == "block":
        return True
    if "decision" in outcome and (
        not isinstance(decision, str) or decision not in {"allow", "transform"}
    ):
        raise GuardrailsEvaluationError(reason_code="invalid_response")
    if "failed" in outcome:
        failed = outcome["failed"]
        if not isinstance(failed, bool) or failed:
            raise GuardrailsEvaluationError(reason_code="invalid_response")
    transforms = outcome.get("transforms", [])
    if not isinstance(transforms, list):
        raise GuardrailsEvaluationError(reason_code="invalid_response")
    if transforms or decision == "transform":
        raise GuardrailsEvaluationError(reason_code="transformed_response")
    return False


def parse_screen_response(
    payload: Mapping[str, Any], *, expected_text: str | None = None
) -> GuardrailsScreenDecision:
    """Require structural executed-input evidence before accepting clean prose.

    A trusted, correctly loaded profile remains an operator assumption. This
    cannot attest rail effectiveness or detect all swallowed upstream errors.
    """
    if not isinstance(payload, Mapping):
        raise GuardrailsEvaluationError(reason_code="invalid_response")
    content = _response_content(payload)
    if content is not None and "NOMOS_SCREEN_BLOCKED" in content:
        return _blocked_decision()
    guardrails = payload.get("guardrails")
    log = guardrails.get("log") if isinstance(guardrails, Mapping) else None
    rails = log.get("activated_rails") if isinstance(log, Mapping) else None
    if not isinstance(rails, list) or not rails:
        raise GuardrailsEvaluationError(reason_code="invalid_response")

    executed_input = False
    blocked = False
    for rail in rails:
        if not isinstance(rail, Mapping):
            raise GuardrailsEvaluationError(reason_code="invalid_response")
        kind, name, stop = rail.get("type"), rail.get("name"), rail.get("stop")
        if (
            not isinstance(kind, str)
            or not kind.strip()
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(stop, bool)
        ):
            raise GuardrailsEvaluationError(reason_code="invalid_response")
        decisions = rail.get("decisions", [])
        if not isinstance(decisions, list) or any(not isinstance(x, str) for x in decisions):
            raise GuardrailsEvaluationError(reason_code="invalid_response")
        if kind != "input":
            continue
        if stop or "stop" in decisions:
            blocked = True
            continue
        actions = rail.get("executed_actions")
        if not isinstance(actions, list) or not actions:
            raise GuardrailsEvaluationError(reason_code="invalid_response")
        for action in actions:
            if not isinstance(action, Mapping):
                raise GuardrailsEvaluationError(reason_code="invalid_response")
            action_name = action.get("action_name", action.get("name"))
            if not isinstance(action_name, str) or not action_name.strip():
                raise GuardrailsEvaluationError(reason_code="invalid_response")
            if _action_reports_block(action):
                blocked = True
        executed_input = True
    if blocked:
        return _blocked_decision()
    if not executed_input:
        raise GuardrailsEvaluationError(reason_code="invalid_response")
    if expected_text is not None:
        if content is None:
            raise GuardrailsEvaluationError(reason_code="invalid_response")
        if content != expected_text:
            raise GuardrailsEvaluationError(reason_code="transformed_response")
    return GuardrailsScreenDecision(allow=True, status="clean", reason_code="")


class GuardrailsHttpClient:
    """Per-council bounded, cancellation-safe input-only HTTP adapter.

    httpx is optional and imported only during enabled evaluation. Each call
    closes its own client/stream; a service's semaphore bounds concurrent calls.
    """

    def __init__(self, config: GuardrailsConfig):
        self._config = config.model_copy(deep=True)
        self._semaphore = asyncio.Semaphore(config.max_in_flight)

    async def evaluate(self, text: str) -> GuardrailsScreenDecision:
        cfg = self._config
        if len(text) > cfg.max_input_bytes:
            raise GuardrailsEvaluationError(reason_code="input_too_large")
        try:
            encoded_text = text.encode("utf-8")
        except UnicodeError as exc:
            raise GuardrailsEvaluationError(reason_code="evaluation_error") from exc
        if len(encoded_text) > cfg.max_input_bytes:
            raise GuardrailsEvaluationError(reason_code="input_too_large")
        try:
            httpx = importlib.import_module("httpx")
        except ImportError as exc:
            raise GuardrailsEvaluationError(reason_code="dependency_unavailable") from exc
        body = json.dumps(
            {
                "model": "screen",
                "messages": [{"role": "user", "content": text}],
                "guardrails": {
                    "config_id": cfg.config_id,
                    "options": {"rails": ["input"], "log": {"activated_rails": True}},
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            async with asyncio.timeout(cfg.timeout_ms / 1000):
                async with self._semaphore:
                    async with httpx.AsyncClient(
                        timeout=cfg.timeout_ms / 1000,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        async with client.stream(
                            "POST",
                            cfg.url + "/v1/chat/completions",
                            content=body,
                            headers={
                                "Content-Type": "application/json",
                                "Accept": "application/json",
                                "Accept-Encoding": "identity",
                            },
                        ) as response:
                            if not 200 <= response.status_code < 300:
                                raise GuardrailsEvaluationError(reason_code="unavailable")
                            # Bound the bytes the JSON parser sees without allowing
                            # an HTTP decoder to inflate a compressed chunk first.
                            encoding = response.headers.get("Content-Encoding", "").strip().lower()
                            if encoding not in {"", "identity"}:
                                raise GuardrailsEvaluationError(reason_code="invalid_response")
                            pieces: list[bytes] = []
                            size = 0
                            async for piece in response.aiter_bytes(
                                chunk_size=min(65536, cfg.max_response_bytes + 1)
                            ):
                                size += len(piece)
                                if size > cfg.max_response_bytes:
                                    raise GuardrailsEvaluationError(
                                        reason_code="response_too_large"
                                    )
                                pieces.append(piece)
                            raw = b"".join(pieces)
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise GuardrailsEvaluationError(reason_code="invalid_response") from exc
            if not isinstance(payload, dict):
                raise GuardrailsEvaluationError(reason_code="invalid_response")
            return parse_screen_response(payload, expected_text=text)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise GuardrailsEvaluationError(reason_code="timeout") from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise GuardrailsEvaluationError(reason_code="unavailable") from exc
