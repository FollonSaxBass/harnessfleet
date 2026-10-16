# Keep managed stdio separate from native shell delegation

Codex 0.162.1 denies direct Unix socket access in its tool sandbox. Preopened
descriptors survive a standalone sandbox command, but the actual exec tool drops
them. Neither path qualifies the current shell-based delegation interface.

Provide an independently selected experimental stdio MCP binding for host Codex.
Keep disabled and leaf defaults and retain Unix as a distinct selection. The bridge
uses the same scoped execution API and durable receipts; it never opens storage.
The native client approves only the owned server's bounded `run` tool. Core grants
still decide targets, billing, permissions, cwd, depth and capacity.

The supervisor mints generation facts, an isolated helper home and a fixed launcher.
Core authenticates kernel writer credentials, pins the helper and native ancestry,
and fences obsolete or replaced helpers. EOF and cancellation end observation;
they do not cancel children or authorize retries. Native death revokes further
requests. Owned cleanup remains necessary before releasing capacity.

Record the requesting actor as a bridge associated with a native invocation.
Leave the exact native caller unknown. This preserves the distinction between
execution authority and causal lineage instead of inventing a shell/subagent edge.

Qualification must exercise actual native startup, environment, calls and
continuation, plus policy and failure boundaries through the installed package.
Synthetic provider evidence stays separate from account compatibility. This
decision provides no hostile same-UID containment or universal MCP requirement.
