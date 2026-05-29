from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


TRUNK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_CODECS = ("ulaw", "alaw", "slin")


@dataclass(frozen=True)
class SipTrunk:
    trunk_id: str
    host: str
    user: str
    password: str
    auth_user: str = ""
    contact_user: str = ""
    from_user: str = ""
    display_name: str = ""
    enabled: bool = True
    codecs: tuple[str, ...] = DEFAULT_CODECS
    expiration: int = 300
    retry_interval: int = 30
    forbidden_retry_interval: int = 300

    @property
    def registration_name(self) -> str:
        return f"trunk-{self.trunk_id}-reg"

    @property
    def endpoint_name(self) -> str:
        return f"trunk-{self.trunk_id}-endpoint"

    @property
    def auth_name(self) -> str:
        return f"trunk-{self.trunk_id}-auth"

    @property
    def aor_name(self) -> str:
        return f"trunk-{self.trunk_id}-aor"

    @property
    def identify_name(self) -> str:
        return f"trunk-{self.trunk_id}-identify"

    def redacted_dict(self) -> dict[str, Any]:
        return {
            "trunk_id": self.trunk_id,
            "host": self.host,
            "user": self.user,
            "auth_user": self.auth_user or self.user,
            "contact_user": self.contact_user or self.user,
            "from_user": self.from_user or self.user,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "codecs": list(self.codecs),
            "expiration": self.expiration,
            "retry_interval": self.retry_interval,
            "forbidden_retry_interval": self.forbidden_retry_interval,
            "registration": self.registration_name,
            "endpoint": self.endpoint_name,
            "password": {"configured": bool(self.password), "redacted": True},
        }

    def stored_dict(self) -> dict[str, Any]:
        return {
            "trunk_id": self.trunk_id,
            "host": self.host,
            "user": self.user,
            "password": self.password,
            "auth_user": self.auth_user,
            "contact_user": self.contact_user,
            "from_user": self.from_user,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "codecs": list(self.codecs),
            "expiration": self.expiration,
            "retry_interval": self.retry_interval,
            "forbidden_retry_interval": self.forbidden_retry_interval,
        }


