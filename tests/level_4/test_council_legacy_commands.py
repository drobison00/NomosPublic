# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stored workflows whose command predates the control-character ban.

Such records were valid when written. They must stay readable (status, pending
lists) but must never become executable: approval is refused and nothing is
minted. A fresh submission with the same command is rejected outright.
"""

import pytest
from pydantic import ValidationError

from nomos.level_0.types import Intent
from nomos.level_2.quorum import VoteType
from nomos.level_4.council import SecurityCouncil, WorkflowState

from .test_council_workflow_store import FakeWorkflowStore, _council

LEGACY_COMMAND = "/bin/true\nrm -rf /tmp/x"


def _shell_intent(command: str, tag: str) -> Intent:
    return Intent.create(
        agent_id="agent-1",
        action_type="shell.exec",
        description=f"shell action {tag}",
        target_resource="/tmp/test",
        command=command,
        justification="legacy command hydration test",
    )


def _store_with_legacy_and_clean() -> tuple[SecurityCouncil, str, str]:
    """Persist two awaiting-approval records; rewrite one to a pre-ban command."""
    store = FakeWorkflowStore()
    council = _council(store)
    legacy = _shell_intent("/bin/true", "legacy")
    clean = _shell_intent("/bin/true", "clean")
    for intent in (legacy, clean):
        result = council.submit_intent(intent)
        assert result.success and result.requires_approval, result.error
        record = result.workflow.to_durable_dict()
        if intent is legacy:
            record["intent"]["command"] = LEGACY_COMMAND
        store.upsert_workflow(
            intent.intent_id,
            record,
            state=WorkflowState.AWAITING_APPROVAL.value,
            is_terminal=False,
            expires_at=None,
        )
    return council, legacy.intent_id, clean.intent_id


def test_legacy_record_hydrates_and_lists_alongside_clean_record():
    council, legacy_id, clean_id = _store_with_legacy_and_clean()
    replica = _council(council._workflow_store)  # fresh replica: reads only the store

    legacy = replica.get_workflow(legacy_id)
    clean = replica.get_workflow(clean_id)
    assert legacy is not None and legacy.intent.command == LEGACY_COMMAND
    assert clean is not None and clean.intent.command == "/bin/true"

    pending = {wf.intent_id for wf in replica.get_pending_approvals()}
    assert pending == {legacy_id, clean_id}


def test_legacy_record_cannot_be_approved_or_minted():
    council, legacy_id, clean_id = _store_with_legacy_and_clean()
    store = council._workflow_store
    replica = _council(store)
    assert replica.get_workflow(legacy_id).intent.command == LEGACY_COMMAND

    result = replica.approve_intent(legacy_id, "alice")
    assert result.success is False
    assert "command no longer admissible" in result.error
    assert replica.get_workflow(legacy_id).state == WorkflowState.DENIED
    assert store.rows[legacy_id]["state"] == WorkflowState.DENIED.value
    assert replica.mint_token(legacy_id) is None
    assert replica.execute_intent(legacy_id).success is False

    # The clean record beside it is unaffected.
    assert council.approve_intent(clean_id, "alice").success
    assert council.approve_intent(clean_id, "bob").success
    assert council.get_workflow(clean_id).state == WorkflowState.APPROVED
    assert council.mint_token(clean_id) is not None


def test_unreadable_pending_record_is_skipped_not_fatal():
    council, legacy_id, clean_id = _store_with_legacy_and_clean()
    store = council._workflow_store
    store.rows[legacy_id]["workflow_json"]["intent"]["command"] = "mkfs.ext4 /dev/sda1"
    replica = _council(store)

    assert {wf.intent_id for wf in replica.get_pending_approvals()} == {clean_id}


def test_fresh_submission_with_newline_is_rejected():
    with pytest.raises(ValidationError) as exc:
        _shell_intent(LEGACY_COMMAND, "fresh")
    assert "control character" in str(exc.value)


def test_wrong_shaped_intent_record_is_skipped_not_fatal():
    """A record whose ``intent`` is not a mapping must be skipped like any other
    unreadable record, not turn the whole listing into an error."""
    store = FakeWorkflowStore()
    council = _council(store)
    good = _shell_intent("/bin/true", "good")
    bad = _shell_intent("/bin/true", "bad")
    for intent in (good, bad):
        result = council.submit_intent(intent)
        assert result.success and result.requires_approval, result.error
        record = result.workflow.to_durable_dict()
        if intent is bad:
            record["intent"] = "not-a-mapping"
        store.upsert_workflow(
            intent.intent_id,
            record,
            state=WorkflowState.AWAITING_APPROVAL.value,
            is_terminal=False,
            expires_at=None,
        )

    replica = _council(store)
    pending = replica.get_pending_approvals()
    assert [wf.intent.intent_id for wf in pending] == [good.intent_id]


@pytest.mark.parametrize("finalize", [False, True])
@pytest.mark.parametrize("vote", ["approve", "deny"])
def test_legacy_revision_refused_before_quorum_mutation(finalize, vote):
    council, legacy_id, _ = _store_with_legacy_and_clean()
    # Keep a real open quorum with pre-existing votes, then load historical
    # intent data exactly as a stored workflow would be hydrated.
    workflow = council.get_workflow(legacy_id)
    record = council._quorum_manager.get_status(legacy_id).record
    council._quorum_manager.cast_vote(legacy_id, "alice", VoteType.DENY, auto_close=False)
    council._quorum_manager.cast_vote(legacy_id, "bob", VoteType.APPROVE, auto_close=False)
    before = record.to_dict()
    workflow.intent = Intent.from_stored(
        council._workflow_store.rows[legacy_id]["workflow_json"]["intent"]
    )

    result = council.revise_vote(legacy_id, "alice", vote, debate_round=1, finalize=finalize)

    assert not result.success
    assert "command no longer admissible" in result.error
    assert record.to_dict() == before
    reloaded = _council(council._workflow_store).get_workflow(legacy_id)
    assert reloaded.state == WorkflowState.DENIED
    assert reloaded.denied_at is not None
    assert reloaded.denial_reason == result.error
    assert council.mint_token(legacy_id) is None


@pytest.mark.parametrize("allow_pending", [False, True])
def test_legacy_batch_finalization_refused_before_quorum_mutation(allow_pending):
    council, legacy_id, _ = _store_with_legacy_and_clean()
    for voter in ("alice", "bob"):
        council.cast_member_vote(legacy_id, voter, VoteType.APPROVE, finalize=False)
    record = council._quorum_manager.get_status(legacy_id).record
    before = record.to_dict()
    workflow = council.get_workflow(legacy_id)
    workflow.intent = Intent.from_stored(
        council._workflow_store.rows[legacy_id]["workflow_json"]["intent"]
    )

    result = council.finalize_intent_review(legacy_id, allow_pending=allow_pending)

    assert not result.success
    assert "command no longer admissible" in result.error
    assert record.to_dict() == before
    reloaded = _council(council._workflow_store).get_workflow(legacy_id)
    assert reloaded.state == WorkflowState.DENIED
    assert reloaded.denial_reason == result.error
    assert council.mint_token(legacy_id) is None


@pytest.mark.parametrize(
    "state", [WorkflowState.APPROVED, WorkflowState.COMPLETED, WorkflowState.DENIED]
)
@pytest.mark.parametrize("allow_pending", [False, True])
def test_historical_invalid_finalization_never_reports_success(state, allow_pending):
    council, legacy_id, _ = _store_with_legacy_and_clean()
    store = council._workflow_store
    store.rows[legacy_id]["workflow_json"]["state"] = state.value
    replica = _council(store)
    assert replica.get_workflow(legacy_id).intent.command == LEGACY_COMMAND
    result = replica.finalize_intent_review(legacy_id, allow_pending=allow_pending)
    assert not result.success
    assert "command no longer admissible" in result.error
    assert replica.mint_token(legacy_id) is None


@pytest.mark.parametrize("finalize", [False, True])
def test_clean_debate_revision_and_batch_semantics_preserved(finalize):
    council, _, clean_id = _store_with_legacy_and_clean()
    assert council.cast_member_vote(clean_id, "alice", VoteType.DENY, finalize=False).success
    assert council.cast_member_vote(clean_id, "bob", VoteType.APPROVE, finalize=False).success
    result = council.revise_vote(clean_id, "alice", "approve", debate_round=1, finalize=finalize)
    assert result.success
    if not finalize:
        assert result.workflow.state == WorkflowState.AWAITING_APPROVAL
        assert (
            council.finalize_intent_review(clean_id, allow_pending=True).workflow.state
            == WorkflowState.AWAITING_APPROVAL
        )
        assert council.finalize_intent_review(clean_id).success
    assert council.get_workflow(clean_id).state == WorkflowState.APPROVED
    assert council.mint_token(clean_id) is not None


@pytest.mark.parametrize("route", ["approve", "deny", "cast-approve", "cast-deny"])
@pytest.mark.parametrize("hidden", ["\r", "\n", "\x00", "`"])
def test_legacy_vote_wrappers_deny_without_quorum_mutation(route, hidden):
    council, legacy_id, _ = _store_with_legacy_and_clean()
    workflow = council.get_workflow(legacy_id)
    data = workflow.intent.to_dict()
    data["command"] = f"/bin/echo before{hidden}after"
    workflow.intent = Intent.from_stored(data)
    record = council._quorum_manager.get_status(legacy_id).record
    before = record.to_dict()
    if route == "approve":
        result = council.approve_intent(legacy_id, "alice")
    elif route == "deny":
        result = council.deny_intent(legacy_id, "alice")
    else:
        vote = VoteType.APPROVE if route == "cast-approve" else VoteType.DENY
        result = council.cast_member_vote(legacy_id, "alice", vote, finalize=False)
    assert not result.success
    assert "command no longer admissible" in result.error
    assert record.to_dict() == before
    reloaded = _council(council._workflow_store).get_workflow(legacy_id)
    assert reloaded.state == WorkflowState.DENIED
    assert reloaded.denial_reason == result.error


@pytest.mark.parametrize("use_preflight", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_headless_allow_policy_cannot_approve_reused_legacy_intent(use_preflight, legacy):
    from nomos.level_1.policy import DecisionType, PolicyRule

    store = FakeWorkflowStore()
    council = SecurityCouncil(workflow_store=store)
    council._policy_engine.add_rule(
        PolicyRule(
            rule_id="test-headless-allow",
            name="Headless allow",
            priority=0,
            action_types={"shell.exec"},
            decision=DecisionType.ALLOW,
        )
    )
    intent = _shell_intent("/bin/true", "headless")
    if legacy:
        data = intent.to_dict()
        data["command"] = LEGACY_COMMAND
        intent = Intent.from_stored(data)
    preflight = council.preflight_intent(intent).preflight if use_preflight else None
    result = council.submit_intent(intent, preflight=preflight)
    if legacy:
        assert not result.success
        assert "command no longer admissible" in result.error
        assert result.workflow.state == WorkflowState.DENIED
        assert _council(store).get_workflow(intent.intent_id).state == WorkflowState.DENIED
        assert council.mint_token(intent.intent_id) is None
    else:
        assert result.success
        assert result.workflow.state == WorkflowState.APPROVED
        assert council.mint_token(intent.intent_id) is not None
