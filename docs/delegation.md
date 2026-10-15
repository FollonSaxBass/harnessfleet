# Scoped native delegation

This candidate is not ready for native delegation acceptance. A real Codex CLI
0.162.1 workspace-write parent could not connect to its endpoint and stopped before
launching a child. Model-free sandbox probes reproduce `EPERM` in read-only and
workspace-write. No implicit network widening or direct-storage fallback is allowed.
The deterministic protocol tests below do not qualify that native sandbox path.

Runs default to `--delegation leaf`, or `delegation="leaf"` in Python. Leaf runs
receive no delegation capability, endpoint or delegation skill instruction. They
retain a managed-context marker: attempts to delegate fail without opening Store.
This also applies on continuation. Leaf execution does not require a transport.

`--delegation required` requests delegation explicitly. Both the inherited grant
and current policy must select `delegation_transport: "experimental-unix-v1"` and
`experimental: true`. Missing transport configuration means `disabled`. A Route
may disable it independently. Required execution refuses before admission when
disabled, with dispatch and continuation revalidation. Selection is recorded per
generation; a leaf continuation cannot acquire delegation. Experimental opt-in
permits transport investigation; it does not certify native connectivity. Do not
enable it expecting Codex CLI 0.162.1 delegation to work in its default sandbox.

Managed host CLI and Python clients select a generation-specific Unix socket before
opening a registry or loading instance configuration. Native environments receive
no instance config/state location, and Codex receives no registry writable root.
Trusted supervisors and administrative terminals retain local storage access.

The worker interface permits Route discovery under the parent grant; child start,
batch, list, status, events, follow and output; and child cancel, stop and resume.
Children must belong to that exact parent generation. Configuration, controller
management, global enumeration, retention, dispatch and recovery are unavailable.
Followers block on execution events for at most 120 seconds per request; a timeout
leaves execution running.

Authentication checks the capability, active parent generation, a pinned native
process receipt and the actual message writer's attributable native ancestry.
Linux message credentials identify writers even after a connected socket is handed
to another process. UID and execution markers alone do not authorize requests.
Unverifiable, reparented or exited ancestry is refused. Boxes cannot originate
managed delegation; namespace support is not inferred.

The version-1 protocol bounds requests to 1 MiB, responses to 32 MiB and batches to
128 targets. Each parent has eight concurrent request slots and receive deadlines.
Clients cannot pass descriptors, supply accepted receipts or select artifact paths.

Mutations carry request ids bound to the parent generation and request content.
Reusing an id returns the stored result; different content gives `request_conflict`.
A pending or ambiguous receipt gives `request_uncertain` and is never redispatched.
Clients never retry automatically or fall back to direct storage. Transport errors
report the id: repeat only that id with `--request-id ID` on delegated start, batch,
resume, cancel or stop. Python clients accept `request_id=ID`. Host administration
does not accept the flag. Inspect existing children after ambiguity; do not submit
the assignment under a new id.

This is scoped authorization for trusted host workers. It does not isolate same-UID
filesystem/process access, fully scrubbed environments, host IPC or dashboard
administration. See the [trust boundary](recovery.md) and [backlog](remaining-work.md).
`policy.worker_isolation` defaults to `trusted`, including older configurations. A
Route may also require a mode. Any non-trusted requirement is refused before
admission; no isolated backend or secure unmanaged-caller enrollment is qualified.
