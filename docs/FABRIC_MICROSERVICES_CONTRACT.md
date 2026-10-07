# Fabric ↔ SAS microservice contract V3

Status: V3 specification with initial rootfs implementation, 2026-10-07.
Helmut's rootfs-backed instance is enabled for operator-directed first integration.
No live smoke service or tunnel was started; live acceptance remains unverified.

This document specifies the filesystem contract for Fabric-installed auxiliary
services in SAS instances. It supersedes the earlier proposal where Fabric
started processes and absence of a PID file disabled supervision. V2 also
supersedes V1's Fabric-side stopping: SAS owns process lifecycle exclusively;
Fabric owns installation and registration.

V3 supersedes V2's placement of executable installation files and PID files
under the appliance-writable `/work`. Lifecycle ownership, registry grammar,
locking order, exact-run stop semantics and durable evidence remain unchanged.

## V3 change and rationale

V2 put `10-start.sh`, `db.info`, locks and payloads beneath `<host-work>` and
allowed PID publication in that same tree. Smithproxy needs writable `/work`
for its own operation. Consequently, a compromised appliance could replace
a script, payload or parent directory which SAS would later trust and execute.
Changing a pathname or making an individual file read-only does not fix that.

V3 separates three trust domains:

| Domain | Host authority | Smithproxy view | Microservice view |
| --- | --- | --- | --- |
| Appliance work | SAS provisions; appliance writes | `/work`, writable | Not mounted by default |
| Microservice installation | Fabric writes; SAS validates/reads | Inaccessible | `/microservices`, read-only |
| Per-run state and PID | SAS provisions; that run writes | Inaccessible | `/run/sas-microservice`, writable |

Only trusted Fabric/SAS host-side operations can change installation files,
registration and lock inodes. A service can publish its own PID without gaining
write access to executable installation files or another service's runtime.
SAS-owned durable run records are separate from all three domains and are never
mounted into either workload. Rootfs and non-rootfs variants MUST enforce the
same boundary; section 12 specifies the requirements.

This is a breaking contract revision. Fabric must use the new lookup paths and
PID environment variable, not probe or fall back to `/work/microservices`.
Section 14 describes the coordinated transition. Publishing the document alone
does not change mounts; see the implementation boundary for the deployed subset.

**Implementation boundary (2026-10-06):** managed-instance lookup, a configurable
60-second scanner, per-run systemd cgroups, status/stop and a generic rootfs are
implemented. Activation requires a prepared rootfs and an active rootfs-backed
Smithproxy instance. Non-rootfs Smithproxy instances stay installation-only;
there is no unsafe host-FS fallback. Microservices themselves always use rootfs.
Multi-server lookup and Test Drive supervision are not implemented. The first
Helmut integration is operator-directed: unit tests only, no live smoke service
or live Tuntom interoperability test was requested/performed. The acceptance
checklist below remains the live verification checklist, not a claim of passing.

## 1. Responsibilities

| Fabric | SAS |
| --- | --- |
| Locate the instance through SAS | Provide authoritative instance coordinates |
| Install binaries, configuration, scripts and version metadata | Supply the generic execution rootfs and mounts |
| Register, update and unregister its services | Scan registrations and start services |
| Request verified SAS stop before changing files | Stop complete execution groups, including descendants |
| Never start the newly installed service | Own execution and instance-level lifecycle |

SAS does not interpret Tuntom configuration or Fabric-specific metadata.
Fabric does not build a rootfs, create systemd units or start the new Tuntom
process after installation. Fabric MUST NOT signal PIDs or enumerate descendants
as an alternative stop mechanism. No Fabric-produced rootfs image or per-service
`service.json` is required by this contract.

## 2. Locate the instance

Connect to the SAS entry server over SSH and query the full instance UUID:

```sh
sas_which_instance "$INSTANCE_UUID" --origin
sas_which_instance "$INSTANCE_UUID" --namespace
sas_which_instance "$INSTANCE_UUID" --work-dir
```

Each selector returns exactly one value and a newline on stdout. Errors use
stderr and a nonzero exit status. The helper uses authenticated SAS API access;
it must not require making runner credentials readable to unrelated users.

* `origin`: SSH-resolvable hostname of the runner owning the instance, not the
  hostname of the querying client. Hostnames must be configured accordingly.
* `namespace`: the instance's existing network namespace name.
* `work-dir`: absolute host path of the instance workspace.

