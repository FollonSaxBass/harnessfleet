# Public contract v1

The installed CLI and Python `RunManager` share one execution contract. JSON records
carry `schema_version: 1`. Internal SQL tables, file layout and tokens are private.
Errors are `{ "schema_version":1, "error":{"code":"...","message":"..."} }`.
CLI errors exit 2; unsuccessful followers and incomplete batches exit 1; successful
operations exit 0. `--version` is plain text.

## Execution

```python
from harnessfleet import RunManager

manager = RunManager(config_path="/your/config.json", state_path="/your/state")
run = manager.start("codex-local", "Check the public contract", cwd="/your/project",
                    model="YOUR-MODEL", permission="read-only")
final = manager.wait(run["id"], timeout=120)
output = manager.output(run["id"])
assert final["status"] == "finished" and output["verified"]
```

Other methods: `routes(diagnose=False)`, `list(offset=0,limit=50)`, `status(id)`,
`start_many(targets,assignment,selection=...,**options)`, `cancel(id)`, `stop(id)`,
`dispatch()`, `recover(id=None)`, `resume(id,assignment,expected_generation=None)`,
`keep(id,value=True)` and `prune()`.

`start` options include `delegation="leaf"|"required"` with `leaf` as default,
billing, permission, model, cwd, queue, structured selection
and finite `auto_resume`. An exact target is `{ "route":"R", "model":"M" }`.
Required sets use `selection.required`; allowed sets use `selection.allowed`.
An absent or `null` constraint is unspecified; a non-null constraint must be a list
of resolved targets. Inherited authority still applies when a constraint is unspecified.
`start_many` returns every target with either `run` or an explicit `error` and an
`all_started` flag. Native unavailability may be discovered by the asynchronous
worker after admission; followers/output remain the final authority.
Malformed target members receive `selection` errors without hiding valid members'
admitted run ids. Malformed explicit selection constraints reject the entire request
before admission. A failed batch is not permission to repeat its successful members.

Public Agent Run fields include stable `id`, current `generation`, lifecycle `status`,
Route/harness/environment/account/billing/model/permission, cwd, derived identity,
execution `parent_run` and `parent_generation`, managed depth, native `session_id`,
caller when attributed by the graph, timestamps, `invoked`, `accepted`,
`cleanup_confirmed`, failure code, quota reset hint, output digest, keep flag and
remaining automatic cycles. Assignments, grants, credential references and tokens
are never in public execution records. Native session identifiers are not credentials.

Records also expose `delegation` and the selected `delegation_transport`. Routes
report configured transport and native qualification separately from executable/login
readiness. A successful native login does not qualify delegation IPC. Required
delegation without the separately enabled transport fails before admission.

Statuses are `queued`, `starting`, `running`, `stopping`, `finished`, `failed`,
`quota`, `authentication`, `stopped`, `cancelled`, `rejected` and `uncertain`.
Uncertain records retain capacity. `invoked` is a durable consumption marker set
before the native process call; it does not prove that a provider accepted a model
request. A failed or absent receipt never permits replay.

`output` returns id, generation, verified and text. The default verifies terminal
state, cleanup, per-generation receipt and SHA-256. `partial=True` exposes unverified
evidence. Bounded reads reject links, special files and traversal.

## Events and graphs

`run events ID --after SEQ` returns monotonically numbered execution events,
generation, timestamp, kind and a bounded structured detail. Progress contains event
kinds, not raw tool payloads. Supervisors persist progress asynchronously in ordered
batches, so live progress and native session updates can lag their source events.
Successful completion flushes every submitted event and session update before
publishing the terminal record. Failure/cancellation can discard pending progress;
a supervisor crash can lose its uncommitted suffix. The terminal `completed` event
reports `progress_settled` separately from the run's original failure code. An
unsettled writer retains uncertain cleanup and capacity without replacing that
cause or publishing accepted output. Durable invocation/ownership
records still prohibit replay. Full native
JSONL is retained separately as bounded, redacted evidence when supervision finishes.
Follow blocks on Linux notifications; a lost supervisor
is reconciled without re-invocation. Timeout leaves execution running.

`Graph(manager).page(parent=None,offset=0,limit=50,query="")` is also exposed by
`GET /api/graph`. It returns nodes, total, next_offset, source status and issues.
Each node declares available_children and leaf_confirmed. An incomplete source is
not a confirmed leaf. All available descendants remain accessible through expansion
and pagination. A filter includes necessary ancestors. `Graph.snapshot()` returns
the complete available normalized graph for Python consumers.

Edges distinguish `authority`, `causal` and `managed_parent`. The latter states
execution parentage with unknown exact caller; it is not inferred native parentage.
Native keys incorporate source, harness, environment, account, identifier and optional
generation occurrence. Cycles and duplicates produce explicit issues. Generation
history is never moved to a later reused session.

`GET /api/output?id=NODE` returns verified managed output or an unverified observed
native reply. Public graph pages do not embed replies. Native observation cannot
launch or attach execution. HTTP control requires loopback Host, same Origin and a
session CSRF header. The local API has no CORS and is not a multi-user security boundary.

`cursor-transcripts-v1` is an opt-in observation source for an explicitly configured
project directory. It produces partial native session/subagent nodes and latest
observed replies; it does not bind managed generations or grant control. Missing
or unreadable transcripts and read-budget limits produce explicit issues while
preserving navigable nodes. See [configuration](configuration.md#cursor-transcript-observation).

`connectors KIND` returns metadata items with redacted string fields: `id` and
`parent` up to 512 characters, `label` up to 200, and `kind`, `status` and
`completeness` up to 80. Replies and undeclared fields are excluded. Invalid items
are omitted with `connector_record` issues and partial completeness. Source files
retain the existing 16 MiB read limit.

Only the current major contract version is supported. Compatible additions may
add optional fields; consumers should ignore fields they do not recognize. A breaking
meaning or field change requires a new contract version. Plugins declare version 1.


Managed host clients use [scoped delegation](delegation.md) with the same CLI and
Python method names, child visibility and durable mutation request ids. `events`
is available through both interfaces. Route records declare `execution_boundary`
separately from adapter capabilities: scoped transport does not certify isolation.

## Managed stdio binding

`harnessfleet managed-stdio` is a capability-only optional binding of execution v1.
It accepts no instance configuration or state override, never opens Store and
requires supervisor-issued context. Helper stdout contains bounded MCP messages;
startup refusal writes only a fixed stderr diagnostic and exits 2.

Its `run` tool uses `{operation, arguments, request_id?}`. `arguments` are the same
public RunManager keyword arguments; `follow` maps to `wait`. Mutations require a
caller-supplied 32-hex ID before dispatch. JSON-RPC IDs correlate protocol messages
and do not authorize replay. A tool response contains the v1 result/error envelope
as JSON text, with `isError` on failure. No arbitrary argv or shell executor exists.

An additive public run field `requesting_actor` is null or contains bridge kind,
opaque instance `id`, originating `run` and `generation`, `exact_native_caller: null`
and `completeness: "partial"`. Raw process identities and capabilities stay private.
This metadata does not create an exact-native-caller causal edge. Historical
versions retain their generation-specific attribution across continuation.
