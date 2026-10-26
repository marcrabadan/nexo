# Shield phase 2: execution isolation and dependency ceilings

Phase 2 adds a Linux offline worker, hard resource limits and synchronous
per-module permission ceilings. Ordinary `security run`/`trace` retain phase 1
API enforcement. OS confinement is requested explicitly and never emulated
when the host cannot provide it.

## Commands and requirements

```
cd compiler && cargo build --locked --workspace
cd ..
nexo security probe
nexo security run --sandbox=linux --cpu-seconds 2 --memory-mib 256 \
    --file-size-mib 16 --wall-seconds 5 app.nexo
nexo security trace --dependencies permissions.json app.nexo
```

`probe` tests actual Landlock ABI availability, `openat2`, installation of a
seccomp filter in a child, actual Landlock grant/denial enforcement in a second
child, and a non-root execution identity. The supported
profile is `linux-offline-v1`: Linux x86_64, Landlock ABI >=3, openat2, seccomp
and a dedicated non-root worker. Other platforms or unavailable mechanisms
return failure before the target program executes. Status 125 means setup or
an unsupported guarantee; 124 means the wall deadline expired.

The interpreter CLI delegates `--sandbox=linux` to the native compiler/worker;
it does not claim to sandbox the Python host interpreter. The repository's
compiler and worker must have been built. `--dependencies` without an OS profile
works independently in both interpreters. Resource-limit options without
`--sandbox=linux` are rejected, rather than accepted without enforcement.

## Kernel boundary

The compiler runs in the trusted host and builds an artifact before launching
an independent `nexo-shield-worker` supervisor. The supervisor owns its child exclusively and uses a pinned pidfd for termination
so timeout handling cannot target a recycled PID. Monitoring errors also stop and
reap the worker. Its single-threaded child:

1. Rejects root execution, drops effective/permitted/inheritable/ambient Linux
   capabilities and sets no_new_privs. Failure stops setup.
2. Canonicalizes and pins grants with openat2 NO_SYMLINKS/NO_MAGICLINKS; adds
   Landlock rules on those pinned descriptors. The executed inode is the same
   pinned inode that receives execute permission, not a second path lookup.
3. Installs hard rlimits, seals incidental descriptors with CLOSE_RANGE_CLOEXEC,
   and applies the Landlock ruleset and seccomp syscall allowlist.
4. Executes the pinned ELF descriptor with execveat. Its argument/environment
   buffers remain valid through the syscall and incidental FDs close on exec.

The only new unsafe Rust boundary is in `security/sandbox/linux.rs`, where the
Linux ABI requires raw syscalls and C-compatible structures. Each ownership,
buffer-lifetime and dedicated-process assumption is documented there. There are
no allocation or locking operations in a fork/pre_exec closure.

Landlock enforces granted filesystem objects through kernel path resolution,
including final symlinks and concurrent symlink replacement. Read grants allow
file contents and directory listing; write grants allow ordinary creation,
modification, truncation, removal, rename and symlinks within granted trees.
Special device/socket/FIFO creation is not granted. Metadata-changing syscalls
such as chmod/chown/xattrs are absent from the seccomp allowlist.

For Nexo programs, OS grants derive from positive `read`/`write` policy rules.
They must name existing files or directories; use an explicit existing output
directory when creating files. Landlock cannot subtract a denied subtree from
a larger allow rule, so this profile rejects filesystem deny exceptions rather
than granting an inaccurate approximation. Disjoint positive rules are required.
The trusted boot set adds read-only `/usr/lib`, the dynamic loader and, when
present, `/etc/ld.so.cache`; these do not become Nexo API grants. The worker,
runtime library directory and dependency configuration cannot overlap a write
grant. Configuration/grant directories must be trusted during setup.

Network syscalls, all clone/fork variants, arbitrary execve, ptrace,
process_vm_*, io_uring, bpf, namespace/mount changes and later security-control
changes are not in the syscall allowlist. execveat is restricted to the pinned
FD; Landlock independently restricts executable inodes. The x86_64 audit
architecture is checked and x32 syscall numbers are not allowed. Native code
cannot remove these kernel restrictions by changing an environment flag.

