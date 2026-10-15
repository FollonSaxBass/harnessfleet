# Scope delegation separately from worker isolation

Native clients previously opened the shared registry and needed its write grant.
Move normal native delegation to a generation-specific Unix socket served by the
trusted supervisor. Authenticate capability and actual process ancestry, scope
operations to that parent's children, and retain durable request receipts. This
preserves the CLI/Python workflow while removing its registry write requirement.

Trusted control-plane processes retain storage access. Moving every supervisor
write into one service would be a larger lifecycle refactor and would not enforce
same-UID filesystem/process separation. This first step provides scoped delegation
for trusted workers. Hostile-worker isolation, protected administration and explicit
unmanaged-caller enrollment require a separately qualified confinement backend.
Refuse isolation requirements while that backend is absent.

## Scoped transport staging

Native Codex qualification exposed a Unix connection denial. Keep the transport
default-disabled independently of experimental execution. Starts default to leaf
execution without a capability or delegation instructions. Required delegation is
an explicit option with grant/policy opt-in, dispatch and continuation checks.
Earlier native proofs do not qualify this transport. Preserve the separate working
deployment while investigating a compatible, attributable IPC mechanism.
