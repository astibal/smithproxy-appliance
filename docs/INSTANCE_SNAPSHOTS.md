# Instance upgrades and snapshots

Managed instances expose versioned `smithproxy`, `tuntom`, and imported `program`
components. Router and webfsd are appliance tools, not independently upgradeable
components. An upgrade changes one selected component, restarts the affected
processes, and preserves the instance ID, configuration, workspace, deadline,
namespace, addressing and Wiring.

The console orders matching Git refs (or matching imported-program names) first
and marks them as the same branch. Other archived versions remain selectable.

## Named snapshots

Snapshots are materialized below the private runner state directory. Each has a
UUID, operator name, parent UUID and a human-readable `snapshot_path`. Restoring
a snapshot moves the instance to that path; a later snapshot creates a branch.
Deleting a parent does not invalidate a child because every snapshot contains a
complete workspace copy and deployment manifest.

Modes:

- `hot`: fsync attempt and best-effort copy while processes run.
- `cold` (default): freeze/thaw the main systemd unit; microservices continue.
- `stop`: stop main/Tuntom processes and microservices without deleting the
  namespace or allocations, copy, then restart and reconcile them.

Restore always uses the stop path. Special files such as sockets are not copied;
skipped or concurrently changed paths are recorded in snapshot warnings.

## Forensic snapshots

`forensic=true` captures live cores with `gcore` before lifecycle quiescing.
Each active Slice member is identified by PID and process start time. The package
also includes selected `/proc` records (including environment and mappings), FD
and namespace links, executable and workspace hashes, systemd state/journal and
namespace network state. Core collection may pause each process and may expose
credentials or decrypted traffic. Snapshot storage and private manifests remain
root-only; public API views omit deployment secrets and configuration values.

Collector failures retain the materialized snapshot as `forensic_status=partial`
with per-collector errors. Existing evidence is never silently discarded.
