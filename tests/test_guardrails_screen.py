# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Runtime regressions for the optional per-council Guardrails stage."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from nomos.config.settings import CouncilSettings, GuardrailsConfig, OPAConfig
from nomos.guardrails import GuardrailsEvaluationError, GuardrailsScreenDecision
from nomos.level_0.types import Intent
from nomos.level_4.council.workflow import WorkflowState
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput
from nomos.nat.review_audit import build_intent_review_ledger_payload
from nomos.opa.client import OPAPolicyDecision


class Screen:
    def __init__(self, outcome="clean", calls=None):
        self.outcome = outcome
        self.calls = calls if calls is not None else []
        self.texts = []

    async def evaluate(self, text):
        self.calls.append("guardrails")
        self.texts.append(text)
        if self.outcome == "error":
            raise GuardrailsEvaluationError("REMOTE_PRIVATE_MARKER", reason_code="unavailable")
        blocked = self.outcome == "blocked"
        return GuardrailsScreenDecision(
            allow=not blocked,
            blocked=blocked,
            status=self.outcome,
            reason_code="blocked" if blocked else "",
            findings=["The configured input rail blocked the prose."] if blocked else [],
        )


def service(tmp_path, **guardrails):
    return CouncilService(
        CouncilSettings(
            council_id="guardrails-runtime",
            require_auth=False,
            llm_backends={},
            members=[],
            council_key_path=str(tmp_path),
            opa=OPAConfig(enabled=False),
            guardrails=GuardrailsConfig(enabled=True, **guardrails),
        )
    )


def request(route, tmp_path, **overrides):
    values = dict(
        agent_id="runtime-test-agent",
        description="Review a routine service status report",
        command="/bin/echo status",
        working_directory=str(tmp_path),
        target_resource="svc/status",
        justification="A harmless routine service check.",
    )
    values.update(overrides)
    return (AuthorizeInput if route == "authorize" else ProposeIntentInput)(**values)


async def invoke(svc, route, tmp_path):
    return await getattr(svc, route)(request(route, tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["authorize", "propose_intent"])
@pytest.mark.parametrize("outcome", ["blocked", "error"])
async def test_enforce_denies_before_downstream_review(tmp_path, route, outcome):
    svc = service(tmp_path)
    screen = Screen(outcome)
    svc._guardrails_client = screen

    async def forbidden(_intent):
        pytest.fail("downstream code reviewer reached after enforced screen denial")

    svc._run_code_review = forbidden
    result = await invoke(svc, route, tmp_path)
    if route == "authorize":
        assert result.status == "denied"
        assert result.writ is None
        identifier = result.decision_id
    else:
        assert result.success
        assert result.state == WorkflowState.DENIED.value
        identifier = result.intent_id
    assert svc.council.get_workflow(identifier) is None
    assert len(screen.texts) == 1
    assert "/bin/echo status" not in screen.texts[0]
    assert "REMOTE_PRIVATE_MARKER" not in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["enforce", "shadow"])
@pytest.mark.parametrize("fail_closed", [True, False])
@pytest.mark.parametrize("outcome", ["clean", "blocked", "error"])
async def test_raw_status_and_effective_allow_truth_table(tmp_path, mode, fail_closed, outcome):
    svc = service(tmp_path, mode=mode, fail_closed=fail_closed)
    svc._guardrails_client = Screen(outcome)
    result = await svc._evaluate_guardrails(
        Intent.create(
            agent_id="test",
            description="Routine status report",
            command="/bin/echo status",
            working_directory=str(tmp_path),
            target_resource="svc/status",
        )
    )
    assert result is not None
    expected = mode == "shadow" or outcome == "clean" or (outcome == "error" and not fail_closed)
    assert result.allow is expected
    assert result.blocked is (outcome == "blocked")
    assert result.status == outcome
    assert result.mode == mode
    assert "REMOTE_PRIVATE_MARKER" not in str(result.findings)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["authorize", "propose_intent"])
