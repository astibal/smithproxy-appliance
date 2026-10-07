# Read-only instance lookup

## Persistent instance aliases

```sh
sasctl instance alias <UUID> tatka-smoula
sas_which_instance tatka-smoula --microservices-dir
sasctl instance show tatka-smoula
sasctl instance alias tatka-smoula ''  # clear the alias
```

Aliases use 1–63 lowercase ASCII letters, digits and hyphens, starting with a
letter; UUID-shaped names are reserved. They are unique across retained instance
records, including stopped instances. Exact aliases resolve to canonical UUIDs;
UUIDs, paths, systemd units, deadlines and persistence settings do not change.
Naming an instance does not make its TTL unlimited or enable boot recovery.
The authoritative alias is stored atomically in the instance JSON, not in a
second symlink index. Clearing/deleting the record frees the alias for reuse.
Fabric should pin the UUID after lookup, not reuse an alias for a delayed stop.

Authenticated `POST /v1/instances/{UUID}/alias` accepts `{"alias":"tatka-smoula"}`;
an empty string clears it. Location accepts an exact UUID or alias and returns
both `id` (canonical UUID) and `alias`. Other direct instance API paths continue
to use UUIDs; sasctl resolves aliases before making those requests. The console
shows and searches aliases alongside UUIDs. Set/change aliases through CLI/API.

Install the deployment launcher:

```sh
install -m 0755 deploy/sas_which_instance /usr/local/bin/sas_which_instance
```

The launcher uses the standard `/opt/smithproxy-appliance` layout and console
Python environment (which contains the existing sasctl dependencies).
The helper uses sasctl credentials; on the appliance it defaults to the protected
`/etc/smithproxy-appliance/runner-client.env`. Do not make that file world-readable.
Use `SASCTL_CONFIG` for a different authenticated runner/context configuration.

```sh
sas_which_instance <full-instance-UUID>
sas_which_instance <full-instance-UUID> --namespace
sas_which_instance <full-instance-UUID> --namespace-path
sas_which_instance <full-instance-UUID> --work-dir
sas_which_instance <full-instance-UUID> --microservices-dir
sas_which_instance <full-instance-UUID> --origin
sasctl instance location <full-instance-UUID> --work-dir
```

API: authenticated `GET /v1/instances/{id}/location` (same runner authorization
as other instance queries). Unknown/invalid UUID returns 404. Full JSON contains
`id`, last-reconciled `state`, `namespace`, `namespace_path`, `work_dir`, `unit`,
`slice`, `origin`, `namespace_exists`, and `work_dir_exists`. `origin` is the
runner's hostname, not the CLI client's hostname. Paths refer to the runner
host, not the client. This endpoint covers managed instances, not Test Drives.

Selectors `--unit`, `--slice`, `--state` are also supported. No selector prints
JSON; a selector prints just the value. Errors go to stderr with nonzero exit
status. Missing namespaces/directories cannot be selected, even if a stopped
instance record still exists. This is a snapshot, not a lifecycle lock: an
instance may stop immediately after lookup. No process is started or attached.

Microservice installation staging uses a separate host directory:
`/var/lib/smithproxy-appliance/microservices/<UUID>` on Helmut. Lookup returns
`microservice_contract_version: 3`, `microservices_dir` and
`microservices_dir_exists`. The version identifies the V3 installation layout,
NOT by itself a claim that execution is available. With no prepared rootfs,
`microservice_mode` is `installation_only`, `microservice_execution_enabled`
is false, and capabilities are false. With the V3 backend and an active rootfs
appliance, mode is `managed`; consult the explicit capabilities. Helmut has this
backend enabled for `tatka-smoula` (2026-10-07). Fabric installs/registers but
never executes the scripts itself. No live smoke workload was run.
Unprovisioned directories are not created by lookup and the CLI selector fails.

```sh
sasctl instance microservice tatka-smoula 10 status
# After Fabric withdraws registration under its locks:
sasctl instance microservice tatka-smoula 10 stop --owner tuntom --run-id <exact-run-id>
```

Scripts publish PID using `SAS_MICROSERVICE_PID_FILE`; writable state belongs
under `SAS_MICROSERVICE_RUNTIME_DIR`. Tuntom's `--tun-netns` target is the mounted
handle in `SAS_INSTANCE_NETNS`, not a host `/run/netns` path. See the contract for
image ABI, UID/GID, permissions, exact-run identity and update ordering.

Microservice transport runs in the **host/root network namespace** while retaining
its own rootfs. `namespace_path`/`SAS_INSTANCE_NETNS` identify the target for TUN
devices, not the transport process. Profile transport-veth namespaces are not used
for microservice execution. The current privileged TUN bootstrap also has host
network privileges; it is restricted to trusted Fabric-installed programs.
