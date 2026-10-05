# Tuntom Fabric boundary

Tuntom support is split into two lifecycle domains.

```text
host / Fabric domain
  tuntom switch @ switch IP
  fabric state
  topology, ports, health and diagnostics UI
             │
             │ unique endpoint package
             │ (port + tunnel ID + switch IP + secret)
             ▼
per-run Slice
  fabric0 ─ tuntom relay ─ local socket ─ divert adapter ─ di0/do0 ─ smithproxy
```

The runner owns the per-Slice relay and adapter. `Tuntom Binaries` tracks an
immutable source commit and archives both `tuntom` and
`tuntom-divert-adapter`; a matching switch can later be added to the same
component bundle.

The future Fabric subsystem should own:

- switch build selection, start/stop/restart and systemd unit;
- socket readiness and compatibility with the selected adapter build;
- attachment inventory, link state, counters and fabric topology;
- a dedicated Fabric UI and API, not fields bolted onto an instance detail;
- reconciliation after runner or console restart.

VIA consumes ingress and egress together. Its start request carries only a
`headless_endpoint_id`. The imported package supplies the unique Fabric port,
tunnel ID, switch IP and credential; SAS atomically reserves it before spawn
and binds it to exactly one Slice after start. A stopped Slice keeps ownership,
so the same tunnel identity cannot silently appear in another instance. SAS
allocates the `fabric0` address and local relay socket from its own pools.

`ipvlan-l3` also needs one ordinary upstream route for the whole configured
Fabric transport prefix towards the SAS host. This is a stable underlay route,
not per-flow policy routing. `ipvlan-l2` can instead be used when the switch is
directly reachable on the parent's L2 segment.

Ordinary Tuntom remains a separate, one-sided ingress or egress driver whose
start request supplies both TUN addresses, the peer host and secret.

Slices only reference the fabric endpoint and immutable Tuntom build. They must
remain independently stoppable and must not receive broad management access.
Restarting the runner or console must not stop either live Slices or the switch.
