# Support and qualification

This is an alpha, not a certified v0.1 release. The product specification is broader
than the capabilities qualified here. Unsupported operations fail before a native
invocation; experimental execution requires explicit policy opt-in.

| Adapter | Linux/WSL launch | Continuation | Attachment | Automatic quota recovery | Permission modes |
| --- | --- | --- | --- | --- | --- |
| Codex CLI | Experimental | Experimental native resume | No | No | read-only, workspace-write |
| Claude Code | Experimental | Experimental fork | No | No | read-only, workspace-write, delegate |
| Cursor Agent | Experimental, explicit Metered only | No | No | No | unrestricted |
| DSH | Experimental, configured Metered transport | No | No | No | unrestricted |
| Codex app | Disabled | No | Disabled | No | None |
| Antigravity CLI | Disabled: external Windows ownership unqualified | No | No | No | None |
| Antigravity app | Disabled | No | Disabled | No | None |

All native execution on macOS and native Windows is unsupported. WSL uses Linux
pidfds; Windows-native processes require a separate qualified ownership backend.
Linux amd64 Boxes for Codex/Claude have an experimental daemon/container receipt
backend tested with synthetic Docker executables. Native image/account qualification
is pending; arm64, remote daemons and Docker Desktop are refused. Box-origin delegation
is explicitly refused. Qualification did not operate on a real Docker daemon.

Codex uses its native sandbox. Claude read-only exposes only Read/Glob/Grep; workspace
write adds file-editing tools. Claude delegate also enables Bash, which is not OS
containment. Cursor and DSH do not promise read-only enforcement and reject that
request. Their protocol/configuration remains experimental and needs native evidence.
Native starts default to leaf execution without delegation capability. Required
delegation uses a separately enabled experimental interface without a registry
write grant. Unix and host-Codex stdio MCP are distinct explicit selections;
neither is a fallback for the other. The stdio bridge authenticates its own process
and native invocation, with an unknown exact native caller. Its synthetic protocol
evidence does not qualify real-account use. Missing transport configuration means disabled.
Same-UID host access remains trusted and can expose other run tokens or
permit state tampering outside that interface; see the
[state trust boundary](recovery.md) and [remaining work](remaining-work.md).

Native lineage accepts explicit normalized-v1 exports and an experimental
`cursor-transcripts-v1` source for one configured project's transcript directory,
including IDE conversations without a chat store and their immediate native children.
Cursor observation has synthetic fixture/browser evidence; complete native tracing
and automatic SQLite/JSONL store discovery are not qualified. Observation, managed
execution and exact causal lineage are separate. Compact observed ancestry survives
source loss, with expired completeness. App control requires a future lease backend.

Optional quota/cost information is supplied through configured snapshots. No automatic
account billing probe or provider-specific quota recovery is certified. Public adapter
plugins can prove account-specific recovery through the versioned contract.

## Evidence recorded 2026-10-15

The successful native proofs listed below predate the scoped Unix transport.
They do not certify this candidate's delegation path. Its first real Codex
workspace-write proof failed with a delegation transport error before child launch;
model-free sandbox probes reproduce Unix connection denial in both supported
permission modes. The parent was consumed once and cleanup was confirmed. This is
an acceptance blocker; Claude-to-Codex and continuation transport qualification
were not attempted afterward. See [scoped delegation](delegation.md).

- Native versions: Codex CLI 0.162.1 and Claude Code 2.1.299,
  Linux x86_64/WSL, Python 3.12.
- Real Codex CLI → Claude Code passed through the common skill. The parent launched
  exactly one child, followed its execution, read verified output and integrated the
  child's result. Requested models were gpt-6.1-sol and sonnet; Claude reported
  claude-sonnet-5-5 for the sonnet alias.
- Real Claude Code → Codex CLI passed the same launch/await/verify/integrate workflow
  with exactly one child. A separate host controller brokered the root launch.
- Both native directions used Subscription billing, explicit Routes, a synthetic
  local challenge and dedicated temporary runtime state. No intermediate human
  commands were needed between either parent's launch and result integration.
- Initial qualification exposed a child supervisor lifetime failure and a stale
  Claude login. The host launch broker was corrected, and the user refreshed their
  own login before new qualification. Failed or ambiguous work was not automatically
  replayed. The initial uncertain child was explicitly stopped and reconciled.
- Deterministic policy, lifecycle, process, graph, browser, Box and plugin tests pass.
  Synthetic Docker evidence does not certify a native image/account combination.
- After the whole-product corrections, a separate native Codex CLI 0.162.1
  workspace-write run and its explicit continuation passed using gpt-6.1-sol on
  Subscription. Both generations wrote distinct synthetic files in the instance
  state directory outside the project cwd; the continuation read the first file.
  Stable run identity, the resumed native session, both verified outputs and
  controller/worker cleanup were checked. This bounded temporary-directory write
  check does not prove denial outside allowed roots or isolation from other runs.

Private run ids, native session ids, account identifiers, transcripts and credential
material are excluded from public evidence. Native runs remain in private temporary
instance state outside this repository. Passing the bounded challenge does not
certify arbitrary projects, provider profiles or every native tracing format.

## Remaining release gates

A broader certified v0.1 still requires qualified app attachment/continuation
backends, native Linux amd64 container execution, provider-specific DSH and Cursor
evidence, native source tracing and expanded crash/ownership qualification. Do not replace an existing workspace deployment with this
alpha. Later platform combinations qualify separately.


Claude's native top-level spend-limit refusal is classified as quota when its
structured assistant error fields identify `rate_limit` / `usage_limit_reached`
and HTTP 429. A matching initialized session is required. The following error
result is never accepted as final output, even when its subtype says success.
Only a finite, valid seconds timestamp from that envelope's rejected `seven_day`
`rate_limit_info.resetsAt` is exposed as the reset. Missing, malformed or differently
scoped reset information remains unknown. Text, nested subagent errors and
`overageResetsAt` do not establish that reset. Classification does not enable
automatic quota recovery, change accounts or replay an invocation. This envelope
was observed on Claude Code 2.1.300; regression qualification uses synthetic native
executables and does not certify current real-account quota availability.
