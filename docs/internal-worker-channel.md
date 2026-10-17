# Internal receive-only worker channel

This opt-in experiment extends the disconnected namespace launcher. It is not
available through RunManager, Routes, adapters, skills or CLI admission. Native
isolation requests remain unsupported. The launcher and channel share one trusted
controller thread and the narrow runtime described in
[the launcher decision](adr/0010-disconnected-namespace-launcher.md).

The trusted caller may use `launch(spec, control_channel=True)`. The option must
be a boolean; the default creates no channel. The launcher creates a fresh Unix
SEQPACKET pair, enables credentials and passes only the guest endpoint, using the
reserved `HF_CHANNEL_FD` environment key. Caller-supplied values for that key are
refused for an enabled channel. It binds the host receiver before granting payload
execution and closes the parent's guest endpoint. Endpoints cannot be supplied by
a payload or reused across launches.

`handle.control_channel` is a read-only property. Its internal receiver offers
`receive(timeout=...)` and `revoke()`, with no send, raw descriptor or rebinding
interface. Revocation is irreversible and precedes termination or an uncertain
close refusal. It also blocks previously queued data from an inactive writer.
These observations grant no execution authority and are not an atomic fence
against a writer dying immediately after validation.

## Records and writer observations

Each record has a network-order `!4sQII` header: magic `HFCP`, generation, sequence
and body length. The complete record is bounded to 4096 bytes, including its
20-byte header. The disconnected experiment fixes generation to one; this is not
a managed Agent Run generation or a durable continuation receipt. Sequences start
at zero and advance only after acceptance. At most sixteen received records,
including rejected records, are considered. Timeouts must be finite and positive;
waiting does not repeatedly read unchanged logs.

The receiver requires one set of kernel credentials matching the originally
pinned payload's host PID and user/group. It also checks the original process
birth receipt, PID namespace and live native/init/launcher pins. A forked child
or another sender cannot acquire the original writer's identity by sharing the
endpoint. Missing, extra or unsupported ancillary controls and truncated records
are refused. Every received SCM_RIGHTS descriptor is closed before rejecting it.

A failed received-descriptor close revokes the receiver, preserves the handle's
resource uncertainty and makes subsequent handle close refuse completion. The
number is never retried, even if independent cleanup later frees it and an
unrelated descriptor reuses that number. Cancellation preserves the original
exception while remaining received descriptors still receive cleanup attempts. Raw ownership-descriptor errors follow
the same retention rule. Endpoint and stream close errors remain recorded after an
independent cleanup; another close cannot report success or retry the failed
resource. Other cleanup is still attempted; process exit evidence
is distinct from successful closure of all host resources.

## Failure and qualification boundaries

Post-spawn failure retains the original handle. Unexpected exceptions and
cancellation retain the original exception; expected startup errors retain it as
the cause of a startup refusal. Secondary cleanup does not replace the primary
failure. Once writing or flushing the execution byte is attempted, execution may
have started and no automatic retry is authorized. Runtime preparation attempts
remaining descriptor cleanup after an error and retains unresolved resources.
Preparation failures keep their runtime owner, including temporary source files,
sealed descriptors and owners from nested construction failures. Cleanup cannot
replace the original cancellation or unexpected failure. An incomplete startup
retains unconfirmed process ownership even before namespace-init pinning.

Tests use synthetic payloads, explicit pinned runtime fixtures and owned kernel
processes. They cover original/forked senders, malformed frames, sequence and
generation refusal, ancillary truncation, rights cleanup, dead queued writers,
revocation, bounded records/timeouts and startup/cleanup failures. A passing pure
frame test, skipped prerequisite or private proof is not native adapter
qualification. Source and installed-wheel results must identify the exact tested
snapshot separately.

This does not enable model accounts, network access, project or credential
mounts, outbound replies, managed delegation or recovery after controller loss.
Large descriptor tables, broader parser/traffic shutdown combinations and durable
ownership reconciliation need separate qualification. Callers must keep uncertain
handles and inspect evidence rather than treating a timeout or close exception as
permission to replay an invocation.
