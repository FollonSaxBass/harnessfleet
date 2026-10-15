# Remaining work toward v0.1

This is an implementation backlog against the [specification](specification.md),
not a claim of native qualification. The [support matrix](support.md) remains the
capability contract. Whole-product review and deterministic regressions cover the
current implementation; they do not certify disabled adapters or external providers.

| Work item | Stories | Acceptance evidence |
| --- | --- | --- |
| Protect the registry from native workers | 14, 22, 34–38 | Move state mutations behind a controller-owned service with authenticated, scoped delegation requests and per-run artifact access. A workspace-write native worker must neither read other run/controller tokens nor change another run's grant, receipt or generation. Current same-user state access assumes trusted workers and is a pre-existing v0.1 security gate. |
| Native lineage sources and exact caller attribution | 40–43, 47–48, 51 | Opt-in sources for qualified native versions; a mixed native/managed recursive tree attaches a managed child to its exact native caller and generation. Reused sessions, forks, source loss, collisions and unknown callers remain explicit. Cursor transcripts currently observe immediate children without proving complete ancestry or binding managed runs. |
| Cursor and DSH native execution profiles | 3, 16, 60, 64–65 | Qualify the configured account, billing mode, model, transport and native versions through launch, verified output, authentication failures, ownership and crash recovery. Doctor must identify missing prerequisites without invoking a model. No provider or billing fallback. |
| App and Antigravity execution/attachment | 16, 50, 61, 64–65 | A lease/ownership backend refuses occupied or unsupported sessions, records explicit attachment and proves safe continuation, controller loss and cleanup. Observation alone never creates control. Current app and Antigravity adapters stay disabled. |
| Built-in quota recovery and account snapshots | 30–31, 53 | Account-specific readiness proof, separate quota/authentication classification, finite continuation budgets, stable run identity and weekly-limit refusal where applicable. Built-ins currently refuse automatic recovery; plugins can implement the existing contract. Quota/cost snapshots remain optional. |
| Native Linux amd64 Boxes | 35–38, 57–59 | Dedicated accounts, explicit mounts, a real local daemon/image and crash/acknowledgement/replacement tests with unrelated-container sentinels. Deterministic Docker fixtures alone do not qualify a native combination. Box-origin delegation remains refused. |
| Additional platforms | 35–38, 58, 65 | Separate process ownership and reconciliation proofs for macOS; separate Docker Desktop and arm64 qualification. Native Windows remains outside v0.1 scope. |
| Skill selection and integration scenarios | 6, 8–13, 21, 70 | Agent-level cases distinguish required sets from allowed sets, clarify ambiguity and integrate verified results without human CLI steps. The recorded Codex↔Claude native challenge remains bounded to the versions and permissions in the support matrix. |
| Certified release and later workspace adoption | 67–69, 72 | Build/audit reproducible archives, qualify claimed native combinations, publish versioned checksums and complete recovery docs. Apply the compatibility contract through separately reviewed private wrappers. Installation and qualification must preserve existing commands, accounts and services. |

Lifecycle review identified and regression-tested healthy-controller recovery,
per-target batch accounting, controller identity/token isolation and Codex
workspace-write continuation. Observation regressions cover stale Cursor head replies
and refresh after disconnecting an expanded source. These are corrections to current
behavior, separate from the feature work above.
