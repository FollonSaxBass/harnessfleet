# Configure an instance

`harnessfleet init` writes `~/.config/harnessfleet/config.json`, or the path in
`HARNESSFLEET_CONFIG`. State defaults to `~/.local/state/harnessfleet`, or
`HARNESSFLEET_STATE`. XDG directory overrides are supported. CLI `--config` and
`--state` take precedence. Initialization never overwrites a file.

The configuration has `schema_version: 1`. This synthetic example enables two
Subscription Routes on Linux. Replace model identifiers and project paths with
values supported by your installed native CLIs. Enable only Routes and permission
modes you intend to authorize.

```json
{
  "schema_version": 1,
  "routes": [
    {"id":"codex-local","harness":"codex_cli","environment":"host",
     "account":"personal","billing":"subscription","models":["YOUR-CODEX-MODEL"],
     "default_model":"YOUR-CODEX-MODEL","concurrency":1},
    {"id":"claude-local","harness":"claude_code","environment":"host",
     "account":"personal","billing":"subscription","models":["YOUR-CLAUDE-MODEL"],
     "default_model":"YOUR-CLAUDE-MODEL","concurrency":1}
  ],
  "policy": {
    "routes":["codex-local","claude-local"],
    "billing":["subscription"],
    "permissions":["read-only","workspace-write","delegate"],
    "cwd_roots":["/your/project"],
    "max_depth":2,"max_parallel":4,"max_invocations":12,
    "experimental":true
  },
  "sources":[],"projects":[],"connectors":{},"notifications":{},
  "retention":{"events_days":3,"output_days":14},
  "dashboard":{"port":8421}
}
```

Route `account` is a local non-secret label, not proof of an authenticated account.
Codex/Claude Subscription diagnostics verify the native login method, without
returning account details. A configured `CODEX_HOME` or `CLAUDE_CONFIG_DIR` selects
a different native account store by reference, through `environment_vars`. It does
not copy or migrate that store. Native CLIs may refresh their own credentials and
write native sessions during execution; extraction and installation do not change
existing native stores. Use dedicated native homes when isolating qualification.

`models` is the explicit available set for this Route; `default_model` must be a
member. HarnessFleet never replaces an unavailable requested model. `executable`
can name a native binary by absolute path. Route concurrency is positive. Policy
limits are positive integers, and managed depth cannot exceed two. Optional
`expires_at` is a Unix timestamp. Each admitted tree retains a grant snapshot;
current policy and that grant both apply at dispatch and continuation. A narrower
permission request narrows descendant permissions too. Invocation budgets include
all generations and descendants in that tree.

Metered Routes require both `policy.billing` authorization and explicit
`--billing metered`. Use `credential_env: {"OPENAI_API_KEY":"MY_ACCOUNT_KEY"}`
to reference a process environment credential. Never put secret values in JSON.
Ambient API credentials are removed unless explicitly referenced. Subscription
Codex forces ChatGPT login; Claude forces Claude.ai and removes alternate-provider
selectors. No `any` billing mode authorizes a fallback.

DSH's experimental `transport` Route object accepts `base_url`, `api_key_env` and
`provider`, plus a matching `credential_env` reference. It generates a per-run model
and OpenAI-compatible provider overlay with retry disabled. Provider-specific
request normalization and DSH startup compatibility need separate qualification;
no built-in provider profile is certified. Cursor execution is experimental and
requires explicit Metered authorization because Included Usage is not yet proven.

## Connected native lineage

No native home is scanned by default. Enable a bounded normalized source export:

```json
{"id":"my-cli-store","harness":"codex_cli","environment":"host","account":"personal",
 "path":"/your/exports/native.json","format":"normalized-v1","enabled":true}
```

The export is a regular JSON file:

```json
{"schema_version":1,"completeness":"partial","nodes":[
 {"id":"session-1","label":"Analysis","kind":"session","status":"working"},
 {"id":"child-1","parent":"session-1","label":"Review","kind":"native_subagent",
  "status":"done","reply":"An observed native reply"}
]}
```

Completeness is `complete`, `partial`, `expired`, `unsupported`, `missing` or
`unreadable`. A connected source's account/environment namespace must match a
managed generation before attribution. For managed associations add
`binding:{"run_id":"RUN","generation":1}` and `managed_session_id` matching that
generation's native session. A native child uses the same binding and its own `id`.
`delegations:[{"run_id":"CHILD-RUN","generation":1}]` identifies an exact caller
only when its binding matches that run's execution parent. Reused sessions need
one occurrence per generation. `native_parent_binding` handles a parent occurrence
with a different generation binding. Cwd or declared depth never establishes an edge.

