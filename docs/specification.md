# HarnessFleet product specification

The intended product is independently installable, open-source, local and single-user.
Its primary workflow is an agent in one harness delegating bounded work to agents
in other harnesses, collecting verified results and continuing autonomously within
an approved policy. The implementation repository is `FollonSaxBass/harnessfleet`;
coordination lives in the existing GitLab tracker. MIT licensing applies to the
clean extraction. This specification describes the requested destination, not a
claim that every feature or platform is certified in the alpha.

## Required user outcomes

1. Control a local instance's accounts, policy and data.
2. Install independently of the originating workspace.
3. Receive prerequisite and native authentication guidance.
4. Diagnose readiness without invoking models.
5. Try a synthetic demonstration without credentials.
6. Install one common skill with selected harness bindings.
7. Discover structured Routes, capabilities, billing and support levels.
8. Require a particular model or harness/model pair.
9. Require several targets with explicit participation or failure for each.
10. Restrict selection to an allowed target set.
11. Let the orchestrator choose targets when the user leaves selection open.
12. Clarify ambiguous participation before launching.
13. Report forbidden/unavailable targets without silent substitution.
14. Preauthorize autonomous delegation through policy.
15. Authorize billing explicitly without switching accounts or providers.
16. Launch one or several agents across harnesses.
17. Derive Agent Identity from execution facts.
18. Pass bounded assignments and explicit references, never whole conversations.
19. Let the caller provide and prepare its working directory.
20. Match authorized permission requests to actual native capabilities.
21. Allow further host-origin delegation when authorized.
22. Preserve or narrow inherited authority across descendants.
23. Distinguish Native Subagents from managed Agent Runs and their depth limits.
24. Refuse immediately when capacity is full unless queuing was requested.
25. Persist optional per-Route queues without premature native invocation.
26. Cancel queued work before dispatch.
27. Revalidate queued policy and capabilities before invoking a model.
28. Follow execution using events or blocking followers.
29. Separate verified final output from partial progress.
30. Distinguish quota, authentication and execution failures.
31. Bound explicit automatic continuation after proven quota recovery.
32. Preserve Agent Run identity across continuation generations.
33. Retain each generation's result and evidence.
34. Fence obsolete supervisors from updating newer generations.
35. Signal only demonstrably owned processes.
36. Retain capacity and explicit uncertainty when cleanup is not proven.
37. Reconcile evidence after controller/daemon loss without forgetting ownership.
38. Never automatically replay consumed or ambiguous invocations.
39. Retain active, kept and uncertain evidence despite retention expiry.
40. Show managed execution and connected Native Subagents together.
41. Attach managed delegations to their exact attributable native caller.
42. Namespace identities by source, harness, environment/account and native id.
43. Preserve generation-specific native associations across reuse and forks.
44. Report duplicates and cycles explicitly.
45. Make every available descendant reachable through expansion and pagination.
46. Preserve necessary ancestors when filtering.
47. Distinguish missing, unsupported, expired, partial, unreadable and empty sources.
48. Enable native-session observation only for explicitly connected sources.
49. Keep observation read-only and separate from execution grants.
50. Require explicit supported attachment before controlling native sessions.
51. Read available native replies and managed outputs from their nodes.
52. Preserve selection, focus and expansion across dashboard refreshes.
53. Offer optional account-specific quota/cost information.
54. Offer optional user-configured notifications.
55. Provide initially empty tasks, handoffs, ownership and documentation connectors.
56. Limit Git inspection to configured projects.
57. Run Boxes using dedicated accounts and explicit mounts.
58. Tie container effects to recorded daemon and container ownership receipts.
59. Refuse Box-origin managed delegation in v0.1.
60. Make DSH OpenAI-compatible transport, models and provider profiles configurable.
61. Attach/continue only supported, available and unoccupied conversations.
62. Allow public adapter plugins without core changes.
63. Version public records and interfaces independently of persistence.
64. Declare actual adapter capabilities and reject unsupported modes before effects.
65. Declare support per platform/capability, requiring experimental opt-in.
66. Preserve existing commands during installation.
67. Exclude all private data and retain required provenance/license notices.
68. Document a later private-wrapper compatibility contract.
69. Preserve the current workspace, account state, command bindings and services.
70. Prove real Codex-to-Claude and Claude-to-Codex skill-driven delegation.
71. Run deterministic tests without operator accounts or production state.
72. Publish reproducible GitHub source, wheel, sdist, checksums and documentation.

## Decisions and boundaries

- Python package, CLI `harnessfleet run`, common skill and loopback dashboard on port
  8421. No `hf`. `agent-run` only through explicit non-overwriting activation.