class SipTrunkStore:
    def __init__(self, registry_path: str, pjsip_include_path: str) -> None:
        self.registry_path = Path(registry_path)
        self.pjsip_include_path = Path(pjsip_include_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.pjsip_include_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.registry_path.exists():
            self._write_registry({})
        if not self.pjsip_include_path.exists():
            self.render()

    def list(self) -> list[SipTrunk]:
        trunks = [trunk_from_dict(item) for item in self._read_registry().values()]
        return sorted(trunks, key=lambda trunk: trunk.trunk_id)

    def get(self, trunk_id: str) -> SipTrunk | None:
        validate_trunk_id(trunk_id)
        data = self._read_registry().get(trunk_id)
        if data is None:
            return None
        return trunk_from_dict(data)

    def upsert(self, trunk: SipTrunk) -> SipTrunk:
        validate_trunk(trunk)
        data = self._read_registry()
        data[trunk.trunk_id] = trunk.stored_dict()
        self._write_registry(data)
        self.render()
        return trunk

    def set_enabled(self, trunk_id: str, enabled: bool) -> SipTrunk | None:
        trunk = self.get(trunk_id)
        if trunk is None:
            return None
        updated = SipTrunk(**{**trunk.stored_dict(), "enabled": enabled})
        return self.upsert(updated)

    def delete(self, trunk_id: str) -> SipTrunk | None:
        validate_trunk_id(trunk_id)
        data = self._read_registry()
        removed = data.pop(trunk_id, None)
        if removed is None:
            return None
        self._write_registry(data)
        self.render()
        return trunk_from_dict(removed)

    def render(self) -> None:
        trunks = [trunk for trunk in self.list() if trunk.enabled]
        content = render_pjsip_trunks(trunks)
        self.pjsip_include_path.write_text(content, encoding="utf-8")

    def _read_registry(self) -> dict[str, dict[str, Any]]:
        try:
            parsed = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(parsed, dict):
            raise ValueError(f"{self.registry_path} must contain a JSON object")
        return {str(key): dict(value) for key, value in parsed.items()}

    def _write_registry(self, data: dict[str, dict[str, Any]]) -> None:
        self.registry_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def trunk_from_dict(data: dict[str, Any]) -> SipTrunk:
    return SipTrunk(
        trunk_id=str(data["trunk_id"]),
        host=str(data["host"]),
        user=str(data["user"]),
        password=str(data.get("password") or ""),
        auth_user=str(data.get("auth_user") or ""),
        contact_user=str(data.get("contact_user") or ""),
        from_user=str(data.get("from_user") or ""),
        display_name=str(data.get("display_name") or ""),
        enabled=bool(data.get("enabled", True)),
        codecs=tuple(str(codec) for codec in data.get("codecs", DEFAULT_CODECS)),
        expiration=int(data.get("expiration", 300)),
        retry_interval=int(data.get("retry_interval", 30)),
        forbidden_retry_interval=int(data.get("forbidden_retry_interval", 300)),
    )


def render_pjsip_trunks(trunks: list[SipTrunk]) -> str:
    lines = [
        "; Generated by FlowHunt Voicebot. Do not edit manually.",
        "",
    ]
    for trunk in trunks:
        validate_trunk(trunk)
        allow = ",".join(trunk.codecs)
        auth_user = trunk.auth_user or trunk.user
        contact_user = trunk.contact_user or trunk.user
        from_user = trunk.from_user or trunk.user
        lines.extend(
            [
                f"[{trunk.registration_name}]",
                "type=registration",
                "transport=transport-udp",
                f"outbound_auth={trunk.auth_name}",
                f"server_uri=sip:{trunk.host}",
                f"client_uri=sip:{trunk.user}@{trunk.host}",
                f"contact_user={contact_user}",
                f"endpoint={trunk.endpoint_name}",
                "line=yes",
                f"retry_interval={trunk.retry_interval}",
                f"forbidden_retry_interval={trunk.forbidden_retry_interval}",
                f"expiration={trunk.expiration}",
                "",
                f"[{trunk.auth_name}]",
                "type=auth",
                "auth_type=userpass",
                f"username={auth_user}",
                f"password={trunk.password}",
                "",
                f"[{trunk.aor_name}]",
                "type=aor",
                f"contact=sip:{trunk.host}",
                "",
                f"[{trunk.endpoint_name}]",
                "type=endpoint",
                "transport=transport-udp",
                "context=from-liveagent",
                "disallow=all",
                f"allow={allow}",
                f"outbound_auth={trunk.auth_name}",
                f"aors={trunk.aor_name}",
                f"from_user={from_user}",
                f"from_domain={trunk.host}",
                "direct_media=no",
                "rtp_symmetric=yes",
                "force_rport=yes",
                "rewrite_contact=yes",
                "timers=no",
                "",
                f"[{trunk.identify_name}]",
                "type=identify",
                f"endpoint={trunk.endpoint_name}",
                f"match={trunk.host}",
                "",
            ]
        )
    return "\n".join(lines)


def validate_trunk(trunk: SipTrunk) -> None:
    validate_trunk_id(trunk.trunk_id)
    for label, value in {
        "host": trunk.host,
        "user": trunk.user,
        "auth_user": trunk.auth_user,
        "contact_user": trunk.contact_user,
        "from_user": trunk.from_user,
        "password": trunk.password,
        "display_name": trunk.display_name,
    }.items():
        validate_clean_value(label, value)
    if not trunk.host:
        raise ValueError("host is required")
    if not trunk.user:
        raise ValueError("user is required")
    if trunk.enabled and not trunk.password:
        raise ValueError("password is required for enabled trunks")
    if not trunk.codecs:
        raise ValueError("at least one codec is required")
    for codec in trunk.codecs:
        validate_clean_value("codec", codec)
    if trunk.expiration <= 0:
        raise ValueError("expiration must be greater than 0")
    if trunk.retry_interval <= 0:
        raise ValueError("retry_interval must be greater than 0")
    if trunk.forbidden_retry_interval <= 0:
        raise ValueError("forbidden_retry_interval must be greater than 0")


def validate_trunk_id(trunk_id: str) -> None:
    if not TRUNK_ID_RE.fullmatch(trunk_id):
        raise ValueError("trunk_id must contain only letters, numbers, underscores, or dashes")


def validate_clean_value(label: str, value: str) -> None:
    if any(character in value for character in "\r\n[]"):
        raise ValueError(f"{label} must not contain control characters or brackets")
