# systemd deployment

The two units are deliberately independent:

```text
sas-runner.service  (root, required)  <- private API -> Capture Zone Portal
        ^
        `-- sas-console.service (sas-console, optional admin UI)
```

Stopping or disabling `sas-console.service` has no effect on the runner or on
Smithproxy instances. A normal runner restart also preserves active transient
Smithproxy units, namespaces and routes. Full teardown remains explicit through
`CZ_RUNNER_STOP_INSTANCES_ON_EXIT=1` and must not be set in normal service
configuration.

## Deployment lifecycle

Managed instances now have a durable desired state and private deployment
manifest. This is independent of the `persistent` flag (which controls cleanup
of terminal records, not whether a running deployment survives reboot).

| Event | Behaviour |
| --- | --- |
| Runner restart / code upgrade | Adopt active units; do not restart their processes or networking. |
| Host reboot | Recreate intended-running managed deployments with the same UUID and retained network leases. |
| Interrupted spawn | Persisted spawn intent is retried; already-active units are adopted. |
| Explicit stop / CLI clean shutdown | Desired state becomes stopped; no resurrection. |
| Expired deadline | Stop the whole Slice; do not restore it after reboot. |
| Recovery failure | Retain workspace and leases, expose `recovering` and an error; retry with backoff (up to 5 minutes). |
| Process crash | Existing auto-restart policy/limit applies. A failed deployment retains its files until explicit stop or expiry. |

Recovery uses frozen launch arguments, **not the current runtime profile**.
The existing config, work files, captures, certificates and rootfs selection are
reused without rendering or native-save. Profile edits therefore cannot silently
modify a deployment during recovery. Binary libraries and network allocation
metadata must remain available, along with the workspace. Missing workspace or
config blocks recovery rather than inventing replacements.

TTL is an absolute wall-clock deadline. A separate transient systemd timer stops
the Slice even if the runner is down. Restart does not extend TTL; use the
extension action. Timers are recreated after host reboot. Network/resource
cleanup happens when the runner next reconciles. Unlimited deployments have no
deadline timer. A host crash naturally loses process memory and live connections;
this is restoration, not VM/process checkpointing.

Only one runner may own a state directory (advisory process lock). Instance JSON,
deployment manifests and network leases use fsync + atomic replacement. API and
`sasctl instance list/show` expose observed state and `desired_state`; manifests
contain private launch data and are never included in instance responses.

Test Drives retain their temporary lifecycle; they are not reboot-restored.
Old instances without deployment manifests remain discoverable/adoptable, but
cannot be safely recreated. Respawn them through the upgraded runner before
expecting reboot restoration. Do not move a live workspace: configs and mount
bindings contain absolute paths. In particular, `/tmp` and `/run` development
workspaces are **not** reboot-durable.

## Files and identities

Expected production layout:

```text
/opt/smithproxy-appliance/                 application checkout, root-owned
/etc/smithproxy-appliance/runner.env       0600 root:root
/etc/smithproxy-appliance/runner-client.env 0600 root:root
/etc/smithproxy-appliance/console.env      0600 root:root
/etc/smithproxy-appliance/source-ips.json  0640 root:root
/etc/smithproxy-appliance/smithproxy.cfg.in 0640 root:root
/var/lib/smithproxy-appliance/             runner state, created by systemd
/var/lib/smithproxy-appliance/state/       observed + desired instance JSON
/var/lib/smithproxy-appliance/state/deployments/ private frozen launch manifests
/var/lib/smithproxy-appliance/instances/   durable instance workspaces
/var/lib/sas-console/                      admin JSON, created by systemd
```

The console account is a locked system identity with no shell. Example setup
commands are shown for a future appliance installation; do not run them on a
development workstation:

```bash
groupadd --system sas-console
useradd --system --gid sas-console --home-dir /nonexistent \
  --shell /usr/sbin/nologin sas-console

install -d -o root -g root -m 0755 /opt/smithproxy-appliance
install -d -o root -g root -m 0700 /etc/smithproxy-appliance
install -o root -g root -m 0600 deploy/runner.env.example \
  /etc/smithproxy-appliance/runner.env
install -o root -g root -m 0600 deploy/runner-client.env.example \
  /etc/smithproxy-appliance/runner-client.env
install -o root -g root -m 0600 deploy/console.env.example \
  /etc/smithproxy-appliance/console.env
```

Generate independent secrets for `CZ_RUNNER_TOKEN` and
`SMITHPROXY_APPLIANCE_CONSOLE_SECRET` with `openssl rand -hex 32`. The runner
token is the only secret shared between the services. The console never reads
the runner's root-only settings.

Install console dependencies, including the production Gunicorn server, in the
console virtual environment:

```bash
python3 -m venv /opt/smithproxy-appliance/.venv
/opt/smithproxy-appliance/.venv/bin/pip install \
  -r /opt/smithproxy-appliance/requirements-runner.txt
python3 -m venv /opt/smithproxy-appliance/console/.venv
/opt/smithproxy-appliance/console/.venv/bin/pip install \
  -r /opt/smithproxy-appliance/console/requirements.txt
```

Install host runtime/build dependencies separately (systemd, iproute2, nftables,
the required compiler and Smithproxy libraries; optional tuntom/QEMU/debug tools).
The `gcore` command from GDB is required when live forensic snapshots are used.
The service does not install system packages. Stage the template and source pool
at the configured `/etc` paths before enabling it. Protect `/opt` and `/etc`
against writes by the console account. The root runner intentionally shares the
host mount namespace: filesystem sandboxing directives that create another mount
namespace would hide its persistent netns mounts from PID 1. Individual appliance
units remain isolated; the console retains its own service sandbox.

`Type=notify` reports runner readiness only after API and WebSocket listeners are
bound. Deployment reconciliation continues in the background. Network-online is
an ordering prerequisite, not proof that an external Fabric switch is reachable.

## Unit installation

```bash
install -o root -g root -m 0644 deploy/sas-runner.service \
  /etc/systemd/system/sas-runner.service
install -o root -g root -m 0644 deploy/sas-console.service \
  /etc/systemd/system/sas-console.service
systemctl daemon-reload
systemctl enable --now sas-runner.service
systemctl enable --now sas-console.service  # optional
```

For a headless appliance, do not enable `sas-console.service`. The portal talks
directly to the runner API. Before binding the API to a private network address,
put authenticated TLS/mTLS transport in front of it; the runner itself currently
provides bearer authentication over plain HTTP/WebSocket.

Useful checks:

```bash
systemctl status sas-runner.service sas-console.service
journalctl -u sas-runner.service -u sas-console.service -f
systemd-analyze security sas-runner.service sas-console.service
```

Acceptance checks on a disposable deployment host (not the development PC):

1. Spawn finite-TTL and unlimited managed instances; edit their RW config/work.
2. Restart only `sas-runner`; verify the appliance PIDs and files are unchanged.
3. Stop the runner beyond the finite TTL; verify its Slice is stopped by the timer.
4. Reboot the host; verify the unlimited deployment restores with the same UUID,
   addresses and files, while expired/manually stopped deployments remain stopped.
5. Break an artifact path or link prerequisite; verify visible recovery errors,
   retained data/leases and successful retry after repair.

The unit-test suite simulates these lifecycle transitions with a fake backend;
it does not substitute for this privileged host/reboot acceptance test.
