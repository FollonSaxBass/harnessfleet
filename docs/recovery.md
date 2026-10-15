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

Supervisors and native processes carry a per-generation execution marker, Linux
boot/start identity and pinned pidfds for signaling. An unrelated PID or a reused
process group is not enough to authorize a signal. A scrubbed or unreadable live
member in the recorded group leaves cleanup uncertain. Readable detached descendants
retaining the marker are included. A process that scrubs both its marker and group
cannot be proven by this backend; arbitrary daemon containment is outside the
backend's contract. Native tool restrictions are not OS containment.

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

`run prune` applies event/transcript retention after three days and output retention
after fourteen days. Active, kept and uncertain runs are preserved. Compact metadata
and receipts remain. `run keep ID` protects a result. Expired output fails closed;
retention never authorizes replay. State must remain user-owned (mode 0700), with
regular private database/artifact files and no shared untrusted writers.
