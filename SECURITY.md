<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Security

NVIDIA is dedicated to the security and trust of our software products and services, including all source code repositories managed through our organization.

**Please do not report security vulnerabilities through GitHub.** If a potential security issue is inadvertently reported via a public issue or pull request, NVIDIA maintainers may limit public discussion and redirect the reporter to the appropriate private disclosure channels.

## Reporting a potential security vulnerability

Use NVIDIA's current private reporting routes:

- Web: [Report a Security Vulnerability](https://www.nvidia.com/en-us/security/report-vulnerability/), which links to the Vulnerability Disclosure Program.
- E-mail: psirt@nvidia.com. Encrypt reports using the [NVIDIA public PGP key](https://www.nvidia.com/en-us/security/pgp-key/).
- Include the product name, affected version/branch or commit, vulnerability type, reproduction instructions, available proof of concept, and potential impact. Send sensitive details through the private route above; remove unrelated credentials and personal data.

Program scope, eligibility, rewards and recognition are governed by the current [NVIDIA reporting policy](https://www.nvidia.com/en-us/security/report-vulnerability/) and [PSIRT policies](https://www.nvidia.com/en-us/security/psirt-policies/). This document makes no Nomos-specific eligibility, reward, response-time or remediation commitment. For all security-related concerns, see [NVIDIA Product Security](https://www.nvidia.com/en-us/security/).

## Scope and support

The implementation description below was reviewed on **2026-11-05** against public **`drobison00/NomosPublic`** `develop` commit **`5ab4fc89f37de21f1489f5d42840bc32e9409f7f`**. It describes that source snapshot and the bundled examples, not a certification or a guarantee about another revision or a deployed installation. Recheck the active code, configuration and deployment when applying it.

The existing repository policy does not publish a supported-version matrix or a security maintenance schedule. This document does not establish one. The [README](README.md) documents Linux deployment support and says native macOS deployment is not currently supported; this is an operating-system statement, not a security-update promise. [CONTRIBUTING.md](CONTRIBUTING.md) currently says the project is not accepting contributions. The review checklist below is for maintainers and authorized reviewers and does not change that policy.

At this snapshot in `drobison00/NomosPublic`, public PRs #1 (command admission), #2 (sensitive-path normalization), #3 (ledger/pending-read authorization), and #4 (ledger integrity) are **open and unmerged**. Their proposed behavior is not part of this baseline. Revisit this dated description when those changes merge or deployment behavior changes. The checklist states what reviewers must establish for affected changes; an unchecked item must not be read as an existing guarantee.

## Assets, principals and trust boundaries

Nomos brokers privileged operations. Its important assets include agent tokens and account permissions; council and executor signing keys; deployment credentials; commands, tool arguments and packaged files; workspaces and reachable infrastructure; and pending workflows, writs, outcomes, ledger records and telemetry.

| Boundary | Current mechanism and trust requirement |
| --- | --- |
| Agent to council | Authorization inputs carry agent tokens. The authenticator resolves enabled accounts and permission scopes; council membership is separately configurable. Protect tokens and review each ingress path, including the separate proxy credential where configured. The gRPC adapter binds an insecure listener and needs an appropriate transport perimeter. A claimed agent ID alone is not authenticated identity. |
| Operator to management services | Council catalog mutations use a management token. Signup creates live accounts; its admin backend uses HTTP Basic authentication and can change privileges. These credentials and provisioning tools confer operator authority, not tenant isolation. |
| Council to policy and models | Local policy and OPA provide deterministic checks; model members and the chairman provide judgments over supplied data. Model credentials, endpoints, prompts and configuration are part of the trusted deployment. |
| Council to executor | A signed writ binds specified authorization fields. The executor verifies the configured council key and applies its own checks before execution. Protect writs as usable authorization artifacts during their validity window. |
| Executor to host and targets | The Docker reference executor launches local subprocesses; OpenShell delegates to its configured runtime. OS/container isolation, mounts, network policy, runtime privileges and target credentials determine what an admitted action can affect. |
| Services to state and evidence | Redis, PostgreSQL, ledger files, Vault and trusted-executor registries hold authorization or audit state. Their administrators, storage permissions and recovery procedures are security-relevant. Monitor, proxy and plugin paths can expose or assert sensitive evidence. |

See the [architecture guidance](.agents/architecture.md), [security guidance](.agents/security.md), [deployment guide](deploy/README.md) and [trusted executor contract](trusted_executor_contract/models.py) for the component boundaries. Review integrations under their own trust model as well as the core protocol.

## Current mechanisms and limits

### Authentication, permissions and read surfaces

Council settings default to `require_auth: true`; this is not a statement that every tool or endpoint authenticates. Agent authorization uses enabled-account token lookup, deterministic permission categorization and, where configured, council membership. Command categorization is heuristic and must not substitute for executable semantics. Preset roles are defined centrally in [permissions.py](nomos/auth/permissions.py); `admin` includes destructive infrastructure scope, and role names do not provide isolation between tenants.

At this base, council ledger-page, ledger-entry and pending-approval read inputs do not carry authentication tokens. Monitor read surfaces also require deployment access controls; monitor administrative tokens do not protect all reads. Health and contract-discovery endpoints are not authorization gates. Restrict reachability of sensitive APIs, proxies, streams and WebSockets according to their actual authentication behavior. Do not infer confidentiality from a write endpoint being protected.

The reference executor compares `X-Agent-ID` to the writ's target. That caller-supplied header is not an independently authenticated transport identity. A network front door must provide any stronger caller authentication and identity binding required by the installation.

Relevant code: [authenticator](nomos/auth/authenticator.py), [council ingress](nomos/nat/functions.py), [executor API](executors/docker/api.py), [monitor](monitor/server/main.py), and [signup](agent_signup/server.py).

### Policy, council decisions and external providers

OPA is enabled in the bundled council configuration and can be disabled or replaced by the operator. The bundled guidance recommends a Data API `result` object with boolean `allow`; the actual parser also accepts a boolean `result` or an object with boolean `allowed`. Configured fail-closed behavior denies errors or undefined decisions. Bundled OPA deployment loads mounted policies at startup; recreate the sidecar when those files change. Local policy, permission classification and OPA do not necessarily inspect the same representation or cover the same operations. In particular, shell-text rules must not be assumed to examine every structured tool argument.

The configured default review threshold is unanimous, with quorum available by explicit configuration. Votes, debate rounds and chairman interpretation are model judgments; agreement is not proof of safety or independence. The public configuration uses one hosted model for multiple roles. Review failure and timeout behavior at the actual orchestration path, including retries and fallback settings, rather than assuming a provider error always has the same outcome in every mode.

Commands, intent descriptions, packaged code and evidence may be included in provider requests. Treat prompts, tool responses, retrieved content and plugin observations as untrusted input. Operators must choose providers, routing, retention and credentials appropriate to the data and workload; do not submit secrets merely because a request is being reviewed. Hosted trial endpoints have quota and availability limits.

Relevant code and configuration: [OPA client](nomos/opa/client.py), [OPA rules](policies/intent_filter.rego), [local policy](nomos/level_1/policy/engine.py), [council orchestration](nomos/nat/functions.py), [public council configuration](configs/council.yaml), and [settings](nomos/config/settings.py).

### Writs, replay and certified evidence

The [writ contract](trusted_executor_contract/models.py) signs canonical bytes including command, working directory, packaged files, target agent, intent hash, time window, nonce, execution limit, council and threshold metadata, and ledger-entry hash. Verify the fields actually covered by canonical serialization; advisory vote summaries are not included in that signed field set. An Ed25519 signature authenticates those bytes relative to a trusted key. It does not prove that a command is safe, that its intent is truthful, that files on disk are unchanged, or that execution occurred.

The Docker enforcer checks signature and time validity and atomically claims a Redis nonce before local command/path checks and execution. A writ is consumed after that claim even if a later local check fails. Replay protection depends on all relevant executors sharing the appropriate nonce store and namespace, usable Redis state and trusted clocks. Redis loss, reset, restoration or a changed namespace must be considered during recovery; do not assume in-memory or independent stores provide cluster-wide or restart-persistent single use. A claim followed by a crash does not establish exactly-once successful execution or recover the result.

Executor result certification is separate from writ approval. It binds a result to the configured executor identity and signed evidence fields. Verification relies on the trusted-executor key registry and the honesty of the signing executor. A plugin registered as a trusted executor can attest its own observations; that is not independent execution or proof that tool output is true. Protect, provision, rotate and revoke these keys and trust records deliberately. The bundled council accepts raw, uncertified agent evidence by default (`allow_uncertified_evidence: true`); do not assume all evidence shown to reviewers has a verified executor signature.

Relevant code: [writ factory](nomos/auth/writ_factory.py), [enforcer](executors/docker/enforcer.py), [nonce store](executors/docker/nonce_store.py), [certification](executors/common/evidence.py), [key providers](nomos/crypto/signer_provider.py), and [Relay integration](integrations/nemo-relay/DESIGN.md).

### Execution and filesystem boundaries

The reference enforcer applies configured working-directory restrictions, blocked patterns, tool and argument rules, denied paths and packaged-file handling. An empty `allowed_tools` list disables that allowlist; it does not deny all tools. Some commands use shell interpretation. Parsing, regular expressions, allowlists and workspace checks reduce admitted operations; they are not an OS sandbox and cannot establish complete shell or filesystem confinement. The current command and sensitive-path implementations are subject to the unmerged changes noted above.

The signed command and packaged bytes must correspond to what is executed, but external binaries, PATH resolution, environment, interpreters, referenced files, symlinks and concurrent filesystem changes remain material. A signature over a path does not pin its later contents. Operator control of executable search paths, writable workspaces, runtime mounts and target credentials affects this boundary.

Treat the bundled executors as getting-started implementations, as stated in the [security guidance](.agents/security.md). The deployment must supply appropriate isolation, restricted privileges, resource limits, network access and separation of workloads. Do not run mutually untrusted tenants under an assumed application-only isolation boundary.

Relevant code and deployment: [Docker executor](executors/docker/enforcer.py), [OpenShell executor](executors/openshell/enforcer.py), [executor profiles](deploy/executor.yaml), and [Compose deployment](deploy/docker-compose.yml).

### Ledger, persistence and recovery

Ledger entries are hash linked and can be signed when a signer is configured. At this base, `LedgerChain.validate()` recomputes entry hashes and checks previous-hash links; it does **not** verify entry signatures or establish a trusted signer, complete history, contiguous sequence numbering or an external checkpoint. Ledger canonical content excludes metadata, so its hash/signature coverage must not be assumed to include every displayed field. Do not describe a successful chain check as authenticated, complete or immutable history. An administrator able to replace state and its local anchor is outside what internal hash links alone can detect.

Storage mode matters. In-memory state does not survive restart; file ledgers need protected storage and an appropriate writer model. PostgreSQL ledger storage uses transactional append coordination, and PostgreSQL workflow storage provides a durable write-through cache with last-writer-wins semantics, not a distributed review engine. That does not make every operation, side effect and audit write one transaction. Redis-backed and service deployments distribute durable and transient responsibilities differently. Review accounts, token indexes, pending workflows, writs, outcomes, nonces and ledger records separately when planning failover or recovery.

Audit and outcome reporting can fail or be best effort on particular paths; the writ factory can return a signed writ after a persistence warning. Neither a ledger entry nor a missing entry alone proves the full external execution outcome. Backups, access controls, storage durability, recovery validation and any independent audit retention are operator responsibilities.

Relevant code and documentation: [ledger chain](nomos/level_1/ledger/chain.py), [file storage](nomos/level_1/ledger/storage.py), [PostgreSQL ledger](nomos/level_1/ledger/postgres_storage.py), [workflow store](nomos/store/workflow_store.py), [storage schema](nomos/store/SCHEMA.md), and [deployment setup](docs/deployment-setup.md).

### Deployment and operator assumptions

The bundled deployments include development conveniences and must be reviewed as concrete profiles. The raw Compose deployment defaults to the `dev` profile, and the primary deployment script defaults to the OpenShell executor profile. Development preflight performs no checks; `prod` and `production` are aliases for the selected `alpha` checks, not a production-security certification. The README distinguishes the primary Compose Redis path from Kind service mode and the separate service-mode bootstrappers. Do not transfer a port, credential, persistence or signing assumption from one to another.

Keep runtime environment files mode `0600` and credentials out of source, logs, generated reports and browser storage. Replace example credentials and protect Redis/PostgreSQL, Vault access, signing keys and management/admin tokens. Development Vault and bootstrap credentials are not production key-management arrangements. Raw Compose defaults include wildcard host bindings, while bootstrap helpers can generate loopback settings; Helm network policy is disabled by default. Verify the rendered Compose or Helm configuration, bindings, ingress/TLS, mounts, container users, capabilities and network rules for the selected profile; a declared network policy requires a runtime that enforces it.

The current signup console installs Basic-auth middleware on all paths except `/healthz`, with separate council-administration credentials. The older guidance in `.agents/security.md` describing an unauthenticated signup path does not match this implementation. Keep the console loopback-bound or behind suitable front-door authentication and transport protection; constrain signup roles deliberately and replace development credentials. Its account administration can change privileges. Monitor and demo UIs, proxying, metrics, streaming, ledger payloads and execution output may reveal operational data and need reachability and retention controls.

Trusted operators and infrastructure administrators can change code, policy, prompts, model endpoints, account privileges, keys, storage and runtime isolation. The application does not isolate a deployment from its own privileged administrators. Compromised hosts, signing services, providers, plugins and target systems require defenses at their respective boundaries.

### Availability and resource budgets

Input validation, ledger-page caps, pending-work limits, provider concurrency controls and execution timeouts apply at particular layers. The [endpoint limiter](nomos/llm/limiter.py) shares provider/base-URL concurrency within one process, not across all replicas. These controls do not establish a universal request-rate or workload budget. Operators must bound exposed ingress and size provider, storage and executor capacity for their deployment; reviewers must verify which limits are enforced before expensive work and which reset or multiply across workers.

## PR security review checklist

Apply the relevant sections to every authorized change affecting behavior, configuration, dependencies, integrations or deployment. Mark an item complete only with evidence; explain any `N/A` by naming the unaffected boundary. These are review obligations, not assertions that all invariants already hold in the pinned base.

### Scope, entry points and authorization

- [ ] Identify changed assets, principals, trust boundaries and attacker-controlled inputs. Trace every affected public entry point, adapter, alternate approval path and internal caller to its authorization decision and side effect.
- [ ] Demonstrate missing/invalid credentials, disabled accounts, insufficient scopes, non-members and cross-council access are rejected where protection is required. Include a minimally privileged permitted control. Check read APIs, catalog mutations, pending queues, ledger access, proxies, streams and WebSockets separately.
- [ ] Separate agent tokens, management tokens, signup admin authority, executor identity and trusted-executor registration. Do not treat supplied IDs/headers as proof of identity or infer tenancy boundaries from roles.
- [ ] Establish that denial, malformed input and unavailable authorization dependencies cannot mint usable approval or reach protected effects. Record expected audit/telemetry effects separately and verify no unauthorized command, file materialization or external tool invocation occurs.

### Representation, policy and council

- [ ] Trace the exact command or structured operation through parsing, canonicalization, permission categorization, OPA input, model review, signing and execution. Cover duplicate/ambiguous JSON fields, unknown operations and schema/version mismatches. Verify the reviewed and executed identities match.
- [ ] For command/path changes, test normalization, relative and absolute paths, traversal, symlinks, glob bounds, interpreter/wrapper behavior and environment/search-path assumptions using harmless fixtures. Include permitted controls and account for filesystem changes between check and use (TOCTOU).
- [ ] Show that local policy and OPA see the fields their rules require, including packaged content and structured arguments where applicable. Test explicit denial, malformed/undefined responses, service errors, timeouts and configured fail-open/disabled modes; document any change to defaults.
- [ ] Verify unanimous/quorum thresholds, member eligibility, veto/denial semantics, chairman overrides, debate/retry limits and partial or failed reviews on the affected path. Include deterministic tests with controlled model responses; live agreement is not a safety proof.
- [ ] Review prompt injection, untrusted evidence labels, provider data exposure and model/configuration changes. State which provider and plugin assertions are trusted and what is independently verified.

### Writs, executor and evidence

- [ ] Show canonical signed fields bind the intended operation, arguments/digests, target, cwd, packaged bytes, lifetime and nonce. Reject tampered fields, invalid/untrusted keys, wrong targets and expired/future writs; include a valid permitted writ.
- [ ] Check signer provisioning, public-key distribution, rotation/revocation and trust registration. Do not let a requested council or key identifier silently select unauthorized trust.
- [ ] Demonstrate atomic single-use claims under concurrent delivery and shared-store deployment. Test store failure, restart/reset, retry after claim, lost response and recovery without assuming exactly-once effects. Preserve consumed-claim semantics unless deliberately changed and documented.
- [ ] Establish rejection occurs before materialization and execution. Review path resolution, existing files/symlinks, permissions, executable selection, shell invocation, environment, timeout/resource limits, mounts and runtime/network privileges.
- [ ] Verify result signatures, request/writ binding, output digests and trusted-executor lookup. Keep plugin observations, reported outcomes, unknown results and independently executed results distinct. A signed report is not proof of content truth.

### Ledger, state and operational exposure

- [ ] Review accounts, pending workflows, writs, outcomes, nonces and ledger state across workers, cancellation, concurrency, restart, migration, backup and restoration. Identify what is durable and where transaction boundaries leave uncertain outcomes.
- [ ] Test changed ledger integrity checks with altered entries, signatures, links, sequence gaps/reordering and truncation as relevant, plus an intact control. State the trusted key/checkpoint assumptions and what verification cannot establish. Never infer completeness from a self-consistent local chain.
- [ ] Review rendered defaults and example configurations for host bindings, ingress/TLS, signup roles, management/admin access, storage exposure, mounts, container privileges and effective network enforcement. Keep development profiles clearly distinguished from production arrangements.
- [ ] Check input/body, structured-argument, attachment count/bytes, glob expansion and ledger-page bounds; admitted parallelism and queues; model-call, token, debate and retry budgets; and cancellation/timeouts. Exercise oversized/budget-exhausted requests and a permitted control. Identify per-process versus shared limits and any missing ingress/rate controls without assuming a configuration field is enforced.
- [ ] Inspect logs, errors, telemetry, metrics, streams, ledger payloads, reports and UI output for tokens, private keys, sensitive commands/content and cross-user data. Verify redaction and read authorization on each affected route.
- [ ] Review dependency/lockfile changes and action/image provenance, immutable pins and update paths. Inspect secret-scan baseline additions and integration trust; keep real credentials out of examples and generated artifacts and justify scanner suppressions.
- [ ] For CI/build changes, inspect workflow events and token permissions, fork/untrusted code execution, protected-secret access, and artifact/cache trust across jobs and runs. Confirm privileged contexts do not consume untrusted code or outputs without an explicit trust decision; do not infer branch protection or secret restrictions from the workflow alone.

### Evidence required for the review decision

- [ ] Attach the reviewed commit/configuration, affected boundaries, tests and exact commands, prerequisite services, executed counts, failures, skips and unrun checks. Pair meaningful denial/failure cases with permitted controls; do not count skipped tests as passed.
- [ ] Run focused tests for the changed mechanisms and integration tests across changed boundaries. Broaden to the repository checks appropriate to the change. Document residual risks and unavailable infrastructure; green CI does not prove the deployment safe.
- [ ] Recheck this policy and operator docs when an entry point, role, signed format, storage/recovery behavior, provider trust or deployment default changes. Route potential vulnerabilities privately before publishing sensitive regression details.

## Validation references and prerequisites

Use the tests nearest the affected mechanism. Examples already present in the repository include:

| Mechanism | Existing tests to inspect and run as applicable |
| --- | --- |
| Identity and permissions | [authenticator](tests/test_authenticator.py), [permissions](tests/test_permissions.py), [signup auth](tests/test_agent_signup_auth.py), [account integrity](tests/test_account_integrity.py) |
| Admission, OPA and council paths | [policy paths](tests/test_policy_paths.py), [OPA client](tests/test_opa_client.py), [OPA integration](tests/test_opa_nat_integration.py), [chairman preauth](tests/test_chairman_preauth.py), [tool authorization](tests/test_tool_call_authorization.py) |
| Writ and executor | [writ factory](tests/test_writ_factory.py), [enforcer](tests/test_enforcer.py), [executor API](tests/test_executor_api.py), [nonce store](tests/test_executor_nonce_store.py), [OpenShell](tests/test_openshell_executor.py) |
| Ledger and persistence | [ledger](tests/level_1/test_ledger.py), [concurrency](tests/test_ledger_concurrency.py), [PostgreSQL ledger](tests/test_postgres_ledger.py), [durable workflows](tests/test_workflow_durable.py) |
| Exposure and integrations | [Compose security](tests/test_deploy_compose_security.py), [monitor](tests/test_monitor_api.py), [deployment preflight](tests/test_deployment_preflight.py), [Relay security](integrations/nemo-relay/tests/test_security.py) |

These references identify existing coverage, not proof that every checklist item is tested. Add focused regressions for new invariants. A docs-only policy review can validate claims, links and diff scope without running the application suite; report that limit explicitly.

The [test runner](scripts/run_test_suite.py) and [CI workflow](.github/workflows/ci.yml) define different suite selections. For example, the runner supports:

```sh
uv run python scripts/run_test_suite.py --profile unit
uv run python scripts/run_test_suite.py --profile integration
uv run python scripts/run_test_suite.py --profile live --require-deployed
```

Use disposable test services and the repository's setup instructions. Redis tests use `NOMOS_TEST_REDIS_HOST`, `NOMOS_TEST_REDIS_PORT`, `NOMOS_TEST_REDIS_DB`, `NOMOS_TEST_REDIS_PREFIX` and `NOMOS_TEST_REDIS_PASSWORD`; database/prefix cleanup must never target production. PostgreSQL tests use `NOMOS_TEST_DATABASE_URL`, require the PostgreSQL extra/driver and a disposable database, and truncate Nomos tables. See the [Redis helper](tests/redis_test_support.py) and [PostgreSQL helper](tests/postgres_test_support.py). Those tests can skip when dependencies are unavailable.

OPA rule tests require OPA (CI runs the pinned OPA container); mocked OPA tests do not prove a live sidecar loaded the policy. Deployed suites require the relevant running council/monitor/executor stack, Redis/OPA/storage/signing services for its profile, and configured inference endpoints for live authorization. The runner lists per-suite variables, including `NOMOS_GRPC_SMOKE_ADDR`, `NOMOS_MCP_LIVE_URL` and `NOMOS_AUTH_TOKEN`. Dynamic mutation tests are opt-in and require a management token and `NOMOS_MCP_LIVE_MUTATE`; use an isolated test deployment.

The `all` runner profile omits the opt-in mutation suite and does not inventory every test file. CI explicitly excludes three deployed test files from its local pytest step, and its package/image build jobs are disabled at this base. Check the actual selections and reported skips before claiming coverage. Even `--require-deployed` does not turn every conditional skip inside a test into executed evidence. Capture live prerequisites and outcomes separately; do not report a live path as validated when only local, mocked or skipped checks ran.

## Triage guidance

Report a suspected boundary failure privately when an untrusted or insufficiently privileged principal may gain execution, disclosure, approval, evidence authority or access to protected state. Include the affected revision, configuration, prerequisite access and practical impact so PSIRT can assess it. Do not require a completed exploit before reporting a credible concern.

Defense-in-depth improvements, model-quality changes and operator-hardening suggestions need context: identify which implemented boundary is crossed, which deployment assumptions hold, and whether a trusted administrator is already required. Do not automatically classify all such changes as vulnerabilities or dismiss them solely as configuration issues. CVE applicability, severity, affected products, disclosure and remediation are determined through the current NVIDIA process; this checklist does not assign them.
