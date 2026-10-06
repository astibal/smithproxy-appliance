# SAS development and deployment

Authoritative development checkout:
`/home/astib/Documents/Capture.Zone/src/smithproxy-appliance` on the local PC.
Helmut `/opt/smithproxy-appliance` is the deployment/integration-test checkout.

- Edit, test, commit and push from the local PC. Deploy reviewed changes to
  helmut; do not edit application code there as the normal workflow.
- Do not start the old local SAS services; live services run on helmut.
- Before editing, inspect `git status` and the relevant diff. Uncommitted changes
  may belong to another task. Do not reset, clean, overwrite, or rsync-delete them.
- Do not deploy by blindly syncing the old local checkout over this directory.
- Coordinate overlapping edits and service restarts with other active tasks.
- Runner and console are `sas-runner.service` and `sas-console.service`.
- Appliance Slices are independent; do not stop them for a code update.
- Runtime data and secrets are outside Git in `/var/lib/smithproxy-appliance`
  and `/etc/smithproxy-appliance`. Never print or commit private keys/tokens.
- Console listens on [::]:5000 (dual-stack); runner API stays on loopback.
- Console is plain HTTP: use a trusted network or SSH forwarding until TLS is configured.
- Read `docs/HELMUT.md` before changing deployment paths or network settings.