@pytest.mark.parametrize("opa_allows", [True, False])
async def test_opa_short_circuit_and_stage_order(tmp_path, route, opa_allows):
    svc = service(tmp_path)
    calls = []
    svc._guardrails_client = Screen("clean", calls)

    async def opa(*args, **kwargs):
        calls.append("opa")
        return OPAPolicyDecision(allow=opa_allows, reasons=[] if opa_allows else ["policy denied"])

    async def code(intent):
        calls.append("code_review")
        return intent, None

    svc._evaluate_opa_filter = opa
    svc._run_code_review = code
    await invoke(svc, route, tmp_path)
    assert calls[:3] == (["opa", "guardrails", "code_review"] if opa_allows else ["opa"])


@pytest.mark.asyncio
async def test_empty_selected_prose_is_skipped_not_clean(tmp_path):
    svc = service(tmp_path, channels=["context"])
    screen = Screen()
    svc._guardrails_client = screen
    result = await svc._evaluate_guardrails(
        Intent.create(
            agent_id="test",
            description="Routine status report",
            command="/bin/echo status",
            working_directory=str(tmp_path),
            target_resource="svc/status",
            context="",
        )
    )
    assert result is not None
    assert result.status == "skipped"
    assert result.reason_code == "no_prose_content"
    assert result.allow and not result.blocked
    assert screen.calls == []


@pytest.mark.asyncio
async def test_disabled_council_has_no_screen_or_network(tmp_path):
    svc = CouncilService(
        CouncilSettings(
            council_id="off",
            require_auth=False,
            llm_backends={},
            members=[],
            council_key_path=str(tmp_path),
            guardrails=GuardrailsConfig(enabled=False),
        )
    )
    assert svc._guardrails_client is None
    assert (
        await svc._evaluate_guardrails(
            Intent.create(
                agent_id="test",
                description="Routine status report",
                command="/bin/echo status",
                working_directory=str(tmp_path),
                target_resource="svc/status",
            )
        )
        is None
    )


def test_legacy_audit_call_remains_valid_with_no_guardrails():
    payload = build_intent_review_ledger_payload(
        request_type="authorize",
        intent_dict={"intent_id": "legacy"},
        council_id="default",
        agent_id="test",
        final_state="denied",
        decision="deny",
    )
    assert payload["guardrails"] is None
    assert payload["schema_version"] == 1


@pytest.mark.parametrize("service_port", [8000, 18000])
def test_internal_screen_policy_allows_catalog_councils_and_target_port(service_port):
    """Catalog enablement does not depend on the single-council template flag."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("Helm is required for the offline Guardrails policy regression")
    chart = Path(__file__).resolve().parents[1] / "deploy/kubernetes/nomos"
    rendered = subprocess.run(
        [
            helm,
            "template",
            "nomos",
            str(chart),
            "--set",
            "networkPolicy.enabled=true",
            "--set",
            "storage.mode=service",
            "--set",
            "postgres.dev.enabled=true",
            "--set",
            "guardrails.enabled=true",
            "--set",
            "council.guardrails.enabled=false",
            "--set",
            f"guardrails.service.port={service_port}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    docs = [doc for doc in yaml.safe_load_all(rendered.stdout) if doc]
    policies = {
        doc["metadata"]["name"]: doc["spec"] for doc in docs if doc["kind"] == "NetworkPolicy"
    }
    council = next(spec for name, spec in policies.items() if name.endswith("-council"))
    screen_rules = [
        rule
        for rule in council["egress"]
        if any(
            peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
            == "guardrails"
            for peer in rule.get("to", [])
        )
    ]
    assert len(screen_rules) == 1
    assert screen_rules[0]["ports"] == [{"protocol": "TCP", "port": "http"}]
    screen = next(spec for name, spec in policies.items() if name.endswith("-guardrails"))
    assert screen["ingress"][0]["ports"] == [{"protocol": "TCP", "port": "http"}]
    deployment = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-guardrails")
    )
    ports = deployment["spec"]["template"]["spec"]["containers"][0]["ports"]
    assert {"name": "http", "containerPort": 8000} in ports
    service = next(
        doc
        for doc in docs
        if doc["kind"] == "Service" and doc["metadata"]["name"].endswith("-guardrails")
    )
    assert service["spec"]["ports"][0]["port"] == service_port
    assert service["spec"]["ports"][0]["targetPort"] == "http"
