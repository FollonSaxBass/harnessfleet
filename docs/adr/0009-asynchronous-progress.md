# Persist progress outside the native protocol reader

A synchronous progress transaction can delay observation of native completion
until after the native client has terminated its managed helper. Keep protocol
parsing and direct helper liveness checks on the existing reader, and give
ordered progress persistence one bounded writer thread. Throttling synchronous
writes leaves the same blocking path; a second protocol reader would share
mutable completion state and complicate failure ordering.

The queue includes in-flight records in its 4,096-record and 2 MiB limits. Batches
contain at most 128 records and flush within 50 ms, promptly for a new session.
Only progress transactions use a short busy timeout and retry proven rollbacks.
Invocation, ownership and final receipts retain their existing durable writes.
Successful completion requires all progress/session updates flushed and the
writer joined. Unknown writer termination retains capacity without accepted output.

The trade-off is delayed live progress and session durability. A supervisor crash
can lose their uncommitted suffix, while durable consumption and ownership still
forbid replay. Preserve all submitted event kinds in order and the full bounded
native evidence. A Python thread cannot guarantee cancellation of stalled
filesystem calls; failure to join remains explicit uncertainty.
