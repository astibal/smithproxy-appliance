from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError
from .systemd import BackendError


TABLE_FAMILY = "inet"
TABLE_NAME = "capture_zone_access"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FirewallManager:
    """JSON-backed source allow-list rendered into one SAS-owned nft table."""

    def __init__(self, path: Path, executor: Callable[[str], None] | None = None) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.executor = executor or self._nft_apply
        self.last_applied_at = ""
        self.last_fingerprint = ""
        self.namespace_cidr_v6 = "fd42:ca7:200::/64"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "schema": 1,
            "input_enforced": False,
            "forward_enforced": False,
            "authorizations": [],
        }

    @staticmethod
    def _source(value: object) -> str:
        try:
            network = ipaddress.ip_network(str(value).strip(), strict=False)
        except ValueError as exc:
            raise ConfigError(f"source must be an IPv4/IPv6 address or CIDR: {exc}") from exc
        return str(network)

    @staticmethod
    def _chains(value: object) -> list[str]:
        if not isinstance(value, list):
            raise ConfigError("chains must be an array")
        chains = sorted({str(item).lower() for item in value})
        if not chains or any(item not in {"input", "forward"} for item in chains):
            raise ConfigError("chains must contain input and/or forward")
        return chains

    @staticmethod
    def _expiry(payload: dict[str, Any]) -> str:
        raw_ttl = payload.get("ttl_seconds")
        raw_expiry = str(payload.get("expires_at", "")).strip()
        if raw_ttl not in (None, ""):
            if isinstance(raw_ttl, bool):
                raise ConfigError("ttl_seconds must be an integer")
            ttl = int(raw_ttl)
            if not 60 <= ttl <= 31 * 86400:
                raise ConfigError("ttl_seconds must be between 60 and 2678400")
            return (_now() + timedelta(seconds=ttl)).isoformat()
        if not raw_expiry:
            return ""
        try:
            parsed = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigError("expires_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed <= _now():
            raise ConfigError("expires_at must be in the future")
        return parsed.astimezone(timezone.utc).isoformat()

    def load(self) -> dict[str, Any]:
        with self.lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return self._defaults()
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise BackendError(f"cannot read firewall settings: {exc}") from exc
            if not isinstance(value, dict) or not isinstance(value.get("authorizations"), list):
                raise BackendError("firewall JSON is invalid")
            value.setdefault("schema", 1)
            value.setdefault("input_enforced", False)
            value.setdefault("forward_enforced", False)
            return value

    def _save(self, value: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    @staticmethod
    def _active(item: dict[str, Any], now: datetime | None = None) -> bool:
        expires_at = str(item.get("expires_at", ""))
        if not expires_at:
            return True
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry > (now or _now())
        except ValueError:
            return False

    @staticmethod
    def _elements(values: list[str]) -> str:
        networks = list(ipaddress.collapse_addresses(
            ipaddress.ip_network(value, strict=False) for value in values
        ))
        return ", ".join(str(item) for item in networks)

    def render(self, document: dict[str, Any], namespace_cidr: str,
               inherited_forward_sources: list[str]) -> str:
        active = [item for item in document["authorizations"] if self._active(item)]
        input_sources = [item["source"] for item in active if "input" in item["chains"]]
        forward_sources = inherited_forward_sources + [
            item["source"] for item in active if "forward" in item["chains"]
        ]
        def family(values: list[str], version: int) -> list[str]:
            return [value for value in values if ipaddress.ip_network(value, strict=False).version == version]
        input_v4, input_v6 = family(input_sources, 4), family(input_sources, 6)
        forward_v4, forward_v6 = family(forward_sources, 4), family(forward_sources, 6)
        def elements(values: list[str]) -> str:
            rendered = self._elements(values)
            return f"    elements = {{ {rendered} }}\n" if rendered else ""
        input_rules = ""
        if document.get("input_enforced"):
            input_rules = (
                "    ct state established,related accept\n"
                "    iifname \"lo\" accept\n"
                "    ip saddr @input_sources_v4 accept\n"
                "    ip6 saddr @input_sources_v6 accept\n"
                "    counter drop comment \"SAS INPUT deny\"\n"
            )
        forward_rules = ""
        if document.get("forward_enforced"):
            forward_rules = (
                "    ct state established,related accept\n"
                f"    ip saddr {ipaddress.ip_network(namespace_cidr, strict=True)} accept "
                "comment \"instance egress\"\n"
                f"    ip6 saddr {ipaddress.ip_network(self.namespace_cidr_v6, strict=True)} accept "
                "comment \"instance IPv6 egress\"\n"
                "    oifname \"czi*\" ip saddr @forward_sources_v4 accept\n"
                "    oifname \"czi*\" ip6 saddr @forward_sources_v6 accept\n"
                "    oifname \"czi*\" counter drop comment \"SAS instance ingress deny\"\n"
            )
        return (
            f"table {TABLE_FAMILY} {TABLE_NAME} {{\n"
            "  set input_sources_v4 {\n"
            "    type ipv4_addr\n    flags interval\n"
            f"{elements(input_v4)}  }}\n"
            "  set input_sources_v6 {\n"
            "    type ipv6_addr\n    flags interval\n"
            f"{elements(input_v6)}  }}\n"
            "  set forward_sources_v4 {\n"
            "    type ipv4_addr\n    flags interval\n"
            f"{elements(forward_v4)}  }}\n"
            "  set forward_sources_v6 {\n"
            "    type ipv6_addr\n    flags interval\n"
            f"{elements(forward_v6)}  }}\n"
            "  chain input {\n"
            "    type filter hook input priority filter; policy accept;\n"
            f"{input_rules}  }}\n"
            "  chain forward {\n"
            "    type filter hook forward priority filter; policy accept;\n"
            f"{forward_rules}  }}\n"
            "}\n"
        )

    @staticmethod
    def _nft_apply(ruleset: str) -> None:
        present = subprocess.run(
            ["nft", "list", "table", TABLE_FAMILY, TABLE_NAME],
            capture_output=True, text=True, timeout=10, check=False,
        ).returncode == 0
        script = (
            f"delete table {TABLE_FAMILY} {TABLE_NAME}\n" if present else ""
        ) + ruleset
        completed = subprocess.run(
            ["nft", "-f", "/dev/stdin"], input=script,
            capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode:
            raise BackendError(completed.stderr.strip() or "nft firewall apply failed")

    def apply(self, namespace_cidr: str, inherited_forward_sources: list[str],
              document: dict[str, Any] | None = None) -> str:
        with self.lock:
            current = document or self.load()
            ruleset = self.render(current, namespace_cidr, inherited_forward_sources)
            self.executor(ruleset)
            self.last_fingerprint = ruleset
            self.last_applied_at = _now().isoformat()
            return ruleset

    def view(self, namespace_cidr: str, inherited_forward_sources: list[str]) -> dict[str, Any]:
        with self.lock:
            document = self.load()
            now = _now()
            items = []
            for source in document["authorizations"]:
                item = dict(source)
                item["active"] = self._active(item, now)
                items.append(item)
            return {
                **document, "authorizations": items,
                "inherited_forward_sources": inherited_forward_sources,
                "namespace_cidr": namespace_cidr,
                "namespace_cidr_v6": self.namespace_cidr_v6,
                "table": f"{TABLE_FAMILY} {TABLE_NAME}",
                "last_applied_at": self.last_applied_at,
                "ruleset": self.render(document, namespace_cidr, inherited_forward_sources),
                "ipv6_enforced": bool(document.get("input_enforced") or document.get("forward_enforced")),
            }

    def update_settings(self, input_enforced: bool, forward_enforced: bool,
                        namespace_cidr: str, inherited_sources: list[str]) -> dict[str, Any]:
        if not isinstance(input_enforced, bool) or not isinstance(forward_enforced, bool):
            raise ConfigError("firewall enforcement flags must be boolean")
        with self.lock:
            document = self.load()
            candidate = {**document, "input_enforced": input_enforced,
                         "forward_enforced": forward_enforced}
            self.apply(namespace_cidr, inherited_sources, candidate)
            self._save(candidate)
            return self.view(namespace_cidr, inherited_sources)

    def add(self, payload: dict[str, Any], namespace_cidr: str,
            inherited_sources: list[str], registered_source: bool = False) -> dict[str, Any]:
        source = self._source(payload.get("source", ""))
        chains = self._chains(payload.get("chains", ["forward"]))
        label = str(payload.get("label", "")).strip()[:128]
        system = str(payload.get("system", "capture-zone")).strip()[:64]
        if not system or any(ord(character) < 32 for character in system + label):
            raise ConfigError("system or label is invalid")
        item = {
            "authorization_id": str(uuid.uuid4()), "source": source,
            "chains": chains, "label": label, "system": system,
            "expires_at": self._expiry(payload), "registered_source": registered_source,
            "created_at": _now().isoformat(),
        }
        with self.lock:
            document = self.load()
            duplicate = next((current for current in reversed(document["authorizations"])
                              if current.get("source") == source
                              and current.get("chains") == chains
                              and current.get("system") == system), None)
            if duplicate:
                item.update({
                    "authorization_id": duplicate["authorization_id"],
                    "created_at": duplicate.get("created_at", item["created_at"]),
                    "updated_at": _now().isoformat(),
                    "registered_source": bool(
                        duplicate.get("registered_source") or registered_source
                    ),
                })
                candidate = {**document, "authorizations": [
                    item if current.get("authorization_id") == duplicate["authorization_id"]
                    else current for current in document["authorizations"]
                ]}
                self.apply(namespace_cidr, inherited_sources, candidate)
                self._save(candidate)
                return item
            candidate = {**document, "authorizations": [*document["authorizations"], item]}
            self.apply(namespace_cidr, inherited_sources, candidate)
            self._save(candidate)
            return item

    def get(self, authorization_id: str) -> dict[str, Any] | None:
        try:
            if str(uuid.UUID(authorization_id)) != authorization_id:
                return None
        except ValueError:
            return None
        return next((item for item in self.load()["authorizations"]
                     if item.get("authorization_id") == authorization_id), None)

    def extend(self, authorization_id: str, additional_seconds: object,
               namespace_cidr: str, inherited_sources: list[str]) -> dict[str, Any]:
        if isinstance(additional_seconds, bool):
            raise ConfigError("additional_seconds must be an integer")
        try:
            additional = int(additional_seconds)
        except (TypeError, ValueError) as exc:
            raise ConfigError("additional_seconds must be an integer") from exc
        if not 60 <= additional <= 31 * 86400:
            raise ConfigError("additional_seconds must be between 60 and 2678400")
        with self.lock:
            document = self.load()
            item = next((current for current in document["authorizations"]
                         if current.get("authorization_id") == authorization_id), None)
            if not item:
                raise ConfigError("authorization not found")
            raw_expiry = str(item.get("expires_at", ""))
            if not raw_expiry:
                raise ConfigError("permanent authorization cannot be extended")
            try:
                expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ConfigError("authorization has an invalid expiry") from exc
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            base = max(expiry.astimezone(timezone.utc), _now())
            updated = {
                **item,
                "expires_at": (base + timedelta(seconds=additional)).isoformat(),
                "updated_at": _now().isoformat(),
            }
            candidate = {**document, "authorizations": [
                updated if current.get("authorization_id") == authorization_id else current
                for current in document["authorizations"]
            ]}
            self.apply(namespace_cidr, inherited_sources, candidate)
            self._save(candidate)
            return updated

    def delete(self, authorization_id: str, namespace_cidr: str,
               inherited_sources: list[str]) -> dict[str, Any] | None:
        with self.lock:
            document = self.load()
            item = next((item for item in document["authorizations"]
                         if item.get("authorization_id") == authorization_id), None)
            if not item:
                return None
            candidate = {**document, "authorizations": [
                current for current in document["authorizations"]
                if current.get("authorization_id") != authorization_id
            ]}
            self.apply(namespace_cidr, inherited_sources, candidate)
            self._save(candidate)
            return item

    def reconcile(self, namespace_cidr: str, inherited_sources: list[str]) -> None:
        with self.lock:
            document = self.load()
            ruleset = self.render(document, namespace_cidr, inherited_sources)
            if ruleset != self.last_fingerprint:
                self.apply(namespace_cidr, inherited_sources, document)