V3 additionally requires `--microservices-dir` on `sas_which_instance` and
`sasctl instance location`, and `microservices_dir` in the location API. It
returns the absolute, SAS-provisioned **installation** directory, outside
`work_dir`. It is not a path Fabric may derive by appending to `work_dir`.
The location response also advertises `microservice_contract_version: 3`.
These lookup additions are implemented for installation staging. Fabric MUST
refuse V3 installation if they are unavailable or `microservices_dir_exists`
is false. The concrete Helmut installation path is
`/var/lib/smithproxy-appliance/microservices/<UUID>`, separate from `work_dir`.

### Capability-based rollout

The initial implementation advertises `microservice_contract_version: 3` for
the installation layout only, with `microservice_mode: "installation_only"`,
`microservice_execution_enabled: false` and explicit `microservice_capabilities`
(`installation`, `supervision`, `status`, `stop`). Only `installation` can be
true at this stage, for an already provisioned directory. This is NOT full V3
lifecycle readiness and does not satisfy the live-execution release gates.

With a prepared image and a rootfs-backed appliance, mode becomes `managed` and
execution/supervision are enabled. Status and stop are advertised when their
backend is available. `microservice_rootfs` identifies the exact image and
runtime account; `microservice_scan_interval_seconds` advertises the interval.
These fields report implementation availability, not live interoperability-test
results. Never override a false capability in Fabric.

Fabric may stage a first payload in such a directory after host-side isolation
has been verified. Neither Fabric nor SAS starts it. Updates/undeploy of existing
runs still require implemented and verified status/stop; installation mode is
not a bypass. Fabric must check capabilities before lifecycle operations, not
infer execution readiness from the version number alone. Existing directories
are not evidence that no run exists. Full supervision remains disabled until
the release gates pass; this exception permits initial file staging only.

```sh
# V3 installation destination; not the appliance work directory
sas_which_instance "$INSTANCE_UUID" --microservices-dir
```

CLI equivalent: `sasctl instance location UUID --origin|--namespace|--work-dir`.
API equivalent: authenticated `GET /v1/instances/{UUID}/location`, returning
JSON including `origin`, `namespace`, `namespace_path`, `work_dir`, `state`,
`namespace_exists` and `work_dir_exists`.

Fabric first attempts direct SSH/file transfer to `origin`. If unavailable, it
may use the entry server as a transfer/execution intermediary and its configured
SSH identity. A normal SSH jump does not automatically use that server's private
key: these are distinct access modes. Do not copy private keys or disable host
key verification to make the connection work.

Re-query on the destination and verify the instance still belongs there. Never
construct workspace paths or namespace names from the UUID. Lookup is a
snapshot, not a lifetime lease; installation must abort if the instance ends.

## 3. Filesystem layout

```text
<instance-storage>/                  # illustrative; use lookup, not this template
├── work/                           # Smithproxy's writable /work
├── microservices/                  # authoritative microservices_dir
│   ├── db.info
│   ├── db.lock
│   ├── 10-start.sh
│   ├── 10.lock
│   ├── 11-start.sh
│   ├── 11.lock
│   └── tuntom/
│       ├── tuntom
│       ├── … adapters, read-only configuration, other required files
│       ├── version-commit.txt
│       └── version.txt
└── microservice-runtime/            # SAS-managed; never installed by Fabric
    └── <prefix>/<run_id>/           # private writable mount for one run
        ├── <prefix>.pid
        └── … mutable service state
```

`10` is a literal filename prefix, not a separately assigned SAS service ID.
It connects `10-start.sh`, `10.pid`, `10.lock` and the registry row beginning
with `10`. Prefixes are unique within an instance across all owners. Use
positive decimal integers without leading zeroes. Ordering does not imply
dependencies or readiness ordering between services.

PID files are no longer adjacent to scripts. SAS creates a fresh runtime
directory for every `run_id` and mounts only that directory into the service.
Siblings' runtime directories, host runtime parent and durable accounting must
not be visible. Runtime files are not persistent configuration: Fabric supplies
configuration in the installation tree; services write mutable data to their
own runtime directory. No automatic runtime-to-installation copy-back exists.
Runner restart preserves the runtime directory of a surviving run; it does not
allocate a replacement or discard its PID/state. SAS may clean a completed run's
runtime only after verifying its entire execution group empty. Uncertain stops
retain runtime evidence; durable run records/tombstones survive runtime cleanup.

`version-commit.txt` contains the deployed commit ID and a newline. `version.txt`
contains a human-readable version and a newline, or `unknown` until the producer
supplies a version string. These files are informational to SAS.

## 4. Registry: db.info

```text
# prefix owner opaque owner-specific fields
10 tuntom 232 xyz abc
11 tuntom 232 yty jkj blblb
```

