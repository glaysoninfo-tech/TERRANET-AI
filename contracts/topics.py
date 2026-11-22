"""Convenção de tópicos MQTT TERRANET (PIT-001 §10) e tópicos de entrada ChirpStack (§6)."""

from __future__ import annotations

import re
from enum import StrEnum

TOPIC_ROOT = "terranet"
TOPIC_VERSION = "v1"


class TopicKind(StrEnum):
    TELEMETRY = "telemetry"
    STATE = "state"
    EVENT = "event"
    COMMAND = "command"
    COMMAND_RESULT = "command-result"


# Política inicial de entrega (§10.3): (QoS, retain)
DELIVERY_POLICY: dict[TopicKind, tuple[int, bool]] = {
    TopicKind.TELEMETRY: (1, False),
    TopicKind.STATE: (1, True),  # retenção controlada: somente o estado atual
    TopicKind.EVENT: (1, False),
    TopicKind.COMMAND: (1, False),  # comandos físicos nunca retidos
    TopicKind.COMMAND_RESULT: (1, False),
}

_SLUG_RE = re.compile(r"[^a-z0-9_-]")


def slug(value: str) -> str:
    """Normaliza um identificador para um nível de tópico seguro (sem ``/ + #``)."""
    result = _SLUG_RE.sub("", str(value).strip().lower())
    if not result:
        raise ValueError(f"Identificador inválido para tópico: {value!r}")
    return result


def build_topic(tenant: str, site: str, device: str, kind: TopicKind | str) -> str:
    kind = TopicKind(kind)
    return "/".join([TOPIC_ROOT, TOPIC_VERSION, slug(tenant), slug(site), slug(device), kind.value])


def subscription(
    tenant: str = "+", site: str = "+", device: str = "+", kind: str = "+"
) -> str:
    parts = [tenant, site, device]
    levels = [p if p in ("+", "#") else slug(p) for p in parts]
    kind_level = kind if kind in ("+", "#") else TopicKind(kind).value
    return "/".join([TOPIC_ROOT, TOPIC_VERSION, *levels, kind_level])


def parse_topic(topic: str) -> dict[str, str]:
    parts = topic.split("/")
    if len(parts) != 6 or parts[0] != TOPIC_ROOT or parts[1] != TOPIC_VERSION:
        raise ValueError(f"Tópico TERRANET inválido: {topic}")
    return {
        "tenant": parts[2],
        "site": parts[3],
        "device": parts[4],
        "kind": TopicKind(parts[5]).value,
    }


def kind_for_event(event_type: str) -> TopicKind:
    return {
        "telemetry": TopicKind.TELEMETRY,
        "position": TopicKind.TELEMETRY,
        "diagnostic": TopicKind.TELEMETRY,
        "state": TopicKind.STATE,
        "event": TopicKind.EVENT,
        "command_result": TopicKind.COMMAND_RESULT,
    }[event_type]


# ChirpStack v4: application/{application_id}/device/{dev_eui}/event/{event}
CHIRPSTACK_EVENT_TOPIC = "application/{application_id}/device/{dev_eui}/event/{event}"
CHIRPSTACK_COMMAND_TOPIC = "application/{application_id}/device/{dev_eui}/command/{command}"


def chirpstack_subscription(prefix: str = "") -> str:
    return f"{prefix}application/+/device/+/event/+"


def chirpstack_downlink_topic(application_id: str, dev_eui: str, prefix: str = "") -> str:
    return f"{prefix}application/{application_id}/device/{dev_eui.lower()}/command/down"


def parse_chirpstack_topic(topic: str, prefix: str = "") -> dict[str, str] | None:
    if prefix and not topic.startswith(prefix):
        return None
    parts = topic[len(prefix):].split("/")
    if (
        len(parts) != 6
        or parts[0] != "application"
        or parts[2] != "device"
        or parts[4] != "event"
    ):
        return None
    return {"application_id": parts[1], "dev_eui": parts[3].lower(), "event": parts[5]}
