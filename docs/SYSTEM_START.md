# Per-instance checks and reserved `00-start`

## Explicit checks

`POST /v1/instances/{uuid}/microservices/check` queues a check of exactly one
managed instance. It returns HTTP 202 and the usual task object. Concurrent
identical requests deduplicate. It never requests a global scan. Components can
use this endpoint later; cable/network mutations do not invoke it automatically
in this release.

The check shares periodic scanning's locking, identity verification and restart
backoff. Installer locks produce `state: deferred` with `blocked` prefixes, not a
false success. A stopped instance's helpers are stopped/verified, not started.
Unknown UUIDs fail. The normal periodic check remains as a safety net.

```
sasctl --wait instance check-microservices INSTANCE
sasctl instance system-start INSTANCE status
sasctl --wait instance system-start INSTANCE disable
sasctl --wait instance system-start INSTANCE enable
```

Console: instance **Diag** contains the check button, `00-start` state and an
enable/disable control. All mutations run through the task queue.

## Reserved system service

`00-start` is built into trusted runner code. It is a short reconciliation job,
not a permanently running process and **not a guest-editable `00-start.sh`**.
It has no invented PID or external V3 run identity. Prefix `00` stays reserved;
Fabric's `db.info` cannot register or replace it. Existing numbered external
microservices keep their V3 contract.

It is enabled implicitly for normal managed Smithproxy instances and Test Drives,
including non-rootfs instances. It runs before the proxy process is launched,
before external microservices are checked, periodically, and on explicit checks.
An enabled failed network preparation blocks initial process launch / new helper
starts; it does not kill an already running proxy.

For Test Drives, use the equivalent `/v1/test-drives/{uuid}/microservices/check`,
`.../00`, `.../00/configure` endpoints and `sasctl test-drive check-microservices`
or `sasctl test-drive system-start`. Test Drives currently have no external
Fabric microservice supervisor; their check explicitly reports that limitation.
QEMU images and blackboxes are not scanned or addressed. Future managed workload
types must explicitly bind this controller to their lifecycle and desired plan.

## Configuration ownership

Desired addresses/routes come from SAS's durable network allocation and instance
configuration, **not** by adopting whatever currently exists inside a namespace.
Missing leases or interfaces cause an error rather than guessed addresses.
The controller compares actual and desired state and applies only discrepancies:

- IPv4 and IPv6 addresses on SAS-owned interfaces;
- namespace default and authorized-source return routes;
- transparent-proxy local routes and marked lookup rules;
- Test Drive source-address policy routes (table 101);
- VIA's configured fabric endpoint route and local address.

No host routing, firewall/NAT changes, new interfaces, or IP allocation for
cables/switches are performed by this service. Unknown/custom interfaces and
addresses are not flushed. A conflicting reserved policy-rule priority fails
closed instead of silently replacing an unrelated rule. This is reconciliation
of existing network-driver configuration, **not yet a general port/IPAM editor**.
Driver bootstrap still creates the interfaces and their initial network setup.

Each instance has a root-owned atomic JSON status/opt-out record in
`system-start/<uuid>.json`, next to its state directory. It survives runner
restarts. Disabling the service retains the current network and does not grant
the workload additional capabilities. Enabling marks it pending; explicit check
can apply it immediately, otherwise the next periodic check does so.

```
POST /v1/instances/{uuid}/microservices/00/configure
{"enabled": false}
```

Normal instance spawn also accepts `system_start_enabled: false`, exposed as
`sasctl instance spawn --no-system-start ...`.

Smithproxy's existing capability bounding set excludes `CAP_NET_ADMIN`, so a
regular workload cannot change IPs/routes even if an `ip` binary is present.
Removing that binary from future workload rootfs images is optional hygiene,
not the enforcement boundary. Privileged host diagnostics / NetNS shell and
trusted Fabric network adapters remain explicit administrative exceptions.
This change does not strip tools or privileges from the separate adapter rootfs.

## Verification

Unit tests cover targeted checks, installer locks, duplicate starts, ordering,
disabled-state persistence, missing leases, conflicting rules and no-op behavior.
The opt-in `SAS_SYSTEM_START_INTEGRATION=1` kernel test uses only disposable
namespaces and dummy links, exercising dual-stack repair and repeat-check no-ops.
