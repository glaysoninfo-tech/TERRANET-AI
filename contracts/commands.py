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
    QUEUED = "QUEUED"
    TRANSMITTED = "TRANSMITTED"
    NODE_RECEIVED = "NODE_RECEIVED"
    RECEIVED = "RECEIVED"
    ACCEPTED = "ACCEPTED"
    PLC_ACCEPTED = "PLC_ACCEPTED"
    EXECUTED = "EXECUTED"
    PLC_EXECUTED = "PLC_EXECUTED"
    VERIFIED = "VERIFIED"
    PHYSICAL_STATE_VERIFIED = "PHYSICAL_STATE_VERIFIED"
    REJECTED = "REJECTED"
    PLC_REJECTED = "PLC_REJECTED"
    EXPIRED = "EXPIRED"
    RADIO_TIMEOUT = "RADIO_TIMEOUT"
    FAILED = "FAILED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    UNKNOWN = "UNKNOWN"


# Ordem progressiva (suporta tanto fluxo PIT-001 quanto estados detalhados de rádio ARQ-002 §6.3)
_STATE_RANK: dict[CommandState, int] = {
    CommandState.CREATED: 0,
    CommandState.AUTHORIZED: 1,
    CommandState.QUEUED: 2,
    CommandState.DISPATCHED: 2,
    CommandState.TRANSMITTED: 3,
    CommandState.NODE_RECEIVED: 4,
    CommandState.RECEIVED: 4,
    CommandState.ACCEPTED: 5,
    CommandState.PLC_ACCEPTED: 5,
    CommandState.EXECUTED: 6,
    CommandState.PLC_EXECUTED: 6,
    CommandState.VERIFIED: 7,
    CommandState.PHYSICAL_STATE_VERIFIED: 7,
}

ALTERNATIVE_STATES = frozenset(
    {
        CommandState.REJECTED,
        CommandState.PLC_REJECTED,
        CommandState.EXPIRED,
        CommandState.RADIO_TIMEOUT,
        CommandState.FAILED,
        CommandState.EXECUTION_FAILED,
        CommandState.UNKNOWN,
    }
)
TERMINAL_STATES = ALTERNATIVE_STATES | {
    CommandState.VERIFIED,
    CommandState.PHYSICAL_STATE_VERIFIED,
}

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
    return _STATE_RANK[new] > _STATE_RANK[current]


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
    key_id: str = "k1"
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
        "key_id",
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
        optional = {"params", "expected_state", "confirmation_policy", "correlation_id", "key_id"}
        missing = [k for k in cls.SIGNED_FIELDS if k not in data and k not in optional]
        if missing:
            raise ValueError(f"Comando incompleto: {missing}")
        cmd = cls(**{k: v for k, v in data.items() if k in known})
        parse_ts(cmd.issued_at)
        parse_ts(cmd.expires_at)
        if cmd.confirmation_policy not in ("feedback", "none"):
            raise ValueError("confirmation_policy inválida")
        return cmd


def derive_edge_key(master_key: bytes | str, edge_id: str, key_id: str = "k1") -> bytes:
    """Chave derivada por EDGE e versão de credencial (ARQ-002 §3.3)."""
    if isinstance(master_key, str):
        master_key = master_key.encode()
    context = f"terranet-command:{edge_id}" if key_id == "k1" else f"terranet-command:{edge_id}:{key_id}"
    return hmac.new(master_key, context.encode(), hashlib.sha256).digest()


def sign_command(command: Command, key: bytes) -> str:
    return hmac.new(key, command.signing_payload(), hashlib.sha256).hexdigest()


def verify_command(command: Command, key: bytes) -> bool:
    if not command.signature:
        return False
    return hmac.compare_digest(sign_command(command, key), command.signature)


def verify_command_keyring(command: Command, keyring: dict[str, bytes]) -> bool:
    """Verifica assinatura usando o `key_id` declarado para rotação sem indisponibilidade."""
    key = keyring.get(command.key_id)
    if key is None:
        return False
    return verify_command(command, key)
