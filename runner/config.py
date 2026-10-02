from __future__ import annotations

import re
from pathlib import Path


class ConfigError(ValueError):
    pass


PARAMETERS = {
    "socks_port": (1, 65535),
    "http_port": (1, 65535),
    "plaintext_port": (1, 65535),
    "tls_port": (1, 65535),
    "cli_port": (1, 65535),
    "workers": (1, 128),
    "pcap_quota_mb": (1, 4096),
}
TOKEN_RE = re.compile(r"{{([A-Z][A-Z0-9_]*)}}")


def _replace_scalar_assignment(text: str, key: str, value: str) -> tuple[str, int]:
    """Replace one libconfig scalar in source or Smithproxy native-save syntax.

    Source configs commonly terminate assignments with semicolons, while
    Smithproxy's `save config` emits one assignment per line without them.
    Never let a match cross a newline: doing so can consume unrelated config
    sections while looking for a later semicolon.
    """
    pattern = re.compile(
        rf"(\b{re.escape(key)}[ \t]*=[ \t]*)"
        r'("(?:\\.|[^"\\])*"|[^;\r\n{}\[\],]+)'
        r"([ \t]*;?)"
    )
    return pattern.subn(
        lambda match: f"{match.group(1)}{value}{match.group(3)}",
        text, count=1,
    )


def validate_parameters(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ConfigError("parameters must be an object")
    unknown = set(value) - set(PARAMETERS)
    if unknown:
        raise ConfigError(f"unsupported parameters: {', '.join(sorted(unknown))}")
    result: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ConfigError(f"{key} must be an integer")
        low, high = PARAMETERS[key]
        if not low <= raw <= high:
            raise ConfigError(f"{key} must be between {low} and {high}")
        result[key] = raw
    return result


def rebase_runtime_paths(text: str, runtime_dir: Path, assets_dir: Path) -> str:
    """Replace only absolute paths which point into an old appliance runtime.

    Saved Smithproxy configs contain absolute paths. A config copied from a
    Test Drive or another instance must not keep references into that old
    appliance. Arbitrary administrator paths and stable /work paths are left
    untouched; this function is used for dirty restart/upgrade preservation.
    """
    replacements = {
        "certs_path": f'"{assets_dir}/certs/default/"',
        "messages_dir": f'"{assets_dir}/msg/en/"',
        "write_payload_dir": f'"{runtime_dir}"',
        "log_file": f'"{runtime_dir}/messages.%s.log"',
        "sslkeylog_file": f'"{runtime_dir}/sslkeylog.%s.log"',
    }
    old_runtime = re.compile(
        r"^/tmp/capture-zone-runtime/(?:instances|test-drives)/[^/]+(?:/|$)"
    )
    current_root = runtime_dir.resolve(strict=False)

    def stale(value: str) -> bool:
        if not old_runtime.match(value):
            return False
        candidate = Path(value).resolve(strict=False)
        return candidate != current_root and current_root not in candidate.parents

    for key, value in replacements.items():
        pattern = re.compile(
            rf'(\b{re.escape(key)}[ \t]*=[ \t]*)"([^"]*)"([ \t]*;?)'
        )
        text = pattern.sub(
            lambda match: (
                f"{match.group(1)}{value}{match.group(3)}"
                if stale(match.group(2)) else match.group(0)
            ),
            text, count=1,
        )
    capture_pattern = re.compile(
        r'(?ms)(\bcaptures\s*=\s*\{.*?\blocal\s*=\s*\{.*?\bdir\s*=\s*)"([^"]*)"'
    )
    return capture_pattern.sub(
        lambda match: (
            f'{match.group(1)}"{runtime_dir}"'
            if stale(match.group(2)) else match.group(0)
        ),
        text, count=1,
    )


def render_template(template_path: Path, parameters: dict[str, int], runtime_dir: Path,
                    assets_dir: Path | None = None,
                    ca_key_password: str | None = None,
                    text_parameters: dict[str, str] | None = None) -> str:
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read configured template: {exc}") from exc

    values = {key.upper(): str(value) for key, value in parameters.items()}
    values.update({key.upper(): value for key, value in (text_parameters or {}).items()})
    values["RUNTIME_DIR"] = str(runtime_dir)

    required = set(TOKEN_RE.findall(template))
    missing = required - set(values)
    if missing:
        raise ConfigError(f"missing template parameters: {', '.join(sorted(missing))}")
    rendered = TOKEN_RE.sub(lambda match: values[match.group(1)], template)

    # A complete upstream config is kept next to each built binary. Override
    # only the small allowlisted runtime surface; all required schema sections
    # remain exactly as shipped by the matching source revision.
    assets_dir = assets_dir or template_path.parent / f"{template_path.stem}.assets"
    replacements = {
        "certs_path": f'"{assets_dir}/certs/default/"',
        "messages_dir": f'"{assets_dir}/msg/en/"',
        "plaintext_port": f'"{parameters.get("plaintext_port", 50080)}"',
        "ssl_port": f'"{parameters.get("tls_port", 50443)}"',
        "socks_port": f'"{parameters.get("socks_port", 1080)}"',
        "http_connect_port": f'"{parameters.get("http_port", 3128)}"',
        "plaintext_workers": str(parameters.get("workers", 1)),
        "ssl_workers": str(parameters.get("workers", 1)),
        "udp_workers": "-1",
        "dtls_workers": "-1", "quic_workers": "-1",
        "write_payload_dir": f'"{runtime_dir}"',
        "write_pcap_single_quota": str(parameters.get("pcap_quota_mb", 100)),
        "log_file": f'"{runtime_dir}/messages.%s.log"',
        "sslkeylog_file": f'"{runtime_dir}/sslkeylog.%s.log"',
    }
    for key, value in replacements.items():
        rendered, _ = _replace_scalar_assignment(rendered, key, value)

    if ca_key_password is not None:
        # Native saves may omit settings that equal Smithproxy defaults.  A CA
        # overlay needs both values explicitly, so add them to the top-level
        # settings block when they are absent instead of rejecting the bundle.
        def set_or_insert_setting(text: str, key: str, value: str) -> str:
            updated, count = _replace_scalar_assignment(text, key, value)
            if count:
                return updated
            updated, count = re.subn(
                r"(\bsettings\s*=\s*\{)",
                lambda match: f"{match.group(1)}\n    {key} = {value};",
                text, count=1,
            )
            if count != 1:
                raise ConfigError("selected configuration has no settings block")
            return updated

        escaped_password = ca_key_password.replace("\\", "\\\\").replace('"', '\\"')
        rendered = set_or_insert_setting(
            rendered, "certs_path", replacements["certs_path"],
        )
        rendered = set_or_insert_setting(
            rendered, "certs_ca_key_password", f'"{escaped_password}"',
        )

    # dtls_workers is supported by Smithproxy but is absent from the shipped
    # default config.  Missing means 0, which expands to one listener per CPU.
    # Insert this known setting explicitly into the top-level settings block.
    if not re.search(r"\bdtls_workers\s*=", rendered):
        rendered = re.sub(
            r"(\bsettings\s*=\s*\{)",
            rf"\1\n    dtls_workers = {replacements['dtls_workers']};",
            rendered,
            count=1,
        )

    cli_port = parameters.get("cli_port", 50000)
    rendered = re.sub(
        r"(?ms)(\bcli\s*=\s*\{.*?\bport\s*=\s*)\d+(\s*;)",
        rf"\g<1>{cli_port}\2", rendered, count=1,
    )
    return rebase_runtime_paths(rendered, runtime_dir, assets_dir)