Source exports are trusted observation inputs, not control grants. Native store
parsers are not yet qualified. Missing sources preserve cached compact lineage,
marking it expired and withholding old replies. Disabling a source stops observation.

### Cursor transcript observation

The experimental `cursor-transcripts-v1` source reads one explicitly selected
project's `agent-transcripts` directory. This includes IDE conversations that have
no chat store, and the same transcript layout written by Cursor Agent:

```json
{"id":"cursor-project","harness":"cursor_app","environment":"app","account":"personal",
 "path":"/your/cursor-project/agent-transcripts","format":"cursor-transcripts-v1","enabled":true}
```

Choose the harness/environment/account namespace explicitly for the source you
connect. `cursor_app` is an observation-only namespace and provides no launch or
attachment adapter. For CLI transcripts use `cursor_agent` and the matching
environment. Transcript contents alone do not establish which app or account ran
them. No source is enabled during installation.

The supported layout is `<session-uuid>/<session-uuid>.jsonl`, with native children
at `<session-uuid>/subagents/<child-id>.jsonl`. Each session appears once from its
transcript; a chat database is unnecessary. Labels use the first available user
query, excluding Cursor's surrounding context tags. Output exposes the latest
available assistant text, redacted and bounded, as an **unverified observed reply**.
Tool payloads are excluded. Only an explicit successful `turn_ended` event marks
the observed turn done; other activity remains unknown.

Reads use directory ownership of the transcript files to associate native children.
They do not infer managed parentage from cwd or process proximity. Associating an
exact managed generation/caller still requires an attributable normalized export.
There is no implicit scan of other projects, credential files or native homes.

At most 256 KiB is read per transcript, using its beginning and end for larger
files, and at most 16 MiB per source snapshot. Sessions are read by transcript
modification time, newest first, with native identifiers breaking ties. Metadata
ordering does not follow session or transcript links. Every discovered session and supported
child remains in the graph when the read budget is exhausted; unread content stays
unknown and produces an explicit issue. Malformed/missing files and rejected links
are reported without hiding readable sessions. For windowed reads, the beginning
supplies labels; only complete assistant records
in the ending window supply observed output. If none is readable there, output is
unavailable even when an older reply was readable at the beginning. Completeness stays partial
because transcripts cannot prove complete native ancestry or lifecycle. Root,
session, child-directory and transcript symlinks are refused. Native compatibility
is covered by synthetic fixtures; broader native tracing qualification remains open.

## Optional integrations

`projects` is an explicit list of `{ "name":"project", "path":"/your/project" }`.
Only these directories are queried by `harnessfleet connectors projects`; Git writes
are disabled. Other connectors are initially empty and use explicit bounded JSON
snapshot paths: `connectors.tasks={"enabled":true,"path":"..."}` and the same
shape for handoffs, ownership, documentation and quotas. Their payload follows the
normalized `schema_version/nodes/completeness` shape. Public items contain only
bounded, redacted metadata: `id`, `parent`, `label`, `kind`, `status` and
`completeness`. Replies and extra fields are excluded; malformed items produce
issues and partial completeness. No company-specific integration is included.

`notifications.webhook` enables a completion webhook with public run metadata only.
`notifications.bearer_env` references its optional environment credential. Delivery
is bounded to one attempt; a failure adds an event and does not change run success.
No Telegram account or other notification provider is preconfigured.

## Experimental Boxes

Codex and Claude can run on Linux amd64 in a local Docker daemon with explicit
experimental policy opt-in. This backend has deterministic protocol evidence;
native container qualification remains separate. Docker Desktop, remote daemons
and arm64 are refused. No Docker image is built or pulled implicitly.

Set a Route's `environment` to `box:your-account` and add:

```json
{"box": {
  "daemon":"unix:///var/run/docker.sock",
  "image":"your-image:version",
  "account_home":"/your/dedicated-native-account",
  "native_home":"/account",
  "mounts":[{"source":"/your/project","target":"/workspace","mode":"ro"}]
}}
```

Prepare the image's native CLI and dedicated account yourself. Standard host native
homes and the whole home directory are rejected as account mounts; host account
configuration is not inherited. Mount only explicit project directories, in ro/rw
modes. The requested cwd must map through a configured mount. Containers run under
the host uid/gid. `box.native_executable` selects the native executable inside the
image when its name differs. `box.docker_executable` selects the local Docker CLI.

A daemon identity and pending ownership receipt are committed before container
creation. The exact container id/name and owner label are verified before start or
cleanup; a daemon change prevents effects. Missing create acknowledgements retain
uncertainty unless the exact owned container can be reconciled. Containers receive
a Box-origin marker, their derived identity and dedicated native home, without
host delegation tokens or the full host environment. Box workers cannot launch
managed delegations. Do not use this experimental backend to certify an image or
billing/account combination without native qualification.