Whitespace separates columns. Blank lines are ignored; `#` starts a comment.
The first column selects the filename prefix; the second identifies the owner.
The remaining fields are opaque to SAS and must not contain secrets. There is
no quoting/escaping grammar for embedded whitespace or `#` in custom values.

A valid registration means **this service should run while the instance is
active**. A script on disk without registration must not be executed. Duplicate
or malformed registrations must be reported, not guessed at or executed.

Fabric recognizes its rows by the exact owner token `tuntom`. It must preserve
other owners' rows and must never overwrite a prefix belonging to another owner.

## 5. Execution environment

SAS starts each registered script with:

| Property | Required behavior |
| --- | --- |
| Network | Host/root network namespace for transport; TUN devices target the Smithproxy namespace separately |
| Default filesystem | SAS-provided generic microservice rootfs, independent of Smithproxy's rootfs |
| Installation | `microservices_dir` mounted read-only at `/microservices` |
| Appliance workspace | `/work` not mounted by default |
| Current directory | `/microservices` |
| Start command | `/bin/sh ./10-start.sh` |
| Writable runtime | This run's directory mounted at `/run/sas-microservice` |
| PID namespace | No separate PID namespace in V3; PID is host-visible |
| Lifetime | Bound to the instance; no resurrection after instance stop/expiry |

SAS supplies these environment variables in both execution modes:

```text
SAS_MICROSERVICE_PREFIX=10
SAS_MICROSERVICE_RUN_ID=<opaque-run-id>
SAS_MICROSERVICE_DIR=/microservices
SAS_MICROSERVICE_RUNTIME_DIR=/run/sas-microservice
SAS_MICROSERVICE_PID_FILE=/run/sas-microservice/10.pid
SAS_INSTANCE_NETNS=/run/sas/instance.netns
```

The paths above are normative in rootfs mode. Without rootfs SAS still creates
a private mount sandbox with read-only installation and isolated writable
runtime. It may use different in-sandbox absolute paths; the environment and
current directory MUST identify those actual mounts. Scripts use the supplied
paths, not host-path assumptions. `./tuntom/tuntom` resolves from the installation
cwd in both modes. Writing `./10.pid` is a V3 contract violation.

Tuntom TE/DE scripts use `--tun-netns "$SAS_INSTANCE_NETNS"` to target
Smithproxy's namespace. The handle is mounted read-only; host `/run/netns` is
not exposed. Transport executes in the host/root network namespace, explicitly
selected by systemd's `NetworkNamespacePath=/proc/1/ns/net` before rootfs setup.
It does not use the instance namespace or a profile's transport-veth namespace.
SAS does not create a new uplink or alter host forwarding as part of startup.
The rootfs and installation/runtime mounts are unchanged. New run records expose
`execution_namespace: "host"` and `target_namespace_path`; existing runs are not
moved by a runner restart and require an explicit verified stop/restart to change.

### Initial rootfs baseline (schema 1)

Build with `python -m runner.microservice_rootfs <rootfs-library>`. It snapshots
trusted host tools and the ELF dependency closure without executing workload
binaries. The result is content-addressed, never overwritten. `rootfs.json`
records architecture, file SHA-256s, libc version, exported GLIBC/GLIBCXX/CXXABI
version names, shell identity, tools and UID/GID. SAS verifies file hashes before
activation. The baseline comes from the target host; matching library names
does not imply compatibility with binaries built on a newer system.

* Runtime account/group `tuntom`: UID/GID **65532**, no supplementary groups.
  The launch shell starts as root in the isolated rootfs; Tuntom may drop to
  this account. Fabric must give that account read/traverse/execute access to
  required installation files (e.g. root:65532, directories 0750, secrets 0640,
  executables 0750). Installation files remain root-owned and never group-writable.
* Runtime directory: 65532:65532, mode 0700, fresh for each run. The launch shell
  can publish the PID through its bounded DAC capability. HOME/TMPDIR point here.
* Tools: sh, mv, mkdir, rm, sleep, cat, chmod, ip, flock, stat, date, readlink,
  basename, dirname, id, touch, mktemp, env, timeout, kill, grep, sed, awk, head,
  tail. Their exact host sources and file digests are in the image manifest.
