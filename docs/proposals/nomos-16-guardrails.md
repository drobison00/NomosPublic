<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# NOMOS-16: optional Guardrails screening per council

Status: revived implementation and deployment specification. NeMo team alignment
on production profiles, provider selection, and policy effectiveness remains
pending. The initial profile is deliberately model-free.

## Purpose and placement

Screen selected agent-authored prose after OPA permits continuation and before
any downstream code-review, chairman, or member model receives that prose.
Both `authorize` and `propose_intent`, including parallel-chairman mode, follow:

1. Resolve the council and apply its existing route-specific authentication,
   membership, and permission checks.
2. Evaluate OPA. A denial stops here; disabled OPA is a no-op.
3. Evaluate that council's optional Guardrails screen.
4. Run the existing policy, model review, approval, and writ pipeline.

This feature preserves each route's existing authorization semantics: it does
not add a permission gate to `propose_intent`. The existing `authorize`
permission gate still runs before OPA and screening.

Guardrails may deny or observe. A clean screen never grants authority, bypasses
later checks, changes thresholds, or rewrites the Intent. Commands, packaged
files, code artifacts, and raw structured tool arguments retain their existing
security boundaries and are not covered by this prose screen.

## Configuration and independent councils

`CouncilSettings.guardrails` defaults to `enabled: false`. Each resolved council
owns its configuration and client; no process-global client changes another
council's profile. The single-council template accepts
`COUNCIL_GUARDRAILS_ENABLED`, `URL`, `CONFIG_ID`, `TIMEOUT_MS`, `FAIL_CLOSED`, and
`MODE` with the same prefix. Explicit catalog values override that template.

The complete catalog example is
[`configs/guardrails_council_catalog.example.yaml`](../../configs/guardrails_council_catalog.example.yaml).
Its relevant per-council overrides are:

```yaml
councils:
  council-a:
    config_file: council.yaml
    council_id: council-a
    guardrails:
      enabled: true
      url: http://guardrails-a:8000
      config_id: council-a
      mode: enforce
      fail_closed: true
  council-b:
    config_file: council.yaml
    council_id: council-b
    guardrails:
      enabled: false
      url: http://guardrails-b:8000
      config_id: council-b
      mode: enforce
      fail_closed: true
```

A separate observation rollout for B changes only B:

```yaml
guardrails:
  enabled: true
  url: http://guardrails-b:8000
  config_id: council-b
  mode: shadow
  fail_closed: true
```

Provision `council-a` and `council-b` profile directories on their respective
sidecars first. A profile ID selects a loaded sidecar profile; it does not create
one. The shipped profile ID is `default-council`. Start with a reviewed copy of
that profile, or explicitly select it, and validate each configured endpoint.

Existing authenticated council profile operations persist these fields. Supply
the complete intended profile when replacing it. The existing registry
fingerprint/reload mechanism rebuilds services for future requests; an in-flight
request retains its original configuration. Updating A must not mutate B.
This adds no distributed hot-reload guarantee across replicas.

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Construct and use a screen only for this council |
| `url` | `http://localhost:8000` | Trusted sidecar base URL; deployment templates use their service address |
| `config_id` | `default-council` | Loaded NeMo profile |
| `mode` | `enforce` | `enforce` or `shadow` |
| `fail_closed` | `true` | Deny screening errors in enforce mode |
| `timeout_ms` | `2000` | Total queue/connect/read deadline, 1–60000 ms |
| `channels` | see below | Nonempty ordered subset of supported prose channels |
| `max_input_bytes` | `262144` | UTF-8 prose budget, at most 1048576 bytes |
| `max_response_bytes` | `1048576` | Streamed response budget, at most 4194304 bytes |
| `max_in_flight` | `4` | Per-client concurrency, 1–64; not a fleet-wide rate limit |

The default channels are `description`, `justification`, `context`,
`blast_radius`, `evidence`, `preceding_commands`, and `rollback_steps`.
`preceding_commands` and rollback steps are caller-provided descriptive prose;
the command being authorized is excluded. Selected nonempty fields are joined
with stable channel labels. Avoid putting secrets in any selected field.

## Decision and audit contract

| Screen result | Enforce | Shadow | Recorded raw status |
| --- | --- | --- | --- |
| Disabled | Continue without HTTP | Continue without HTTP | Guardrails record is absent/`null` |
| Valid clean | Continue existing checks | Continue existing checks | `clean`, `blocked: false` |
| Detected block | Deny | Continue existing checks | `blocked`, `blocked: true` |
| Evaluation error, fail closed | Deny | Continue existing checks | `error`, `blocked: false` |
| Evaluation error, fail open | Continue existing checks | Continue existing checks | `error`, `blocked: false` |
| No selected prose | Continue without HTTP | Continue without HTTP | `skipped`, `no_prose_content` |