This profile has **no network access**, including localhost and DNS. A Nexo
policy containing a positive network grant is rejected. This avoids claiming
that a hostname allowlist prevents DNS rebinding: an online isolated profile
with address pinning and reviewed proxy handling is still unimplemented.

## Resource contract

| Resource | Enforced bound |
| --- | --- |
| CPU | RLIMIT_CPU soft budget, hard budget one second later; cannot raise either beyond the hard bound |
| Address space | RLIMIT_AS soft=hard, default 256 MiB; not a claim about RSS or page cache |
| Regular file size | RLIMIT_FSIZE soft=hard, default 16 MiB per file; not an aggregate disk quota |
| Descriptors | RLIMIT_NOFILE soft=hard=64 |
| Core dumps | RLIMIT_CORE=0 |
| Wall clock | Supervisor kills and reaps its worker at the configured deadline, default 5 s |

The worker cannot create threads or child processes, so there is no unbounded
process tree or unaccounted descendant to survive the supervisor timeout. A
program using parallel execution is rejected before compilation in this OS
profile. The Nexo runtime enters its main thread directly instead of attempting
the ordinary 1-GiB recursion thread stack. cgroups v2
and namespace isolation are not installed; no aggregate filesystem quota, I/O
rate, shared-host DoS resistance, process-list hiding or PID namespace guarantee
is advertised. Landlock does not conceal all filesystem metadata (for example
stat); grants control content access and mutation, not path existence secrecy.
Use this as a limited single-process worker, not a replacement for tenant VMs.

## Restricted native integrations

```
nexo security worker --read /usr/lib --read /etc/ld.so.cache \
    --read /srv/job/input --write /srv/job/output \
    --cpu-seconds 2 --memory-mib 128 --wall-seconds 5 \
    -- /srv/job/analyser /srv/job/input/data.json
```

The executable is an absolute ELF path. A Python worker uses an absolute Python
ELF executable, with explicit read grants for its interpreter libraries and
script. There is no implicit grant for the project, home, `/etc` or `/proc`.
Runtime library grants are explicit for this native command. Standard output
and error are delegated output channels; stdin reaches EOF. Arguments and
granted files carry non-secret job inputs. Explicit `--env NAME` delegates only
that host binding; other host credentials are removed. Values are delivered to
the trusted helper through a bounded anonymous pipe, never command arguments,
saved files or the helper's dynamic-loader environment. Loader-control bindings are rejected by the Nexo sandbox launcher. The target environment
is constructed only for exec after confinement, so an environment binding cannot
preload a library into the unconstrained helper.

In-process Nexo C/Python imports and shell commands remain rejected. The worker
is an explicit separate execution interface, not transparent per-function FFI
RPC or a claim that native memory/object handles are safe to share. A typed RPC
bridge is future work. Host code, compiler, worker and control/configuration
paths must be trusted and protected against modification between runs. Do not
grant `/proc`, host credential directories, control-plane paths or unnecessary
writable trees to untrusted jobs. Explicitly granted broad filesystem access can
expose sensitive files, just as a broad application policy can.

## Dependency ceilings

Create `permissions.json` with module identities relative to its directory:

```json
{
  "version": 1,
  "modules": {
    "lib/reports.nexo": [["allow", "read", "data/public"]],
    "lib/parser.nexo": []
  }
}
```

```
nexo security run --dependencies permissions.json app.nexo
```

Rule targets use the same working-directory semantics as D142. Every dependency
starts with zero capabilities unless named. Its rules cannot extend the root
policy. At an operation the runtime intersects the application policy, the
lexical module ceiling and every active caller ceiling. An explicit deny wins;
importing a more privileged module or invoking a root callback cannot recover
a capability missing from the importer/caller. A root operation after a
synchronous return regains its own ceiling. A fatal dependency diagnostic cannot
be caught by Nexo `try`.

Dependency boundaries currently export only scalar data and materialized lists,
maps or sets containing data. Callable values (including imported functions or
builtins hidden inside containers), deferred pipelines, records and opaque native
handles cannot cross these returns until they can carry sealed contexts. Result
checks fail closed and are bounded to 100,000 nodes and depth 128. Synchronous
callbacks can be invoked inside the context, but this is not general taint
tracking or a safe protocol for publishing untrusted authority through custom
shared-state queues in trusted application code.

