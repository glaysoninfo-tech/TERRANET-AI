"""Abstração de broker MQTT com políticas de QoS/retenção do PIT-001 §10.3."""

from __future__ import annotations

import fnmatch
import json
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from contracts.topics import DELIVERY_POLICY, TopicKind, build_topic, kind_for_event

MessageHandler = Callable[[str, bytes], None]


def _mqtt_topic_matches(pattern: str, topic: str) -> bool:
    """Correspondência de wildcards MQTT (``+`` e ``#``)."""
    p_parts = pattern.split("/")
    t_parts = topic.split("/")
    for i, part in enumerate(p_parts):
        if part == "#":
            return True
        if i >= len(t_parts):
            return False
        if part != "+" and part != t_parts[i]:
            return False
    return len(p_parts) == len(t_parts)


@dataclass
class PublishedMessage:
    topic: str
    payload: bytes
    qos: int
    retain: bool


class MessageBus(ABC):
    @property
    @abstractmethod
    def connected(self) -> bool:
        ...

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def publish(self, topic: str, payload: bytes | str | dict[str, Any],
                qos: int = 1, retain: bool = False) -> None:
        ...

    @abstractmethod
    def subscribe(self, pattern: str, handler: MessageHandler, qos: int = 1) -> None:
        ...

    def publish_canonical(self, event_dict: dict[str, Any]) -> str:
        """Publica um evento canônico no tópico TERRANET com QoS/retenção padrão (§10.3)."""
        kind = kind_for_event(event_dict["event_type"])
        qos, retain = DELIVERY_POLICY[kind]
        if kind == TopicKind.COMMAND:
            retain = False  # comandos físicos nunca são retidos
        topic = build_topic(
            event_dict["tenant_id"],
            event_dict["site_id"],
            event_dict["device_id"],
            kind,
        )
        self.publish(topic, event_dict, qos=qos, retain=retain)
        return topic

    @staticmethod
    def encode(payload: bytes | str | dict[str, Any]) -> bytes:
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, str):
            return payload.encode("utf-8")
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class InMemoryBus(MessageBus):
    """Broker em memória para testes unitários, integração local e simulação offline."""

    def __init__(self) -> None:
        self._connected = True
        self._lock = threading.RLock()
        self._subs: list[tuple[str, MessageHandler]] = []
        self.published: list[PublishedMessage] = []
        self.retained: dict[str, bytes] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def publish(
        self,
        topic: str,
        payload: bytes | str | dict[str, Any],
        qos: int = 1,
        retain: bool = False,
    ) -> None:
        raw = self.encode(payload)
        with self._lock:
            if not self._connected:
                raise ConnectionError("Broker MQTT desconectado")
            self.published.append(PublishedMessage(topic=topic, payload=raw, qos=qos, retain=retain))
            if retain:
                self.retained[topic] = raw
            handlers = [h for pat, h in self._subs if _mqtt_topic_matches(pat, topic)]
        for handler in handlers:
            handler(topic, raw)

    def subscribe(self, pattern: str, handler: MessageHandler, qos: int = 1) -> None:
        with self._lock:
            self._subs.append((pattern, handler))
            retained = [
                (topic, data)
                for topic, data in self.retained.items()
                if _mqtt_topic_matches(pattern, topic)
            ]
        for topic, data in retained:
            handler(topic, data)


class PahoMqttBus(MessageBus):
    """Cliente MQTT baseado em ``paho-mqtt`` v2 com reconexão automática."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        client_id: str = "terranet-edge",
        username: str | None = None,
        password: str | None = None,
    ):
        import paho.mqtt.client as mqtt

        self._host = host
        self._port = port
        self._connected = False
        self._subs: list[tuple[str, int, MessageHandler]] = []
        self._lock = threading.RLock()
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=False,
        )
        if username:
            self._client.username_pw_set(username, password)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def connected(self) -> bool:
        return self._connected

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: Any, props: Any = None) -> None:
        self._connected = True
        with self._lock:
            for pattern, qos, _ in self._subs:
                client.subscribe(pattern, qos=qos)

    def _on_disconnect(
        self, client: Any, userdata: Any, flags: Any, rc: Any, props: Any = None
    ) -> None:
        self._connected = False

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        with self._lock:
            handlers = [h for pat, _, h in self._subs if _mqtt_topic_matches(pat, msg.topic)]
        for handler in handlers:
            handler(msg.topic, bytes(msg.payload))

    def connect(self) -> None:
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
        self._connected = False

    def publish(
        self,
        topic: str,
        payload: bytes | str | dict[str, Any],
        qos: int = 1,
        retain: bool = False,
    ) -> None:
        self._client.publish(topic, self.encode(payload), qos=qos, retain=retain)

    def subscribe(self, pattern: str, handler: MessageHandler, qos: int = 1) -> None:
        with self._lock:
            self._subs.append((pattern, qos, handler))
            if self._connected:
                self._client.subscribe(pattern, qos=qos)


__all__ = ["InMemoryBus", "MessageBus", "PahoMqttBus", "PublishedMessage", "fnmatch"]
