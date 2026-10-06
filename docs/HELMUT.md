# Helmut: SAS deployment and integration testing

## Current workflow (2026-10-05)

Development and all Git operations (commit/push) run on the local PC in
`/home/astib/Documents/Capture.Zone/src/smithproxy-appliance`.
The complete source state, including uncommitted work and rootfs commit
`5ff2649`, was retrieved from helmut. Edit and run unit tests locally, then
deploy reviewed changes to `/opt/smithproxy-appliance` on helmut.

Before deployment compare remote status/diffs against the previous deployment;
preserve unexpected remote edits. Do not blindly overwrite the checkout or use
rsync --delete. Never deploy local virtualenvs, runtime data, credentials, or
`.git` over the server. Restart only the affected services after verification;
running appliance Slices must remain independent of runner updates.

The sections below record the earlier migration and server layout. Instructions
calling helmut the development source are superseded by this workflow.

## Migration history and server layout

Migration verification: 110 tests passed on Helmut; authenticated API and login
page checked. A temporary Test Drive started successfully, retained its PID
across runner restart, and was deleted afterwards. CMake Release configuration
also completed successfully. No full compilation or host reboot was performed.
Old local Razor runner and console were stopped; their files were retained.

Pending operator approval: host IPv4 and IPv6 forwarding are still disabled.
Global forwarding changes host-wide routing behaviour; it has NOT been enabled.
Routed traffic through the host needs this step before network acceptance tests.

Migrated from Razor on 2026-10-05, including Git history, uncommitted stage-1
changes, build/config/certificate/profile libraries and console admin records.

```
ssh helmut
cd /opt/smithproxy-appliance
git status --short
console/.venv/bin/python -m unittest discover -s tests -q
```

Inspect and preserve existing remote changes before deploying. No Git credentials/private SSH keys were
copied; configure repository access separately if required.

- Code: `/opt/smithproxy-appliance`
- Data: `/var/lib/smithproxy-appliance`
- Secrets/settings: `/etc/smithproxy-appliance` (root-only)
- Console admins: `/var/lib/sas-console/admins.json`
- Services: `sas-runner.service`, `sas-console.service` (enabled on boot)
- Runner HTTP/WebSocket: `127.0.0.1:9080`, `127.0.0.1:9081`
- Console: `[::]:5000` (dual-stack, explicitly enabled by the operator)
- Direct access: `http://192.168.155.118:5000`; plain HTTP, trusted networks only.
- SSH forwarding remains available; runner API is not publicly bound.
- Initial build parallelism: 2 jobs for this small host.

From the desktop:

```
ssh -N -L 15000:127.0.0.1:5000 helmut
# Open http://127.0.0.1:15000
```

Original runtime archive and migration scripts are kept root-private under
`/srv/sas-migration-20261005-Z8hAfo`. The archive checksum is in `runtime.sha256`
(its filename records the original local location). The original data were not
removed from Razor.

Imported instance records are intentionally stopped and persistent; they must
not automatically duplicate a Fabric identity still running elsewhere. Address
leases were reset on the new host. Imported host INPUT enforcement is disabled
for safe SSH access; authorization records and forward enforcement are retained.
Review host INPUT policy before enabling it. No other firewall tables are flushed.

Original configs/build metadata keep their bytes and hashes. Their old absolute
`/tmp/capture-zone-runtime` paths resolve through a compatibility symlink to
`/var/lib/smithproxy-appliance`, recreated by
`/etc/tmpfiles.d/sas-runtime-compat.conf` on boot. New runtime data use `/var/lib`.
Old CMake caches were preserved under `migration-build-cache`, not reused.

Default route is via `192.168.155.1` on `enp1s0`; its dedicated netplan overlay is
`/etc/netplan/90-sas-default-route.yaml`. Other interface configuration was not
overwritten. Keep this overlay separate from other tasks' network edits.

A future extra disk should preferably host `/var/lib/smithproxy-appliance`;
code in `/opt` is small. Preserve paths, permissions and the compatibility alias.