The Rust compiler emits scope entry/restoration in function prologues, common
returns/unwinds and module initialization, **only** for context-enabled builds.
A runtime marker refuses a requested context file on an uninstrumented binary.
Ordinary LLVM optimization paths are unchanged. The Python interpreter scopes
calls, expressions (including deferred pipelines) and module initialization with
finally-based restoration. Module identities are frozen before user statements
execute so later path replacement cannot select a different module's ceiling.

Permission files are bounded, versioned and reject unknown fields, nonexistent
module paths, duplicate canonical identities, malformed rules and root-policy
overrides. They are operator-controlled configuration, not authority supplied
by a dependency or a model. This is file-module attenuation, not a package
registry or signed package manifest. It can be combined with the OS profile.

Asynchronous context propagation is deliberately unsupported. With dependency
ceilings enabled the lexer rejects parallel/spawn, event, server, stream, agent
and pipeline constructs and their internal registration identifiers, including
in dependencies and dead code. Some identifiers used by these constructs are
therefore unavailable in this opt-in profile. This conservative synchronous
restriction prevents context loss instead of pretending to attenuate callbacks
or tasks after dispatch. Context capture/propagation across queues, tasks and
external calls is needed before relaxing it.

## Validation and remaining work

```
cd compiler && cargo test --locked
cd ..
pytest -q tests/test_security_isolation.py tests/test_security.py tests/test_policy.py tests/test_http.py
python compiler/scripts/conformance.py
python compiler/scripts/differential.py
```

Kernel tests run real native payloads: allowed/denied reads, traversal, symlinks
and a symlink replacement race, writes, network/fork prevention, inherited-FD
escape, environment scrubbing, CPU/address-space/file/wall limits. They skip
with a specific reason when the required kernel profile is unavailable; the CI
job explicitly requires a supported probe so it cannot pass by skipping every
Landlock assertion. Independent Rust subprocess tests verify seccomp socket/fork
blocking and actual CPU, memory and file-size limits even without Landlock.

This development environment returns ENOSYS for the Landlock ABI query, both
inside and outside the tool execution filter. It reports seccomp and openat2 as
available. Full-profile filesystem/native/timeout tests therefore cannot be
validated here; attempting a sandbox launch returns 125 without running the
payload. Positive Landlock validation still needs a compatible non-root Linux
host or CI run. No syscall-verification bypass was used to make those tests pass.

Remaining work: a reviewed online profile with DNS/IP/proxy enforcement, cgroup
and namespace profiles, aggregate resource budgets, asynchronous context capture,
typed native RPC and package-registry metadata. Phase 3's information-flow labels
and agent/tool authorization remain separate from these capability controls.


## Results from this cloud environment

- `cargo build --locked --workspace`: passed; compiler and worker artifacts built.
- `cargo test --locked -j 4`: 49 tests passed; the existing optional decision
  microbenchmark remained ignored. Nested subprocess tests additionally verify
  seccomp, actual hard limits and pidfd targeting; no child services remain.
- Full `pytest -q` snapshot: 1125 passed, 14 skipped in 757 seconds. This run
  collected 1139 cases, before 15 final hardening cases were added. Current
  collection contains 1154 cases; the full suite was not rerun after those additions.
- Broad security/policy/HTTP/catalog regressions: 140 passed, 13 skipped.
  Final isolation/catalog run after the last hardening changes: 56 passed,
  13 skipped. The skips are positive Landlock/native/Nexo sandbox cases; the
  full snapshot also skipped the three optional VS Code cases.
- Compiler conformance: 127/127 passed. Differential: 218/228 identical,
  10 existing documented differences and zero unexpected differences.
- Final probe: Landlock ABI 0/unavailable, enforcement false; seccomp, openat2,
  pidfds, non-root identity and exclusive single-thread supervision available.
  A read-only probe outside the tool filter also returned ENOSYS for Landlock.

These results validate the synchronous dependency model, irreversible syscall
filtering, actual resource limits and safe rejection on this host. They do not
establish positive Landlock filesystem confinement here. The added CI capability
step requires actual support before running those kernel integration assertions;
that remote CI job has not been executed from this chat.

For container deployment, see [Shield in Docker](shield-docker.md).
