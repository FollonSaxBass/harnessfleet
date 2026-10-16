# Ownership and recovery

A run reserves capacity transactionally before its supervisor starts. Queueing is
explicit; queued work has not invoked a model. Cancellation and dispatch serialize
through the same database transaction. Dispatch rechecks Route configuration,
capabilities, current policy, inherited grants and active parent generation.

An already running host controller brokers roots requested from unmanaged native
sessions; start it from a host terminal during setup. Direct standalone root launch
is intended for a host terminal, because a native tool may terminate detached
children when it returns. The common skill checks controller readiness first.
The host supervisor brokers managed child launches. A native tool subprocess only
registers a durable request; it does not own the child's supervisor lifetime. The
broker claims the launch before creating its supervisor. Losing a creation
acknowledgement leaves uncertainty and never creates a second invocation.
Recovery leaves an unclaimed, uninvoked root admission pending while the registered
controller is alive. Once a spawn has been claimed, missing creation evidence stays
uncertain even if another controller is available.

Supervisors and native processes carry a per-generation execution marker, Linux
boot/start identity and pinned pidfds for signaling. An unrelated PID or a reused
process group is not enough to authorize a signal. A scrubbed or unreadable live
member in the recorded group leaves cleanup uncertain. Readable detached descendants
retaining the marker are included. A process that scrubs both its marker and group
cannot be proven by this backend; arbitrary daemon containment is outside the
backend's contract. Native tool restrictions are not OS containment.

The optional stdio helper also has a generation-specific registered process receipt.
Cleanup uses that exact PID/start identity even if the helper detaches or scrubs its
marker. A reused identity is never signaled, and unreadable ownership retains
uncertainty. Helper exit before native completion fails required delegation without
restarting it. Existing children keep their own lifecycle and receipts.

`run stop ID` fences new child admission before enumerating descendants and cleans
up only demonstrably owned processes. Unknown cleanup remains `uncertain` with
capacity retained. `run recover` checks lost controllers and local evidence. It
never relaunches consumed or ambiguous work, and a late generation cannot overwrite
a newer generation. Missing process-creation evidence remains uncertain.

Use `run status`, `run events` and `run output --partial` to inspect a failure.
Do not delete records or mark them successful to clear capacity. A completion receipt
error is an evidence failure, not permission to repeat a paid invocation. Partial
and rejected evidence remain available for diagnosis. Native raw logs remain private
in the instance's state directory; public interfaces expose bounded/redacted output.

`run resume ID "FOLLOW-UP"` requires verified cleanup and a supported terminal
native session. It preserves the stable id, Route, billing, permissions, model,
working directory, grant and old generation evidence. Claude forks its native
session; Codex resumes its recorded native session with generation-specific lineage.
Managed host workers use the [scoped delegation client](delegation.md) in every
generation. Codex receives no instance-state writable root. Boxes receive no host
delegation endpoint and cannot originate managed delegations.
Obsolete supervisors cannot publish into the new generation. A managed child's
continuation requires its original parent generation to remain active; otherwise it
is refused before admission instead of waiting for a broker that has exited. Descendant agents may
continue only their own delegated children, under their active parent generation.

Quota, authentication and execution failures are separate. Only structured native
error envelopes classify quota; ordinary mentions of quotas do not. An authoritative
Claude OAuth refresh error is authentication failure. Automatic quota continuation
requires an explicit finite opt-in (at most three cycles), a plugin capability and
proof of exact-account readiness. Consuming the next generation and decrementing its
cycle budget are atomic. No built-in adapter currently proves account-specific
recovery, so built-in automatic recovery is refused.

`service start/status/stop` controls only this instance's queue controller and checks
its process ownership receipt. It installs no system startup hooks. Supervisors are
independent of both controller and dashboard. Keep the controller running for queue
dispatch, or call `run dispatch` explicitly. A queued child whose parent generation
is no longer active is rejected before native effects.
`service run` is the registered controller's internal entrypoint and requires both
its token and process receipt. The token is removed before supervisors are spawned.

`run prune` applies event/transcript retention after three days and output retention
after fourteen days. Active, kept and uncertain runs are preserved. Compact metadata
and receipts remain. `run keep ID` protects a result. Expired output fails closed;
retention never authorizes replay. State must remain user-owned (mode 0700), with
regular private database/artifact files and no shared untrusted writers.

Native workers in this alpha must still be trusted with the host. Normal managed
CLI and Python clients use scoped requests without opening the instance registry.
Same-UID filesystem/process access is not isolated. The database contains other
runs' grants and tokens; a worker able to
write it can bypass API grant validation or alter another run's evidence. Token
removal from the supervisor environment does not isolate that database. Read-only
native sandbox modes are not a separate-user confidentiality boundary either.
Do not run mutually untrusted work in one instance. A controller-owned registry
with a restricted delegation client and scoped artifact access is required before
claiming worker isolation or tamper-resistant inherited authority.


Progress persistence has a bounded queue and a separate writer so database waits
do not delay native completion checks. It retains every submitted progress record
in order during successful execution. Failure cancels uncommitted pending progress;
the reader's bounded native evidence remains available on the existing artifact
path. Overflow, persistence deadline or non-retryable I/O failure prevents
successful output publication. Only a confirmed rollback after SQLite busy/locked
may retry this metadata batch; native invocations never retry with it.

Normal completion flushes progress and confirms writer termination before terminal
publication. If a writer does not terminate, `progress_uncertain` retains capacity
and forbids accepted output. Cancellation cannot hard-interrupt an arbitrary
filesystem syscall. Reconciliation must continue to prove supervisor/process
termination; elapsed timeout alone does not release capacity or authorize replay.
