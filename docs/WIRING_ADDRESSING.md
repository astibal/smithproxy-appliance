# Wiring port addressing

Network → Wiring stores addressing by **instance UUID + interface**, separately
from segment membership. Disconnecting/deleting an empty cable retains the port
configuration; connecting the same port elsewhere reuses it. There is no implicit
rewiring or replacement of occupied interfaces. State is atomic JSON in
`wiring-addresses.json` beside `l2-segments.json`; no database.

## Desired state

- `sas`: `00-start` applies IPv4/IPv6 addresses and explicit routes.
- `guest`: declarations only. SAS makes no addressing changes, including on
  transition from SAS management; existing configuration is handed off.
- `none`: remove only addresses/routes previously managed on this tagged port.
  Unknown addresses/routes are not flushed.

The editor accepts one IP/prefix per line; route lines contain destination/prefix
and an optional gateway. Routes use main table, protocol 242, metric 42760. No
DHCP, NAT, forwarding enablement, host routes or implicit gateway is created.
Routes are added, not blindly replaced: a conflicting existing route fails and
the desired configuration stays saved. IPv6 DAD must complete (five-second
timeout); duplicate/tentative addresses cannot be reported as ready.

Saving is an authenticated queued task: persist desired values, then request
the one instance's microservice check. Errors remain visible in the task and
port status. A stopped instance or disabled `00-start` retains pending config.
The next enabled check applies it. Periodic checks repair managed addressing.
The console keeps form input after submission, including failed validation.

`00-start` attaches reserved cable ports before applying their addressing and
before the subsequent proxy launch. Runtime profiles can declare segment/port
bindings and spawn can override them, including initial addressing; see
`WIRING_STARTUP.md`. The normal L2
reconciler can also attach ports to live instances. Test Drive/QEMU endpoints
are not part of the L2 lifecycle yet.

## Inventory and warnings

Inventory includes attached and disconnected port declarations. Summary branches
(/8, /16 for IPv4; /32, /48 for IPv6) are not allocated ranges. Actual prefixes
are nested by containment, never by rounded JavaScript numbers. Selecting a use
opens its endpoint editor. Multiple addresses in a subnet on the same cable are
normal; identical IPs and overlaps on different segments produce advisory,
nonblocking warnings. Namespace isolation can make duplicates intentional.

Records distinguish source, desired/declared type, managed status, server,
namespace, interface, instance and optional segment/endpoint. Observations live
separately; the targeted Wiring readiness check is not global discovery.
Guest declarations are not presented as verified. Future discovery can add
read-only observations without taking ownership. Unlisted means only
**not recorded in Wiring**, never globally free.

## API and CLI

```
GET  /v1/l2-segments/addressing
POST /v1/l2-segments/addressing/preview
     {"addresses":["10.50.0.1/24"],"segment_id":"…","endpoint_id":"…"}
POST /v1/l2-segments/{id}/endpoints/{endpoint_id}/addressing
     {"mode":"sas","addresses":["10.50.0.1/24","fd42:50::1/64"],"routes":[]}

sasctl l2 addressing
sasctl --wait l2 configure-port CABLE ENDPOINT --mode sas \
  --address 10.50.0.1/24 --address fd42:50::1/64 \
  --route '10.60.0.0/16 10.50.0.254'
```

Preview is a bounded read-only request (no kernel work). Updates return 202/task
ID, use existing task deduplication, and require the same admin authentication
as the rest of the L2 API. Discovery is explicitly `discovery_enabled: false`.
