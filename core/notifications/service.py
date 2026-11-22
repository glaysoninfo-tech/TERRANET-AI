"""Despacho de alertas críticos/atenção com deduplicação e cooldown (POC-001 §5, §13.3)."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from contracts.canonical import CanonicalEvent, EventType

log = logging.getLogger("terranet.notifications")


class NotificationChannel(ABC):
    @abstractmethod
    def send(self, title: str, message: str, metadata: dict[str, Any]) -> bool:
        ...


class InMemoryNotifier(NotificationChannel):
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, title: str, message: str, metadata: dict[str, Any]) -> bool:
        self.sent.append({"title": title, "message": message, "metadata": metadata})
        return True


class WebhookNotifier(NotificationChannel):
    """Compatível com gateways WhatsApp (Evolution/Z-API/Meta), Slack ou alertas customizados."""

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout_s: float = 5.0):
        self.url = url
        self.headers = headers or {}
        self.timeout_s = timeout_s

    def send(self, title: str, message: str, metadata: dict[str, Any]) -> bool:
        import httpx

        try:
            resp = httpx.post(
                self.url,
                json={"title": title, "message": message, "metadata": metadata},
                headers=self.headers,
                timeout=self.timeout_s,
            )
            return 200 <= resp.status_code < 300
        except httpx.HTTPError as exc:
            log.warning("Falha ao enviar webhook de alerta: %s", exc)
            return False


class TelegramNotifier(NotificationChannel):
    def __init__(self, bot_token: str, chat_id: str, timeout_s: float = 5.0):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_s = timeout_s

    def send(self, title: str, message: str, metadata: dict[str, Any]) -> bool:
        import httpx

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            resp = httpx.post(
                url,
                json={"chat_id": self.chat_id, "text": f"[{title}]\n{message}"},
                timeout=self.timeout_s,
            )
            return 200 <= resp.status_code < 300
        except httpx.HTTPError as exc:
            log.warning("Falha ao enviar alerta Telegram: %s", exc)
            return False


class NotificationService:
    def __init__(
        self,
        channels: list[NotificationChannel] | None = None,
        cooldown_s: float = 60.0,
    ):
        self.channels = channels if channels is not None else []
        self.cooldown_s = cooldown_s
        self._last_sent: dict[str, float] = {}

    def notify_event(self, event: CanonicalEvent) -> int:
        if not self.channels or event.event_type != EventType.EVENT:
            return 0
        attrs = event.attributes or {}
        severity = str(attrs.get("severity", "info")).lower()
        if severity not in ("warning", "critical"):
            return 0

        dedup_key = f"{event.tenant_id}:{event.device_id}:{attrs.get('rule_id')}:{attrs.get('alarm_status')}"
        now = time.monotonic()
        last = self._last_sent.get(dedup_key)
        if last is not None and (now - last) < self.cooldown_s:
            return 0
        self._last_sent[dedup_key] = now

        title = f"TERRANET {severity.upper()} · {event.device_id}"
        message = (
            f"Cliente: {event.tenant_id} / {event.site_id}\n"
            f"Regra: {attrs.get('rule_id')} → {attrs.get('alarm_status')}\n"
            f"Observado em: {event.observed_at}\n"
            f"Detalhes: {attrs.get('details', {})}"
        )
        delivered = 0
        for ch in self.channels:
            if ch.send(title, message, {"event_id": event.event_id, **attrs}):
                delivered += 1
        return delivered
