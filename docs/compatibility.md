# Later workspace migration

The source workspace continues to operate independently. No manifest, command
binding, service, private runtime registry or native configuration is migrated by
installing HarnessFleet.

Private wrappers may translate their Route manifest to contract-v1 configuration,
map their legacy command arguments to `harnessfleet run`, and consume public JSON or
`RunManager`. Their review governance, claims, tickets, task records, handoffs and
provider-specific account policy remain outside the product. Optional generic
connectors use user-owned snapshots and start empty.

Run start/follow/output/status/list/stop/resume/keep semantics remain recognizable.
The new core uses explicit exact Route ids and models, per-generation receipts,
structured grants and required/allowed selection. Legacy `--harness/--account` route
resolution, `--billing any`, workspace-relative registries and private import paths
are not public compatibility promises. Public storage is not frozen.

`harnessfleet integrate --compatibility --directory DIR` creates an `agent-run`
forwarder only when no `agent-run` executable or destination already exists. It
preserves foreign executables and bindings. No `hf` executable is installed.

Native store schemas, Box images, Docker Desktop, arm64, macOS process ownership,
app attachment and quota recovery need explicit qualification before wrappers
adopt them. The support matrix records current evidence. Never replace a private
workspace tool merely because a Route is discovered or a run id is allocated.


Normalize records at the wrapper interface, not by assuming private persistence
matches public JSON. For example, a legacy launcher may report a Route as
`{"route":{"id":"box:example"}}`; public HarnessFleet Agent Runs expose
`route_id`. A collector validation error must retain already-produced output and
ownership receipts rather than repeat the native invocation. Managed HarnessFleet
clients now use the scoped transport; private wrappers must not grant registry
writes to restore their old direct-storage behavior.
