from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import render_template
from .systemd import BackendError


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ApplianceExportLibrary:
    """Filesystem-backed, portable appliance bundles; deliberately no database."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _load(self, path: Path) -> dict[str, Any] | None:
        try:
            item = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
            export_id = str(item.get("export_id", ""))
            archive = path / str(item.get("archive_name", ""))
            if path.name != export_id or str(uuid.UUID(export_id)) != export_id:
                return None
            if not archive.is_file():
                return None
            item["size_bytes"] = archive.stat().st_size
            return item
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            try:
                values = [item for path in self.root.iterdir()
                          if path.is_dir() and (item := self._load(path))]
            except OSError:
                return []
        return sorted(values, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def get(self, export_id: str) -> dict[str, Any]:
        try:
            if str(uuid.UUID(export_id)) != export_id:
                raise ValueError
        except ValueError as exc:
            raise BackendError("invalid appliance export id") from exc
        item = self._load(self.root / export_id)
        if not item:
            raise BackendError("appliance export is unavailable")
        return item

    def archive(self, export_id: str) -> tuple[dict[str, Any], Path]:
        item = self.get(export_id)
        path = self.root / export_id / str(item["archive_name"])
        return item, path

    def delete(self, export_id: str) -> dict[str, Any]:
        with self.lock:
            item = self.get(export_id)
            shutil.rmtree(self.root / export_id)
            return item

    @staticmethod
    def _scripts(name: str, short_id: str, filesystem_mode: str, profile: str,
                 slot: int) -> dict[str, str]:
        namespace = f"sae-{short_id}"
        unit = f"sas-export-{short_id}.service"
        ingress_host = f"sxi{short_id[:8]}"
        egress_host = f"sxo{short_id[:8]}"
        in_host = f"10.251.{slot}.1/30"
        in_ns = f"10.251.{slot}.2/30"
        out_host = f"10.252.{slot}.1/30"
        out_ns = f"10.252.{slot}.2/30"
        transparent = profile not in {"socks", "http-proxy", "http_connect"}
        nft = ""
        if transparent:
            nft = r'''
ip netns exec "$NS" sysctl -qw net.ipv4.ip_forward=1
ip netns exec "$NS" ip rule add fwmark 1 lookup 100 2>/dev/null || true
ip netns exec "$NS" ip route replace local 0.0.0.0/0 dev lo table 100
ip netns exec "$NS" nft -f - <<'NFT'
table inet appliance_export {
  chain prerouting {
    type filter hook prerouting priority mangle; policy accept;
    iifname "di0" tcp dport 443 tproxy to :50443 meta mark set 1
    iifname "di0" tcp dport != 443 tproxy to :50080 meta mark set 1
  }
}
NFT
'''
        command = (
            'systemd-run --unit="$UNIT" --slice=sas-appliance-exports.slice '
            '--property="NetworkNamespacePath=/run/netns/$NS" '
            '--working-directory="$BASE/work" "$BASE/bin/run-smithproxy" '
            '--config-file "$BASE/runtime/smithproxy.cfg"'
            if filesystem_mode == "plain" else
            'mkdir -p "$BASE/rootfs/opt/appliance/assets" "$BASE/rootfs/work" "$BASE/rootfs/run"\n'
            'touch "$BASE/rootfs/opt/appliance/smithproxy.cfg"\n'
            'systemd-run --unit="$UNIT" --slice=sas-appliance-exports.slice '
            '--property="NetworkNamespacePath=/run/netns/$NS" '
            '--property="RootDirectory=$BASE/rootfs" '
            '--property="BindReadOnlyPaths=$BASE/runtime/smithproxy.cfg:/opt/appliance/smithproxy.cfg" '
            '--property="BindReadOnlyPaths=$BASE/assets:/opt/appliance/assets" '
            '--property="BindPaths=$BASE/work:/work" --property="BindPaths=$BASE/run:/run" '
            '--working-directory=/work /usr/bin/smithproxy '
            '--config-file /opt/appliance/smithproxy.cfg'
        )
        start = f'''#!/bin/sh
set -eu
[ "$(id -u)" -eq 0 ] || {{ echo "start.sh requires root" >&2; exit 1; }}
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
NS={namespace}
UNIT={unit}
IN_HOST_IF={ingress_host}
OUT_HOST_IF={egress_host}
IN_HOST_CIDR=${{SAS_IN_HOST_CIDR:-{in_host}}}
IN_NS_CIDR=${{SAS_IN_NS_CIDR:-{in_ns}}}
OUT_HOST_CIDR=${{SAS_OUT_HOST_CIDR:-{out_host}}}
OUT_NS_CIDR=${{SAS_OUT_NS_CIDR:-{out_ns}}}
mkdir -p "$BASE/work" "$BASE/run" "$BASE/runtime"
sed "s|__APPLIANCE_ROOT__|$BASE|g" "$BASE/config/smithproxy.cfg" > "$BASE/runtime/smithproxy.cfg"
ip netns add "$NS"
cleanup() {{ ip netns del "$NS" 2>/dev/null || true; }}
trap cleanup EXIT INT TERM
ip link add "$IN_HOST_IF" type veth peer name di0 netns "$NS"
ip link add "$OUT_HOST_IF" type veth peer name do0 netns "$NS"
ip address add "$IN_HOST_CIDR" dev "$IN_HOST_IF"
ip address add "$OUT_HOST_CIDR" dev "$OUT_HOST_IF"
ip link set "$IN_HOST_IF" up
ip link set "$OUT_HOST_IF" up
ip -n "$NS" address add "$IN_NS_CIDR" dev di0
ip -n "$NS" address add "$OUT_NS_CIDR" dev do0
ip -n "$NS" link set lo up
ip -n "$NS" link set di0 up
ip -n "$NS" link set do0 up
ip -n "$NS" route replace default via "${{OUT_HOST_CIDR%/*}}" dev do0
{nft.strip()}
{command}
trap - EXIT INT TERM
echo "{name} started: namespace=$NS unit=$UNIT"
echo "ingress host=$IN_HOST_IF $IN_HOST_CIDR namespace=di0 $IN_NS_CIDR"
echo "egress  host=$OUT_HOST_IF $OUT_HOST_CIDR namespace=do0 $OUT_NS_CIDR"
'''
        stop = f'''#!/bin/sh
set -eu
systemctl stop {unit} 2>/dev/null || true
systemctl reset-failed {unit} 2>/dev/null || true
ip netns del {namespace} 2>/dev/null || true
'''
        status = f'''#!/bin/sh
set -eu
systemctl --no-pager --full status {unit} || true
ip -br -n {namespace} address 2>/dev/null || true
'''
        route = f'''#!/bin/sh
set -eu
[ "$(id -u)" -eq 0 ] || {{ echo "route.sh requires root" >&2; exit 1; }}
case "${{1:-}}" in
  ingress) [ $# -eq 2 ] || exit 2; ip -n {namespace} route replace "$2" via {in_host.split('/')[0]} dev di0 ;;
  egress)  [ $# -eq 2 ] || exit 2; ip -n {namespace} route replace "$2" via {out_host.split('/')[0]} dev do0 ;;
  *) echo "usage: $0 ingress|egress <CIDR|default>" >&2; exit 2 ;;
esac
'''
        binary_wrapper = '''#!/bin/sh
set -eu
BASE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
LIBPATH=
if [ -d "$BASE/deps" ]; then
  for directory in $(find "$BASE/deps" -type d); do
    LIBPATH=${LIBPATH:+$LIBPATH:}$directory
  done
fi
LD_LIBRARY_PATH=${LIBPATH}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export LD_LIBRARY_PATH
exec "$BASE/bin/smithproxy" "$@"
'''
        repack = f'''#!/bin/sh
set -eu
[ $# -ge 1 ] && [ $# -le 2 ] || {{ echo "usage: $0 /path/to/smithproxy [new-name]" >&2; exit 2; }}
SOURCE=$1
[ -f "$SOURCE" ] && [ -x "$SOURCE" ] || {{ echo "binary is not executable: $SOURCE" >&2; exit 2; }}
NAME=${{2:-{name}-rebuilt}}
case "$NAME" in *[!A-Za-z0-9._-]*|'') echo "invalid appliance name" >&2; exit 2;; esac
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT=$(pwd)/$NAME
[ ! -e "$OUT" ] && [ ! -e "$OUT.tar.gz" ] || {{ echo "output already exists: $OUT" >&2; exit 2; }}
TMP=$(mktemp -d "${{TMPDIR:-/tmp}}/sas-appliance-repack.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM
mkdir "$TMP/$NAME"
(cd "$HERE" && tar --exclude='./work/*' --exclude='./run/*' --exclude='./runtime/*' \
  --exclude='./repack-output' -cf - .) | (cd "$TMP/$NAME" && tar -xf -)
mkdir -p "$TMP/$NAME/work" "$TMP/$NAME/run" "$TMP/$NAME/runtime"
python3 - "$SOURCE" "$TMP/$NAME" "{filesystem_mode}" <<'PYDEPS'
import pathlib, re, shutil, subprocess, sys
source, target, mode = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2]), sys.argv[3]
pending, copied, missing = [source], {{}}, set()
while pending:
    current = pending.pop()
    result = subprocess.run(["ldd", str(current)], text=True, capture_output=True)
    output = result.stdout + result.stderr
    for line in output.splitlines():
        if "=> not found" in line:
            missing.add(line.split("=>", 1)[0].strip()); continue
        match = re.search(r"=>\\s+(/\\S+)", line) or re.match(r"\\s*(/\\S+)", line)
        if not match: continue
        reported = pathlib.Path(match.group(1))
        dependency = reported.resolve()
        if reported in copied or not dependency.is_file(): continue
        copied[reported] = dependency; pending.append(dependency)
if missing:
    raise SystemExit("missing shared libraries: " + ", ".join(sorted(missing)))
for reported, dependency in copied.items():
    root = target / ("rootfs" if mode == "rootfs" else "deps")
    destination = root / str(reported).lstrip("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dependency, destination, follow_symlinks=True)
print(f"imported {{len(copied)}} ELF loader/library files")
PYDEPS
if [ "{filesystem_mode}" = rootfs ]; then
  install -m 0755 "$SOURCE" "$TMP/$NAME/rootfs/usr/bin/smithproxy"
else
  install -m 0755 "$SOURCE" "$TMP/$NAME/bin/smithproxy"
fi
python3 - "$TMP/$NAME/manifest.json" "$NAME" <<'PY'
import hashlib, json, pathlib, sys
p = pathlib.Path(sys.argv[1]); root = p.parent; d = json.loads(p.read_text()); name = sys.argv[2]
old_id, new_id = "{short_id}", hashlib.sha256(name.encode()).hexdigest()[:10]
old_slot, new_slot = {slot}, int(hashlib.sha256(name.encode()).hexdigest()[:2], 16) % 253 + 1
for filename in ("start.sh", "stop.sh", "status.sh", "route.sh"):
    script = root / filename
    text = script.read_text().replace(old_id, new_id).replace("{name}", name)
    text = text.replace(f"10.251.{{old_slot}}.", f"10.251.{{new_slot}}.")
    text = text.replace(f"10.252.{{old_slot}}.", f"10.252.{{new_slot}}.")
    script.write_text(text)
d["source_export_id"] = d.get("export_id", ""); d["export_id"] = ""
d["name"] = name; d["repacked_from_binary"] = True; d["repack_identity"] = new_id
d["network"]["ingress_host"] = f"10.251.{{new_slot}}.1/30"
d["network"]["ingress_namespace"] = f"10.251.{{new_slot}}.2/30"
d["network"]["egress_host"] = f"10.252.{{new_slot}}.1/30"
d["network"]["egress_namespace"] = f"10.252.{{new_slot}}.2/30"
p.write_text(json.dumps(d, indent=2) + "\\n")
PY
mv "$TMP/$NAME" "$OUT"
tar -C "$(dirname "$OUT")" -czf "$OUT.tar.gz" "$(basename "$OUT")"
echo "$OUT.tar.gz"
'''
        return {
            "start.sh": start, "stop.sh": stop, "status.sh": status,
            "route.sh": route, "repack-from-binary.sh": repack,
            "bin/run-smithproxy": binary_wrapper,
        }

    def create(self, *, name: str, build: dict[str, Any], binary: Path,
               config: Path, assets: Path, filesystem_mode: str,
               rootfs: Path | None, profile: str,
               config_id: str = "", config_name: str = "",
               parameters: dict[str, str] | None = None) -> dict[str, Any]:
        clean_name = name.strip()
        if not NAME_RE.fullmatch(clean_name):
            raise BackendError("name must contain 1-64 letters, digits, dots, underscores or dashes")
        if filesystem_mode not in {"plain", "rootfs"}:
            raise BackendError("filesystem_mode must be plain or rootfs")
        if not binary.is_file() or not config.is_file() or not assets.is_dir():
            raise BackendError("selected appliance inputs are incomplete")
        if filesystem_mode == "rootfs" and (rootfs is None or not rootfs.is_dir()):
            raise BackendError("selected build rootfs is unavailable")
        export_id = str(uuid.uuid4())
        short_id = export_id.replace("-", "")[:10]
        slot = int(hashlib.sha256(export_id.encode()).hexdigest()[:2], 16) % 253 + 1
        target = self.root / export_id
        temporary = Path(tempfile.mkdtemp(prefix=f".{export_id}-", dir=self.root))
        staging = temporary / clean_name
        try:
            for relative in ("bin", "config", "assets", "work", "run", "runtime"):
                (staging / relative).mkdir(parents=True, exist_ok=True)
            if filesystem_mode == "plain":
                shutil.copy2(binary, staging / "bin/smithproxy")
            else:
                shutil.copytree(rootfs, staging / "rootfs", symlinks=True)
            shutil.copytree(assets, staging / "assets", dirs_exist_ok=True, symlinks=True)
            numeric = {
                "socks_port": 1080, "http_port": 3128, "plaintext_port": 50080,
                "tls_port": 50443, "cli_port": 50000, "workers": 1,
                "pcap_quota_mb": 100,
            }
            text_values = {str(key).upper(): str(value) for key, value in (parameters or {}).items()}
            config_root = Path("/opt/appliance") if filesystem_mode == "rootfs" else Path("__APPLIANCE_ROOT__")
            runtime_path = Path("/work") if filesystem_mode == "rootfs" else config_root / "work"
            rendered = render_template(
                config, numeric, runtime_path, config_root / "assets",
                text_parameters=text_values,
            )
            (staging / "config/smithproxy.cfg").write_text(rendered, encoding="utf-8")
            os.chmod(staging / "config/smithproxy.cfg", 0o600)
            for filename, content in self._scripts(
                clean_name, short_id, filesystem_mode, profile, slot,
            ).items():
                path = staging / filename
                path.write_text(content, encoding="utf-8")
                os.chmod(path, 0o750)
            manifest = {
                "schema": 1, "name": clean_name, "export_id": export_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "filesystem_mode": filesystem_mode, "profile": profile,
                "config_id": config_id, "config_name": config_name,
                "build_id": str(build.get("build_id", "")),
                "commit_id": str(build.get("commit_id", "")),
                "ref": str(build.get("ref", "")),
                "build_type": str(build.get("build_type", "")),
                "network": {
                    "model": "two addressed veth pairs; namespace-local policy only",
                    "ingress_host": f"10.251.{slot}.1/30", "ingress_namespace": f"10.251.{slot}.2/30",
                    "egress_host": f"10.252.{slot}.1/30", "egress_namespace": f"10.252.{slot}.2/30",
                    "host_policy": "none",
                },
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            archive_name = f"{clean_name}.tar.gz"
            archive = temporary / archive_name
            with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
                tar.add(staging, arcname=clean_name, recursive=True)
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            metadata = {**manifest, "archive_name": archive_name,
                        "sha256": checksum, "size_bytes": archive.stat().st_size}
            (temporary / "metadata.json").write_text(
                json.dumps(metadata, separators=(",", ":")), encoding="utf-8"
            )
            shutil.rmtree(staging)
            temporary.replace(target)
            return metadata
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
