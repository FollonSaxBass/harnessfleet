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

`start` options include billing, permission, model, cwd, queue, structured selection
and finite `auto_resume`. An exact target is `{ "route":"R", "model":"M" }`.
Required sets use `selection.required`; allowed sets use `selection.allowed`.
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
kinds, not raw tool payloads. Follow blocks on Linux notifications; a lost supervisor
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