- Independent user-owned configuration/state and credential references. Native CLIs
  remain external prerequisites. No automatic CLI installation or credential migration.
- Separate execution core, adapters, lineage collectors, projection and connectors.
  One versioned CLI/Python `RunManager` contract; storage is private.
- Seven named adapters: Codex CLI/app, Claude Code, Antigravity CLI/app, Cursor Agent,
  DSH. Support varies independently for launch, attachment, continuation, recovery,
  permissions, output and native lineage. Plugins use `harnessfleet.adapters`.
- The orchestrator interprets selection. The core validates resolved exact targets,
  required versus allowed sets, policy and inheritance. No role-based routing engine.
- Subscription is the default; Metered requires explicit selection and policy grant.
  No fallback in model, harness, provider, account or billing.
- Grants cover Routes, billing, permission modes and finite limits. Descendants may
  narrow but never broaden them. Default read-only where enforceable; unsupported
  restrictions fail before execution. Native tool restrictions are not OS containment.
- At most two managed Agent Run levels initially; native subagent depth is separate.
  No automatic worktree creation, snapshotting or cleanup.
- Stable run id, generation-specific supervisor/receipt/output/authorization, fenced
  late publishers and no automatic replay of consumed or ambiguous invocations.
- Immediate capacity refusal unless persistent queue requested; cancellation and
  dispatch-time revalidation precede native effects. Controller independent of UI.
- Explicit finite quota recovery, capability-gated for the exact account. Successful
  continuation and proven quota recovery are distinct capabilities.
- Cleanup needs ownership evidence, with uncertainty retaining capacity. Reconcile
  controller loss rather than assuming termination. Linux pidfds first; macOS needs
  independent ownership, crash and recovery proofs.
- Initial retention: transcripts/events three days, output fourteen days. Compact
  execution/lineage metadata separate; active, kept and uncertain records protected.
- Authority parent and exact causal caller are distinct. Namespace native identities;
  bind reused/forked sessions to generations. Never infer certain lineage from cwd,
  process proximity or declared depth.
- Lazy expansion/pagination without silent truncation. Filters retain ancestors.
  Source capability, availability and completeness remain explicit.
- Native observation is opt-in/read-only; attachment requires separate supported
  control admission. No automatic native-session takeover.
- DSH transport is configurable OpenAI-compatible; provider normalization belongs
  in separately qualified profiles/extensions.
- Linux amd64 local-daemon Boxes qualify first; dedicated accounts, explicit mounts
  and daemon/container receipts. Docker Desktop and arm64 need separate proofs.
  Box workers cannot originate managed delegation in v0.1.
- Optional notifications/connectors start empty. Exclude Second Brain entirely.
- Extract a tracked snapshot without original history or uncommitted changes. No
  personal knowledge, real tasks, handoffs, ownership data, transcripts, credentials,
  runtime registries, account data or configuration copies in public artifacts.
- Native Windows process management, universal provider certification, shared
  multi-user service and mandatory MCP are outside v0.1. Arbitrary daemonized
  descendants cannot be contained without a backend that can prove ownership.
- PyPI is not a first-release prerequisite.

## Verification contract

Use the installed CLI as the primary seam; fake native executables and synthetic
credentials/services/stores substitute only at external boundaries. Python/plugin
bindings share the public contract. Graph/API/browser tests cover mixed recursive
lineage, collisions, reuse, generations, cycles, partial sources, deep/large trees,
filter closure and refresh state. Test selection, policy, inherited grants, billing,
permissions, pre-effect refusal, capacity, queue/cancel races, dispatch revalidation,
crashes and duplicate-launch prevention. Test quota/auth classification, bounded
recovery, stable identity, per-generation output and stale-supervisor fencing.

Test PID reuse, supervisor death, late/detached descendants, uncertain cleanup and
unrelated sentinels. Container tests cover daemon replacement, missing creation
acknowledgements, inaccessible mounts and unrelated containers. Test output gates,
bounded reads, traversal, links, malformed records and public redaction. Verify
installation outside Git, offline demo, missing prerequisites, coexistence and
integration in temporary homes. Audit repository, source archive, wheel, fixtures
and screenshots for excluded content.

Native acceptance requires both Codex CLI → Claude Code and Claude Code → Codex
CLI. Each parent launches, awaits and integrates the child's verified result through
the common skill without intermediate human commands. Record exact native versions
and platform; simulated protocol success does not certify native compatibility.
Publish versioned GitHub release artifacts with checksums, quickstart, skill,
support matrix, plugin guide, recovery documentation, glossary and ADRs.
