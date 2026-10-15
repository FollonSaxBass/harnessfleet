# Adapter plugins

Distributions register a factory in the `harnessfleet.adapters` entry-point group:

```toml
[project.entry-points."harnessfleet.adapters"]
my_harness = "my_package.adapter:MyAdapter"
```

Plugins are trusted local Python code. They can invoke processes and services and
are not a sandbox. They cannot override a built-in id; duplicate ids, unsupported
versions and missing contract methods fail discovery explicitly.

A factory returns an object with `contract_version = 1`, a stable `id` and
`Capabilities`. Implement:

- `diagnose(route) -> dict`: readiness without model invocation or credential output.
- `environment(route, ambient) -> dict`: credential references and exact-account selection.
- `command(run, directory) -> list[str]`: one native invocation, never automatic retries.
- `consume(event, result) -> None`: accumulate `Result.completed`, output, session id,
  failure and optional reset hint from authoritative protocol events.

`Capabilities` independently declares launch, continuation, attachment, quota_recovery,
output, native_lineage, enforceable permission names and a platform support map.
Levels are `unsupported`, `experimental` and `qualified`. Experimental capabilities
need policy opt-in. Claim only restrictions your adapter actually enforces.

`Result` final output needs a completed protocol result, a native session id, a
successful process exit and confirmed cleanup. Core owns capacity, consumption
receipts, process signaling, final publication and generation fencing. Do not write
core run records, infer parentage from cwd or make model calls in diagnostics.

A continuation-capable adapter receives generation >1 with `resume_session_id` and
must preserve its selected account/model/billing and permission mode. To declare
quota recovery it must also implement `quota_ready(run) -> bool` that proves the
exact Route/account is ready. A timestamp guess does not qualify. Core consumes the
finite budget before native invocation. Declaring attachment alone does not enable
control: the alpha has no qualified attachment lease backend and refuses it.

Native lineage is connected through the documented `normalized-v1` source export.
This separates observation from control and avoids hardcoding private native stores.
Source adapters should include completeness, namespace and generation attribution;
missing tracing capabilities must stay explicit. DSH provider normalization belongs
in a separately qualified profile or extension rather than the execution core.

Deterministic plugin tests install synthetic entry-point metadata and exercise the
installed CLI, worker protocol, continuation budget and public output gates.
