# Virtual cable / virtual switch

Both are isolated Ethernet bridges in a dedicated `sas-l2-<UUID>` network
namespace. A cable reserves **at most two endpoints**, even when instances are
stopped. A switch has no two-port limit. Neither object allocates addresses,
routes, NAT, host uplinks or firewall rules. IPv4 and IPv6 frames pass unchanged.

```
instance A netns          isolated cable netns          instance B netns
  cable0  <--- veth --->  port -- br0 -- port  <--- veth --->  cable0
                               no host uplink
```

This is an additional explicit attachment, not a replacement of the current
ingress/egress profile. Choose `none` ingress/egress when creating an appliance
that must have only cable connectivity. Existing instances keep their current
networking. The cable itself is unaddressed; endpoint addressing is separately
managed in Wiring through `00-start`, or declared as guest-managed. Kernel-generated IPv6 link-local
addresses and SLAAC are disabled on newly created cable interfaces. Changing
those settings later is the operator's choice.

## Lifecycle and ownership

- State: `l2-segments.json` next to the instance state directory, atomic JSON,
  mode 0600. The runner process already holds the exclusive state-directory lock.
- Reservations are saved before kernel attachment. One instance/interface can
  belong to only one segment. Repeating the same attachment is idempotent.
- Stop preserves the reservation; deletion of the instance releases it during
  reconciliation. Explicit disconnect releases it immediately after removing
  the owned veth. Failed removal retains the reservation.
- Runner restart leaves links intact. Reconciliation runs every five seconds,
  reconnecting surviving reservations after instance namespace recreation.
- All bridges/ports have ownership aliases. A name collision never authorizes
  overwriting an existing interface. A foreign port in the segment namespace
  disables its bridge and reports an error; SAS does not delete the foreign port.
- This prevents accidental attachment through SAS. It is not a security boundary
  against host root, which can change Linux networking outside SAS. External
  tampering is detected at the next reconciliation, not synchronously.
- Delete a segment only after disconnecting its reserved endpoints. Empty
  segments are intentionally retained until explicitly deleted.
- New interfaces use Ethernet MTU 1500. This version does not configure VLANs,
  spanning tree, MTU negotiation, or guest routing. Avoid intentional L2 loops.

## Console and CLI

Console: **Network → Wiring** (`/network/wiring`; `/l2-segments` remains an alias).
Search by connection name, instance name/UUID or interface. Endpoint cards show
actual observed attachment state separately from persistent reservations; cables
show their vacant ends. Each end has an IP/routing editor and a link to the
shared IPv4/IPv6 inventory. Select a managed instance and a new
interface name (`cable0` by default). Operations appear in the existing task
queue. Refresh reloads observed state; errors are visible and never reported as
successful attachment.

```
sasctl --wait l2 create virtual-cable blackbox-link
sasctl --wait l2 attach blackbox-link INSTANCE_A cable0
sasctl --wait l2 attach blackbox-link INSTANCE_B cable0
sasctl l2 show blackbox-link
sasctl --wait l2 detach blackbox-link ENDPOINT_UUID --yes
sasctl --wait l2 delete blackbox-link --yes

sasctl --wait l2 create virtual-switch lab-network
```

Managed normal instances are currently supported through veth. Orphans are not
adopted. QEMU is currently an image catalogue, without VM lifecycle: TAP requests
are explicitly rejected rather than creating an unused, apparently working port.
TAP attachment must be implemented together with QEMU process/FD ownership;
it will consume the same endpoint reservations. Test Drives are not yet connected
to this lifecycle. Profile/spawn-time bindings are described in `WIRING_STARTUP.md`.

## Authenticated API

Every mutation queues a task and responds **202** with `task_id`, using the usual
task result endpoints. Repeated identical in-flight requests are deduplicated.
GET reads durable observed state without invoking `ip` or waiting on kernel work.

| Method | Path | Body |
|---|---|---|
| GET | `/v1/l2-segments` | — |
| POST | `/v1/l2-segments` | `{"kind":"virtual-cable","name":"blackbox-link"}` |
| GET | `/v1/l2-segments/{id}` | — |
| POST | `/v1/l2-segments/{id}/endpoints` | `{"instance_id":"UUID","interface":"cable0"}` |
| DELETE | `/v1/l2-segments/{id}/endpoints/{endpoint_id}` | — |
| DELETE | `/v1/l2-segments/{id}` | — |

If attachment fails, the task fails **but the reservation remains**. Inspect the
segment error, fix the cause, retry or explicitly disconnect the reserved port.

## Tests

```
console/.venv/bin/python -m unittest discover -s tests -p 'test_l2*.py' -v
SAS_L2_INTEGRATION=1 .venv/bin/python -m unittest discover -s tests -p test_l2_integration.py -v
```

The opt-in root integration test creates only disposable namespaces. It checks
dual-stack traffic, absence of automatic addressing, capacity, reconciliation,
foreign-port fail-closed behavior, stop/recreation and cleanup. It does not touch
host routes, firewall settings or any running appliance.
