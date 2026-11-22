"""Contrato de comandos físicos (PIT-001 §14).

Telemetria pode ser retransmitida; comandos expirados não. Todo comando possui
identificador, solicitante, destino, operação, emissão, validade, estado esperado,
versão de configuração, política de confirmação e correlação. A autoridade do comando
é comprovada por assinatura HMAC-SHA256 com chave derivada por EDGE.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from common.timeutil import parse_ts


class CommandState(StrEnum):
    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    DISPATCHED = "DISPATCHED"
    RECEIVED = "RECEIVED"
    ACCEPTED = "ACCEPTED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


MAIN_PATH = [
    CommandState.CREATED,
    CommandState.AUTHORIZED,
    CommandState.DISPATCHED,
    CommandState.RECEIVED,
    CommandState.ACCEPTED,
    CommandState.EXECUTED,
    CommandState.VERIFIED,
]
ALTERNATIVE_STATES = frozenset(
    {CommandState.REJECTED, CommandState.EXPIRED, CommandState.FAILED, CommandState.UNKNOWN}
)
TERMINAL_STATES = ALTERNATIVE_STATES | {CommandState.VERIFIED}

MIN_TTL_S = 5
MAX_TTL_S = 600


def can_transition(current: str, new: str) -> bool:
    current, new = CommandState(current), CommandState(new)
    if current == new:
        return False
    if current == CommandState.UNKNOWN:
        # Reconciliação: evidência posterior pode esclarecer o resultado físico.
        return new in TERMINAL_STATES
    if current in TERMINAL_STATES:
        return False
    if new in ALTERNATIVE_STATES:
        return True
    return MAIN_PATH.index(new) > MAIN_PATH.index(current)


@dataclass
class Command:
    command_id: str
    tenant_id: str
    site_id: str
    device_id: str
    operation: str
    requested_by: str
    issued_at: str
    expires_at: str
    configuration_version: int
    params: dict[str, Any] = field(default_factory=dict)
    expected_state: dict[str, Any] = field(default_factory=dict)
    confirmation_policy: str = "feedback"
    correlation_id: str = ""
    state: str = CommandState.CREATED.value
    signature: str = ""

    SIGNED_FIELDS = (
        "command_id",
        "tenant_id",
        "site_id",
        "device_id",
        "operation",
        "requested_by",
        "issued_at",
        "expires_at",
        "configuration_version",
        "params",
        "expected_state",
        "confirmation_policy",
        "correlation_id",
    )

    def signing_payload(self) -> bytes:
        data = {k: getattr(self, k) for k in self.SIGNED_FIELDS}
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    def is_expired(self, now: datetime) -> bool:
        return now >= parse_ts(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Command:
        known = {f for f in cls.__dataclass_fields__}
        missing = [k for k in cls.SIGNED_FIELDS if k not in data and k not in (
            "params", "expected_state", "confirmation_policy", "correlation_id")]
        if missing:
            raise ValueError(f"Comando incompleto: {missing}")
        cmd = cls(**{k: v for k, v in data.items() if k in known})
        parse_ts(cmd.issued_at)
        parse_ts(cmd.expires_at)
        if cmd.confirmation_policy not in ("feedback", "none"):
            raise ValueError("confirmation_policy inválida")
        return cmd


def derive_edge_key(master_key: bytes | str, edge_id: str) -> bytes:
    """Chave por EDGE: o comprometimento de um EDGE não permite forjar comandos para outros."""
    if isinstance(master_key, str):
        master_key = master_key.encode()
    return hmac.new(master_key, f"terranet-command:{edge_id}".encode(), hashlib.sha256).digest()


def sign_command(command: Command, key: bytes) -> str:
    return hmac.new(key, command.signing_payload(), hashlib.sha256).hexdigest()


def verify_command(command: Command, key: bytes) -> bool:
    if not command.signature:
        return False
    return hmac.compare_digest(sign_command(command, key), command.signature)
