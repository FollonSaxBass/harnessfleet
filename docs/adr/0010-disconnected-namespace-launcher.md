# Qualify a disconnected launcher before enabling worker isolation

Scoped clients do not confine a same-UID native executable. An executable can
also run during authentication diagnostics, before the main native invocation.
Mount protection, process ownership, helper authentication and continuation must
be qualified together before any adapter may claim worker isolation.

The first code slice is the internal `harnessfleet.boundary` experiment. It is
disconnected from RunManager, routes, plugins and CLI admission. Non-trusted
isolation requests remain unsupported and fail before native invocation.
Its version-1 LaunchSpec is an internal experiment, not a public execution record.

## Deliberately narrow runtime

The trusted controller supplies a versioned `RuntimeManifest` with exact source
paths, guest paths, roles and expected SHA-256 hashes. Only CPython 3.12 on Linux
x86_64 is admitted. `LaunchSpec` carries Python source, not an arbitrary command.
This is an operating-system boundary, not a Python language sandbox: payloads may
use native extensions and ctypes within the namespace and syscall restrictions.

Every selected source and ancestor is opened through directory descriptors without
following symlinks. Sources must be regular, root-owned files without group/other
write or set-id bits. Preparation bounds the source count, individual and total
bytes and archive size. It copies from the same opening, checks before/after
metadata and the expected digest, then applies and verifies WRITE, GROW, SHRINK and
SEAL memfd seals. Metadata alone does not prove atomicity. The expected digest
checks the copied bytes; seals prevent later mutation through host hardlinks or
preopened writable descriptors. No runtime discovery, hash enrollment or implicit
trust in hashes generated from the current host is provided.

Preparation completes before process creation. Only sealed files are exposed with
Bubblewrap `--ro-bind-data`; no host directory is bound. ELF validation admits a
narrow declared ELF64 little-endian x86_64 closure, an exact loader and unambiguous
declared dependencies. It rejects RPATH/RUNPATH and unsupported audit/filter/config
loading mechanisms, without invoking `ldd`, the interpreter or source executables.
It is not a general dynamic-library resolver. Runtime extensions and any optional
dynamic dependencies must be selected explicitly.

Pure-Python standard-library sources form a deterministic ZIP_STORED archive at
`/usr/lib/python312.zip`. Compiled extensions remain separate sealed files. Archive
members exclude traversal, absolute paths, bytecode, customization and site-package
locations. Bootstrap and payload run with `-I -S`, ignoring ambient Python options
and disabling site initialization. Locale is C and timezone UTC. There are no host
NSS, certificate, timezone or locale configuration files.

Fixed guest compatibility links and private proc/dev accompany ephemeral
home/tmp/run/work directories. There are no host-writable mounts, account files,
project trees, artifacts or delegation endpoints. Network access is disabled.
The payload receives only explicit `HF_` metadata and fixed home/path/tmp/locale
settings. The host Bubblewrap binary and its host loader remain trusted parts of
the controller environment; they are distinct from the sealed guest runtime.

Mandatory user, mount, PID, network, IPC and UTS namespaces have no permissive
fallback. Additional user namespaces are disabled. Capabilities are dropped and
Bubblewrap sets no-new-privileges. This experiment requires Linux x86_64, rootless
non-setuid Bubblewrap, an explicitly pinned compatible CPython runtime.
Other architectures and incomplete manifests are rejected.

The trusted Bubblewrap reaper stays PID1. The payload is a distinct process;
giving a hostile payload PID1 lets it clear its own parent-death signal and
outlive the launcher. Proc access to PID1 is masked. A sealed Linux x86_64
seccomp filter blocks ptrace, process_vm read/write, pidfd_getfd, kcmp, setns and
unshare, and rejects alternate syscall ABIs. This protects the reaper's memory
and descriptors as well as host processes. The filter is not a complete syscall
allowlist or a promise of resource-exhaustion resistance.

## Startup and ownership

An initial setup barrier lets the host pin the launcher's pidfd and the distinct
namespace-init receipt, namespace identity and pidfd. Bubblewrap's setup barrier
also proceeds on EOF. A second trusted bootstrap therefore requires an explicit
byte on stdin, refusing EOF before executing the payload. Its readiness prefix is
consumed before untrusted output. The host pins the unique bootstrap child before
granting execution; exec preserves that process identity. Supervisor death during
setup cannot turn a closed pipe into permission to execute the payload.

An ownership-read failure returns the original blocked handle with the exception.
Explicit startup reconciliation pins that same invocation without releasing it;
it is not a retry. Termination does not signal an unconfirmed init or release a
blocked launch merely because the launcher PID is known. Unproven cleanup remains
uncertain. Handles belong to one trusted controller thread and are not durable
recovery records or a substitute for the core's admission receipts.

Confirmed cleanup requires the pinned init, payload and launcher to have exited.
The kernel kills the namespace's remaining processes when its init exits; see
[Linux PID namespace semantics](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html).
The trusted reaper keeps parent-death behavior even when a payload changes its own
signals, forks, detaches, clears its environment and re-execs. Only pinned host
pidfds authorize signals. Numeric namespace IDs, process groups and worker markers
do not authorize cleanup. Bubblewrap's reaper and parent-death implementation are
documented in its [0.9.0 source](https://github.com/containers/bubblewrap/blob/v0.9.0/bubblewrap.c).

Callers must drain stdout/stderr with their own bounded-output policy. Timeouts
do not imply cleanup or authorize replay. The launcher has no model recovery,
account switching, output acceptance or capacity-release behavior.

## Evidence and integration gates

Kernel tests use synthetic host state, sockets and unrelated sentinels, checking
host integrity directly. They cover state/proc/FD/signal access, pathname and
abstract sockets, loopback separation, positive runtime and ephemeral writes,
ownership-read failure, detached re-exec, namespace-init/native/launcher death and
supervisor death during startup or execution. Alternate syscall ABIs are rejected
by the kernel filter. Tests explicitly skip unavailable prerequisites; a skip is
not qualification. Current local evidence uses Bubblewrap 0.9.0 on Linux x86_64/WSL.

Runtime unit tests use synthetic ELF metadata and injected root-ownership metadata,
with real copying, hardlinks, descriptor lifetime and kernel seals. They do not
certify an executable runtime. Actual kernel tests require a separately approved
manifest via `HF_BOUNDARY_RUNTIME_MANIFEST` and its exact SHA-256 via
`HF_BOUNDARY_RUNTIME_SHA256`. Without both, those cases explicitly skip. The fixture
does not generate or bless a host manifest. Kernel qualification must record the
chosen file provenance and manifest hash outside the public repository. A passing
unit suite or a prerequisite skip cannot replace that execution proof.

This does not qualify native accounts, adapter diagnostics, persistent project or
credential mounts, model connectivity, broker identity across PID namespaces,
continuation, resource limits or recovery after loss of the controller's handles.
Those remain separate admission gates. No native isolation mode or existing
workspace deployment is enabled by this module.
