# Declarative Wiring at startup

Runtime profiles store optional `wiring` bindings:

```json
{"wiring": [{"segment_id": "<cable-or-switch-UUID>", "interface": "lab0"}]}
```

They do not reserve capacity until a spawn. A cable still has at most two ends,
including reservations for stopped instances. A spawn reserves **all** its
bindings under one catalogue transaction before creating a namespace. A full or
missing segment rejects the start without partially reserving other segments.

Profiles contain connectivity only, not shared IP addresses. Spawn may override
`wiring` with another list and include optional per-port `addressing`:

```json
{"wiring": [{"segment_id": "<UUID>", "interface": "lab0",
 "addressing": {"mode": "sas", "addresses": ["10.50.0.1/24", "fd42:50::1/64"], "routes": []}}]}
```

Omitted `wiring` inherits the profile; explicit `[]` disables profile bindings
for this spawn. Maximum 16 ports; interface names must be unique and may not
use `lo`, `di0`, `do0`, `fabric0` or `transport0`. Existing interface ownership
checks still reject collisions; no existing adapter is taken over.

Order: reserve → namespace/driver setup → connect ports → `00-start` addressing
→ launch program. Connection failure prevents launch. Failed-spawn cleanup
releases only its own reservations; addressing records are retained as detached
records for diagnostics. If cleanup itself fails, reservations remain visible.

Disabling `00-start` does not disable physical connections, only its addressing
management. The L2 hook precedes the service check. Boot recovery initializes
both hooks before restoring deployments. Later explicit disconnects are not
undone by the original recipe: current Wiring reservations and port-address
records are authoritative. `instance.wiring` is the initial spawn snapshot.

Ingress/egress profiles are not implicitly changed. Use suitable `none` drivers
if the instance must communicate exclusively through cables. This lifecycle
currently covers managed Smithproxy instances, not Test Drives or QEMU/TAP.

## GUI and CLI

Runtime profile forms have a Wiring selector. The spawn dialog normally inherits
it; an explicit override checkbox enables custom port bindings and addresses.
No implicit reassignment occurs. GUI initial routes are empty; configure routes
later in Wiring or provide them through API/CLI.

```
sasctl profile create NAME --build BUILD --config-id CONFIG --wiring-file links.json
sasctl profile update PROFILE --wiring-file links.json
sasctl instance spawn ... --wiring-file spawn-links.json
```

The files contain the JSON **list**, not an enclosing `wiring` object. Omitting
the option on profile update preserves existing bindings. Empty list clears them.
API uses the `wiring` property on runtime-profile create/update and instance spawn.
Runtime profile changes affect future spawns only.