* No host `/work`, `/proc`, `/sys`, DBus socket or host namespace directory is
  exposed. Rootfs and installation are read-only; only per-run state is writable
  (apart from systemd's private device filesystem).
* Capabilities: NET_ADMIN, NET_RAW, SYS_ADMIN (network setns), SETUID, SETGID,
  DAC_OVERRIDE. Namespace operations are restricted to network namespaces;
  mount/chroot, unshare, ptrace/process_vm, module/reboot/swap/clock/raw-I/O,
  file-handle APIs, bpf and io_uring_setup are blocked by syscall filters.
  **With host-netns transport, these are host-network privileges.** The current
  Tuntom namespace-creation bootstrap needs NET_ADMIN/SYS_ADMIN; there is not yet
  a separate least-privilege FD broker. This mode trusts the Fabric-installed
  program with host networking. Rootfs/seccomp do not prevent host-network
  administration through permitted netlink operations. Do not advertise this as
  host-network isolation; workloads must drop privileges after initialization.
* `/dev/net/tun` is the explicitly permitted additional device. Limit: 128 tasks,
  512 MiB memory; stop grace 10 seconds followed by cgroup-wide SIGKILL.
* Scanner interval: `CZ_RUNNER_MICROSERVICE_INTERVAL` (1..86400 seconds, default
  60). Image: `CZ_RUNNER_MICROSERVICE_ROOTFS`, default sibling
  `microservice-rootfs/current` beside `instances`. Image creation/selection is
  explicit deployment work; no per-scan image rebuilding or library installation.

Current implementation rejects installation symlinks and special files, requires
root-owned non-group/world-writable paths, and does not clean up installation
payloads automatically. Preserve them for Fabric's verified undeploy.

Before live integration SAS MUST publish a versioned rootfs specification:

* Immutable artifact identity/digest, architecture and dynamic loader.
* libc, libstdc++, libgcc and libm versions and ABI/symbol baseline, including
  GLIBC/GLIBCXX where applicable.
* Runtime account/group `tuntom`, numeric UID/GID, supplementary groups and
  exact ownership/write permissions for configuration, state and PID publication.
* `/bin/sh` implementation, PATH and complete supported external-tool list.
  Atomic PID publication must work; scripts using `mv` require that utility.
* `/dev/net/tun`, device policy, exact capabilities, mounts and resource limits.

Specific versions and numeric identities are not assigned here. The release
specification is a mandatory gate, not permission to assume host compatibility.
ABI/account/tool changes require a new revision; running environments must not
be silently replaced. Non-rootfs mode also requires a declared baseline.

Fabric supplies small SAS scripts, not existing headless `screen` launchers.
No screen, init system or package manager is implied. A shell alone does not
supply external commands such as `mv`, `ip`, `ps`, `mkdir` or `flock`.

Tuntom adapters require appropriate access to `/dev/net/tun` and scoped network
capabilities. Transport deliberately shares the host network namespace, while
filesystem isolation remains separate. The privilege boundary is described above.

Helper transport follows the host's routes and firewall rules. TUN devices target
the Smithproxy namespace using the mounted namespace handle. V3 startup neither
creates nor changes host interfaces/routes. Transport must not loop into its own
proxy dataplane. Profile transport-veth settings do not select the microservice's
execution namespace.

## 6. PID and process identity

`10.pid` contains one positive decimal, host-visible PID followed by a newline.
The script publishes it at `SAS_MICROSERVICE_PID_FILE`: write a temporary file
in `SAS_MICROSERVICE_RUNTIME_DIR`, close it, and rename it to the supplied PID
path. Use a unique temporary filename, restrictive permissions and fail startup
if publication fails. Do not write the PID in the installation cwd.
All publication must remain on the same filesystem. SAS ignores temporary files.

The PID must identify the long-lived service or its supervisor, never a launcher
that exits immediately. For a simple executable, publishing the shell's `$$`
followed by `exec ./tuntom/tuntom ...` preserves the PID. Execution failure
must become a detectable failed start, not apparent success.

For multiple processes, a supervisor must represent their health and lifetime,
or separate registered scripts must be used. SAS must track process membership
so children are also stopped with the instance, even after their parent exits.

SAS re-reads the PID file under lock; it must not permanently cache an old PID.
It verifies membership in the instance/service and distinguishes PID reuse,
for example using PID plus process start time and an execution group. A PID
file alone is not authority to signal an arbitrary host process.

| Registration/PID state | SAS action |
| --- | --- |
| No registration | Do not start/restart; retain run accounting and status/stop access |
| Registered, no PID, no known live/pending run, service lock available | Start the script |
| Registered, verified live process | Continue supervision |
| Registered, verified process has exited | Restart subject to rate limit |
| Locked by installer | Skip this scan, without waiting indefinitely |
| Invalid PID or ambiguous/foreign process identity | Report error; do not signal or blindly start a duplicate |
| Instance stopped, expired or being removed | Do not start; terminate its tracked helper processes |

Startup needs a bounded pending interval: SAS must not launch another copy while
the first script is still starting or publishing its PID. Failed starts and
rapid crashes require backoff and an exposed failure state.

The scanner's default interval is 60 seconds, configurable on the runner.
Instance stop/expiry and explicit stop requests MUST NOT wait for that scan.

Every start attempt, including an automatic restart, receives a fresh opaque
`run_id`. SAS durably records `(instance UUID, prefix, owner, run_id)` and its
execution-group identity before launching. IDs are never reused after runner
restart, host reboot, prefix reuse or instance removal. A surviving run retains
its identity across runner restart. Accounting is independent of `db.info`, PID
files and workspace existence.

The execution group is a systemd cgroup or equivalently verified containment,
not merely a parent PID or POSIX process group. Stop includes all descendants;
workloads must not escape into unmanaged host services. Missing PID with a known
live/pending run triggers reconciliation, not a duplicate start. PID files are
observations, never authority to signal arbitrary host processes.

## 7. Locking and atomicity

Fabric and SAS use exclusive advisory `flock` locks on the same host filesystem:

* `db.lock` protects registry read-modify-write and prefix allocation.
* `10.lock` protects the complete installation/update/removal of that service,
  and SAS's recheck/start/restart decision.

Lock files are stable inodes. They must not be unlinked, replaced or renamed
until the whole instance is removed. Releasing a lock means closing/unlocking
its descriptor, not deleting the file. Shared storage without reliable flock
semantics is outside this contract.

When both locks are needed, acquire `db.lock` before any service lock. Acquire
multiple service locks in numeric order. Do not reacquire `db.lock` while
holding only a service lock. SAS should use nonblocking acquisition and skip
busy services; it must recheck registration, PID and instance state after
acquiring the locks.

The installer holds the service lock throughout an update. A simple compliant
implementation holds the registry lock for the whole transaction too. Do not
hold these locks while downloading remote payloads; stage downloads first.

SAS must not pass lock descriptors into a long-lived child. The start script
does not acquire `10.lock` recursively. SAS's internal startup tracking prevents
duplicate launches after it releases the startup lock.

Status and stop MUST work while Fabric holds both filesystem locks. Neither API
may acquire `db.lock` or a prefix lock, directly or through a worker. They use
independent durable run records and internal lifecycle synchronization. The
scanner must not wait for filesystem locks while holding an internal lock needed
by status/stop. This prevents Fabric → API → scanner deadlocks.

Stop/start decisions serialize internally. Stop verifies registration withdrawal
by reading the atomically published registry without taking Fabric's locks;
inability to verify withdrawal is not success. The scanner must recheck
registration at launch and never act on stale cached registration.

Registry and script changes use a temporary file plus atomic rename in the same
directory. Rename prevents partial reads; locking prevents conflicting lifecycle
decisions. Add file and parent-directory fsync where power-loss durability is
required.

## 8. First installation

Fabric:

1. Resolve and verify instance coordinates; stage and validate the payload.
2. Acquire registry and chosen service locks; verify prefix ownership again.
   For reused prefixes, reconcile SAS run records and confirm prior groups are
   empty before replacing files. Registration absence alone is insufficient.
3. Install binaries/configuration and executable scripts with restricted modes.
4. Write version metadata. SAS, not Fabric, owns runtime/PID cleanup.
5. Atomically add registrations only after the installation is complete.
6. Release locks. **Do not execute the start scripts.**

SAS's next scan sees the registered service without a PID and starts it.
SAS allocates a new run/runtime directory. The script publishes its PID at
`SAS_MICROSERVICE_PID_FILE`; SAS verifies it against that run's execution group.

## 9. Update

Fabric stages new files before taking locks, then:

1. Acquire registry and service locks; revalidate instance and ownership.
2. Atomically remove its affected registrations from `db.info` temporarily.
3. Query SAS status and request stop for every affected exact owner/run_id.
   Require verified `stopped` or `already_stopped` for all current affected runs.
   Fabric MUST NOT kill processes itself.
4. Only after all affected groups are confirmed empty, update binaries,
   configuration, scripts and version metadata in the installation tree.
   SAS owns old runtime/PID cleanup; Fabric does not edit runtime directories.
5. On success, restore registrations atomically. SAS creates fresh runtime
   directories for the new runs; old PID observations are never reused.
6. Release locks. SAS performs the new starts on its next scan.

Temporarily withdrawing registrations makes a crashed installer fail closed:
locks are released automatically on process death, but SAS will not launch a
half-installed service. If Fabric fails before withdrawing a registration, it
must not yet have modified the installation. Fabric never edits runtime PID files.

For files shared by prefixes 10 and 11, lock and withdraw **both** registrations
before changing those files. Locking only 10 is insufficient for updating a
shared Tuntom binary while 11 still uses it.

On timeout, identity conflict, unknown state or transport failure, Fabric MUST
leave registrations withdrawn and preserve payload, scripts and PID files. It
MUST NOT overwrite/delete files or restore registration based on assumed stop.
Locks can be released for later recovery because registration remains absent.
A lost response is resolved by status/retry with the same run identity; a changed
current run requires fresh reconciliation, not blind retargeting.

## 10. Undeploy

Fabric acquires the registry/service locks, verifies ownership, withdraws its
registrations and requests SAS stop of all affected exact owner/run_id pairs.
Only after verified emptiness may it remove scripts and unreferenced payload
files from the installation tree. SAS handles runtime/PID cleanup. Fabric preserves
other owners and stable lock files. It never removes the SAS-owned workspace.

Removal of a PID file alone no longer disables supervision: a registered service
without a PID is eligible for start. Removing registration alone is not proof
that an already running process has stopped; Fabric must verify termination
before removing its files.

## 11. Status/stop API: normative wire contract

The endpoints in this section use authenticated runner API access. `owner` is an
identity consistency check, not a replacement for authorization. Prefix is a
positive decimal string; instance ID is a full UUID. Every response includes
`contract_version: 3`. The `/v1` runner API namespace and this document's
microservice contract version are independent.

### Status of a prefix or a particular run

```http
GET /v1/instances/{UUID}/microservices/{prefix}
GET /v1/instances/{UUID}/microservices/{prefix}?run_id={opaque-run-id}
```

Without `run_id`, return the latest known run. With `run_id`, return evidence
for exactly that run, never substitute a newer one. Example values:

```json
{
  "contract_version": 3,
  "instance_id": "11111111-1111-4111-8111-111111111111",
  "prefix": "10",
  "origin": "sas-node-1",
  "instance_state": "present",
  "registered": false,
  "owner": "tuntom",
  "run_id": "opaque-run-A",
  "current_run_id": "opaque-run-A",
  "state": "running",
  "process_group_empty": false,
  "verified_at": "2026-10-06T10:00:00Z"
}
```

| Field | Semantics |
| --- | --- |
| `instance_state` | `present`, `removed_clean`, or `unknown` |
| `registered` | Current registration present: true/false; null when unverifiable |
| `owner` | Recorded run owner; registry owner for a never-started slot; null when not established |
| `run_id` | Requested/latest run; null only for never-started or unknown identity |
| `current_run_id` | Latest known run at this prefix, or null if none/unknown |
| `state` | `starting`, `running`, `stopping`, `stopped`, `never_started`, `unknown` |
| `process_group_empty` | true only for verified emptiness, false for known activity, null for uncertainty |
| `verified_at` | RFC3339 UTC time of actual verification, or null |

`removed_clean` requires durable evidence that all instance helper runs were
stopped and the instance cannot launch again. Missing files alone are not proof.
Historical run evidence may remain valid even if current instance state is unknown.

`never_started` with true emptiness is permitted only for an existing,
verifiably managed slot with no recorded/pending/live run. Missing PID alone is
insufficient. Fabric may install without a stop request in this case, only while
holding the locks with registration withdrawn and the instance still present.
There is no synthetic run ID to stop.

Status returns 200 for an established state, including retained historical
evidence. Unverifiable state returns 503 with `error: "state_unknown"` and
`state: "unknown"`. No record/evidence returns 404 with `error: "not_found"`
and `state: "unknown"`. Malformed identifiers return 400. None of these errors
authorizes overwriting/deleting files. The location endpoint's 404 is likewise
not proof of process cleanup.

### Stop an exact run

```http
POST /v1/instances/{UUID}/microservices/{prefix}/stop
Content-Type: application/json

{"contract_version": 3, "owner": "tuntom", "run_id": "opaque-run-A"}
```

`owner` and `run_id` are mandatory. There is no prefix-only, wildcard or
"whatever is current" stop. For an active target, Fabric MUST first withdraw
registration and retain its locks. SAS rejects a still-registered target with
409 `registration_present`; status/stop never wait on Fabric's locks.

Successful synchronous response:

```json
{
  "contract_version": 3,
  "instance_id": "11111111-1111-4111-8111-111111111111",
  "prefix": "10",
  "owner": "tuntom",
  "run_id": "opaque-run-A",
  "result": "stopped",
  "process_group_empty": true,
  "verified_at": "2026-10-06T10:00:02Z"
}
```

| HTTP / result or error | Meaning |
| --- | --- |
| 200 / `stopped` | Exact run and all descendants stopped, group verified empty |
| 200 / `already_stopped` | Exact run has verified-empty completion evidence |
| 409 / `identity_conflict` | Owner mismatch or conflicting run identity; no process signalled |
| 409 / `registration_present` | Active target is still registered; no process signalled |
| 404 / `unknown_run` | No trustworthy record/tombstone; not cleanup proof |
| 503 / `state_unknown` | Host/systemd/membership state cannot be verified |
| 504 / `stop_timeout` | Bounded server stop timeout expired without verified emptiness |
| 400 / `invalid_request` | Invalid/missing identity fields or contract version |

Errors contain `contract_version`, `error`, and `process_group_empty: null`
(or false if activity is known), never a success result. Example:

```json
{"contract_version": 3, "error": "stop_timeout", "process_group_empty": false}
```

Only HTTP 200 plus `stopped`/`already_stopped`, matching requested identity and
`process_group_empty: true` is stop success. A task-queue acknowledgment is not
confirmation. SAS cancels pending starts for the stopped run before confirming.
Server stop timeout must be bounded and documented in the implementation release;
a client timeout alone says nothing about whether server-side stop completed.
The initial implementation bounds internal-lock acquisition to 1 second,
systemd status queries to 3 seconds each and the stop command to 20 seconds.
Allow at least 35 seconds at the client; sasctl does so for exact-run stop.

CLI parity (aliases resolve to UUIDs; historical runs require the UUID):

```sh
sasctl instance microservice tatka-smoula 10 status
sasctl instance microservice <UUID> 10 status --run-id <run-id>
sasctl instance microservice <UUID> 10 stop --owner tuntom --run-id <run-id>
```

Retries of the same tuple are idempotent. If a known completed run A has since
been replaced by B, a repeated stop for A returns `already_stopped` and MUST NOT
touch B. If A has no matching evidence, reject it rather than targeting B.
A response about A is not proof that prefix 10 is empty; Fabric reconciles
current status under its locks and stops all current affected runs before
changing files. It never automatically substitutes a new run ID after conflict.

### Durable evidence after removal and restarts

SAS stores completion tombstones outside the instance workspace, including
instance UUID, prefix, owner, run ID and verified completion. They survive
registry removal, instance deletion and runner restart. V3 requires no automatic
tombstone expiry; introducing retention requires an explicit contract revision.

SAS reconciles execution-group membership before claiming verified state after
restart. PID reuse or a missing unit with ambiguous ownership is not proof.
Boot identity must be considered: old PID observations cannot attach a new host
process to an old run. Historical confirmed completion remains historical evidence.

After Fabric restart, status/stop and retained run identity replace local PID
knowledge. After a lost stop response, Fabric queries/retries the same tuple.
It does not signal host processes itself or infer completion from missing files.

If the instance disappears during a transaction, Fabric stops installing and
uses retained evidence to distinguish clean removal from unknown state. It must
not recreate the workspace or follow a path now owned by another instance.
Unknown state leaves files untouched and is reported as a blocker.

## 12. Security and recovery requirements

The installer and SAS are trusted; appliance workloads must not be able to alter
registrations, scripts, payload executables or lock inodes used for privileged
execution. Protecting individual files is insufficient if a workload can replace
their parent directory. Mounts/permissions must enforce this boundary. Config
secrets require restrictive permissions and must not appear in metadata/logs.

The runtime permission to publish a PID must not allow replacement of privileged
scripts or registration. SAS MUST NOT execute workload-modifiable scripts with
host-root authority. The rootfs release's permissions/mount design must enforce
this independently of whether Smithproxy uses rootfs.

Required mount and permission boundary:

* The installation directory and every replaceable ancestor are controlled by
  trusted host-side SAS/Fabric administration, outside appliance-writable trees.
* Smithproxy gets neither installation nor microservice runtime mounts. In
  non-rootfs mode their host paths MUST be inaccessible, not merely absent at
  `/microservices`. Equivalent aliases, inherited FDs and process/root access
  must not provide a bypass. Read-only would still leak installation secrets.
* A microservice gets a read-only installation mount and only its own writable
  runtime. No alternate writable host-path alias of the installation is allowed.
  The read-only mount includes scripts, binaries, configuration, registry and
  locks. Reading the shared tree implies services within one instance share an
  installation confidentiality boundary; it is not multi-tenant secret isolation.
* Microservices cannot remount the installation writable, modify host mounts,
  escape their cgroup or change another service's runtime. Capabilities, device
  access, syscall restrictions and namespace handles must be limited accordingly.
  Rootfs alone is not sufficient isolation, especially with broad capabilities.
* Installer/scanner path handling rejects symlink/path traversal outside the
  provisioned installation/runtime trees and validates directory identity under
  lock. A script or payload must not resolve into appliance-writable `/work`.
* Registry/lock files remain stable on the host even though the helper sees a
  read-only mount. Helpers never acquire installer locks. Only Fabric and SAS
  participate in that host-side locking protocol.

If the boundary cannot be established or verified, SAS MUST refuse the helper
start with an actionable error. There is no automatic host-root or V2 fallback.

On runner restart SAS reconciles registrations with actual process membership
and PID identity; it must not duplicate surviving helpers. On host reboot stale
PIDs are not accepted as proof of identity. Restart is allowed only after the
instance namespace/workspace is restored and its desired state permits running.
Registration survives reboot; PID observations do not constitute desired state.

## 13. Release gates and simulation acceptance

Fabric can implement against section 11. A versioned rootfs ABI/account/tool/device
manifest is now published by the implementation. On Helmut the operator requested
first integration without a live smoke test. Unit tests cover lifecycle logic;
the following live interoperability and containment checks are still pending.
The specification and actual artifact must be tested with the intended Tuntom
binary; dependency names alone are not sufficient ABI verification.

* First install triggers exactly one SAS start; Fabric starts nothing.
* Rootfs and non-rootfs execution both publish PID through the supplied runtime
  path; attempts to write PID or change scripts in the installation cwd fail.
* A locked update cannot trigger a restart; an installer crash leaves the
  withdrawn service inactive rather than running partially updated files.
* A successful update is restarted by SAS with the new PID and payload.
* Dead-process restart, PID reuse, invalid PID, failed exec and restart storms
  have distinct, safe behavior.
* Undeploy preserves other owners and stops all affected children.
* Runner restart does not duplicate helpers; instance stop/expiry stops them.
* Rootfs contains required libraries/tools and does not expose host credentials.
* Tuntom creates its interfaces in the instance netns and its transport route
  does not loop back into its own dataplane.
* Status/stop succeed without deadlock while Fabric holds registry/prefix locks.
* Owner/run mismatches never stop a different run; delayed stop A cannot stop B.
* Repeated stop A returns `already_stopped` only with verified evidence, and is
  not mistaken for proof that a newer run B is stopped.
* Surviving descendants prevent stop success even if the original PID exited.
* Timeouts, lost replies, missing records and unknown state leave registrations
  withdrawn and payload untouched; retry can recover without duplicate starts.
* Prefix reuse, Fabric restart and runner restart preserve identity safety.
* Shared-binary updates wait for verified stop of every affected service.
* Post-removal queries distinguish `removed_clean` from unknown/missing evidence.
* Runtime PID publication cannot replace privileged scripts/registrations.
* Smithproxy cannot read or modify installation/runtime data through logical
  paths, absolute host paths, aliases or parent-directory replacement in either
  filesystem mode; its ordinary `/work` remains writable.
* A microservice cannot modify its installation or another run's runtime, nor
  bypass read-only mounts with the capabilities required by its network driver.
* Lock files retain their host inode throughout install/update/undeploy; runtime
  cleanup does not delete locks or durable completion evidence.

Finalizing this V3 document does not imply these implementation gates have been
met. No deployment or live integration is enabled by documentation publication.

## 14. Transition from V2

This is a coordinated breaking change, not an automatic scan of old directories:

1. Keep live integration disabled until V3 lookup, execution boundary,
   rootfs baseline and status/stop semantics pass the release gates.
2. For an existing V2 installation, withdraw registrations and verify all old
   runs stopped using the deployed version's exact-run stop contract. If such
   evidence is unavailable, an administrator must reconcile the processes;
   missing PID files do not authorize migration.
3. SAS provisions protected installation/runtime locations and enforces their
   invisibility in the appliance sandbox. Existing instances may require a
   coordinated restart to install those mount restrictions; no silent assumption
   that an already running instance has acquired them is allowed.
4. Fabric installs a trusted payload into `microservices_dir`, adjusts payload
   references from `../tuntom/...` to `./tuntom/...`, and changes PID publication
   from `./10.pid` to `SAS_MICROSERVICE_PID_FILE`. Do not blindly promote files
   from appliance-writable `/work` into a trusted execution tree.
5. Register only in the new installation tree after validation. SAS does not
   discover or execute old `/work/microservices` registrations. Keep old files
   untouched until verified cleanup is authorized; no automatic destructive move.

Fabric uses contract version 3 for new integration. A version mismatch is
rejected rather than silently accepting V2 path/PID semantics. Run identity,
verified-stop guarantees and retained historical completion evidence must not
be weakened during transition.
