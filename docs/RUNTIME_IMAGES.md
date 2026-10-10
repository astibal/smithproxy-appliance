# Runtime images, stage 1

Profiles select `rootfs_variant`: `barebone`, `utils`, or `network`.
Saving a rootfs profile prepares an image on the runner origin. ELF dependencies
are inspected with readelf; input executables are not run during construction.
Images live next to the runtime-profile store under `runtime-images/<sha256>`.
An image is a snapshot, not a bind mount of host libraries. It contains no package
manager, compiler or documentation. Smithproxy keeps its small existing base,
including the shell required by its auxiliary launcher, certificates and OpenSSL
providers; the selected tools are added to a new image, never to that base.

The profile pins `rootfs_image`. Renaming or resaving without changing the program,
build or variant reuses that image. `refresh_rootfs: true` explicitly rebuilds from
current sources. Existing deployments keep their saved image path and argv. Old
images are retained; image inventory/selection and garbage collection are future
work. `utils` adds basic shell/file utilities; `network` adds ip, ss, ping, nft,
iptables and ip6tables plus xtables modules. Missing tools fail preparation.
Installing a tool does not grant additional capabilities.

## Small programs

Router runs `/usr/bin/sleep infinity`; the kernel does the routing. The trusted
runner enables IPv4/IPv6 forwarding only inside its owned netns. Webfsd runs in
foreground, serves `/work`, and writes access logs to the journal. The webfsd
executable comes from PATH or `program-sources/webfsd` next to the profile store.
On Helmut the latter points into a retained, extracted APT package; no host webfs
service was installed. This is not yet an automatic APT import/update library.

Both use the existing instance store, Slice, task queue, Wiring, restart, deadline
and recovery machinery. They have no implicit host link, default route or NAT.
Their rootfs is RO, with per-instance `/work` and `/logs` writable, private tmp,
devices and PIDs. `PrivatePIDs=yes` requires systemd 257+ (Helmut has 259).
They do not have a Smithproxy CLI; logs and NetNS diagnostics remain available.
Native runtime definitions still use the historical unit/config-sentinel naming
internally; no Smithproxy process or native-save is involved for these programs.

`sasctl profile create NAME --application router --rootfs-variant barebone`
and `--application webfsd --http-port 8000` use the same API as the console.
The normal profile spawn command starts them. Program TTL currently comes from
the profile. Normal managed-instance cleanup semantics apply to `/work`; it is
not a persistent volume or a replacement for profile file storage.

Docker, arbitrary script/directory imports, remote origins, automatic APT tracking,
and configurable additional host mounts are outside this stage.

## Portable rootfs import

A complete SAS runtime image can be imported from a tar archive containing
`runtime-image.json`. The manifest, executable, hashes and expanded size are
validated before the content-addressed image becomes selectable. Imports never
execute archive content and reject traversal, hard links, device nodes and other
special files. Maximum archive size is 128 MiB and maximum expanded size is 1 GiB.

Imported images appear in **Library → Rootfs images**. Create a profile with
application `rootfs` to run the image's pinned `argv`; no host executable or
generated utility variant is added. CLI equivalents:

```sh
sasctl --wait rootfs-image import --file ./image.tar.gz --name Example --version 1
sasctl --wait rootfs-image import --path /srv/images/example.tar --name Example
sasctl rootfs-image list
```