An evaluation error is not evidence of a detected attack. Audit records include
`status`, normalized `reason_code`, effective `allow`, raw `blocked`, `mode`,
`config_id`, selected `channels`, and a fixed local reason. Shadow retains a raw
block/error while setting effective allow true. The optional `guardrails`
review field is `null` when disabled or never reached, including OPA denials.
Older records lacking it remain valid. Cancellation propagates to the caller.

## Sidecar protocol and deployment

The council needs the optional HTTP extra (`uv sync --extra guardrails`), not the
NeMo SDK, GPU, or provider credentials. The separate sidecar image pins
`nemoguardrails[server]==0.24.0` and the tested `yara-python==4.5.4`.
The `python:3.12-slim` base tag remains floating, so image rebuilds are not
fully reproducible; production deployments should review and pin an approved
base-image digest. The adapter sends a screen-only input-rails
request to `/v1/chat/completions` with the selected `config_id`.
A clean response must contain typed executed-input evidence and exactly echo
the submitted prose. Transformations, invalid responses, transport failures,
and budget overruns are normalized errors. The adapter does not follow
redirects or use ambient HTTP credentials. Findings never quote remote bodies.

Compose, Helm, and Kind defaults leave both screening and sidecar provisioning
off. Starting a sidecar does not enable any council:

```bash
docker compose -f deploy/docker-compose.yml --profile guardrails up -d guardrails
```

Enable screening separately in a selected council profile or its single-council
template. Compose's `GUARDRAILS_CONFIG_DIR` selects the mounted profile root.
For local Kind image preparation, opt in with `NOMOS_BUILD_GUARDRAILS=true` when
running `deploy/kubernetes/scripts/build-kind-images.sh`; this builds an image,
without enabling the service or council screen.

Helm's `guardrails.enabled` provisions the optional service. Independently,
`council.guardrails.enabled` enables the single-council template. Set
`council.guardrails.url` to an external trusted endpoint even when
`guardrails.enabled=false`; an empty URL selects the internal service address.
`guardrails.configId` selects the chart's bundled sidecar config;
`council.guardrails.configId` selects the council's requested profile. Keep them
aligned for the internal sidecar. Multi-council catalogs still supply explicit
per-council profiles and endpoints. Network policies and external endpoint
allowlists remain an operator responsibility; allow only intended destinations.
Disabled councils do not depend on sidecar health for startup or requests.
The read-only sidecar filesystem uses a writable runtime volume for temporary
files, home, and caches. On plain Kubernetes set an allowed writable volume
group, for example `guardrails.podSecurityContext.fsGroup=10001`; OpenShift
operators should use the namespace-approved group or SCC assignment instead.

The chart's initial sidecar requests 50m CPU/256Mi memory and limits it to
1 CPU/512Mi. Measure throughput, cold startup, and memory with representative
input before production sizing. Every replica/client has its own concurrency
budget. Treat these resources as starting values, not performance guarantees.

## Assumptions, limitations, and privacy

The bundled `models: []` profile uses local code-injection and context-bloat
rails. It is not general natural-language prompt-injection protection and makes
no detection-rate or false-positive claim. Legitimate prose that contains code
can be blocked. Model-backed profiles require separately reviewed provider,
credential, resource, policy, and data-handling decisions.

The configured sidecar and its profile administrators are trusted. Activated
rail logs are reported structural evidence, not cryptographic attestation of
profile identity or policy effectiveness. Correct rule compilation and loading
must be validated against the pinned server. Screening cannot establish that
all injections were found or all upstream policy actions ran correctly.

Selected prose leaves the council process and reaches the sidecar. Nomos stores
fixed screening reasons rather than raw remote error bodies in this audit
block; this does not make upstream traces, access logs, exception logging, or
other existing audit fields free of prose or sensitive content. Configure
sidecar/provider access controls, encrypted transport where required, log
redaction, retention/deletion, and incident handling for the actual deployment.
Avoid publicly exposing the sidecar. No authentication or provider secret is
embedded in these example URLs or profiles.

## Recovery and verification

This revival selectively recovers feature commit `a06b9d4` and review follow-up
`93b4e8c` onto public develop `5ab4fc89`. Existing dirty worktrees were preserved;
old enabled Kind defaults and unverified benchmark claims were not imported.
Source and profile mirrors follow the current Apache-2.0 project convention.

Runtime regressions cover both routes, OPA short-circuit/order, enforced denial,
shadow/fail-open status handling, empty-prose skips, and legacy audit calls.
The client suite covers protocol validation, bounded transport, channel
selection, and configuration. Offline Compose and Helm renders validate the
default and enabled/external-endpoint variants. Opt-in live tests require
`NOMOS_GUARDRAILS_LIVE_URL`; a skipped live test is not a passed deployment test.
Production profile/team alignment and deployment-specific privacy review remain
open rollout work.
