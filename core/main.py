"""Ponto de entrada ASGI/Uvicorn do TERRANET CORE."""

from __future__ import annotations

import os

from core.api.app import create_app
from core.notifications.service import (
    NotificationChannel,
    NotificationService,
    TelegramNotifier,
    WebhookNotifier,
)
from core.storage.repository import CoreStore


def build_app():
    db_url = os.environ.get("TERRANET_CORE_DB_URL", "sqlite:///data/core.db")
    master_key = os.environ.get("TERRANET_MASTER_KEY", "dev-insecure-change-me")
    rate_limit = int(os.environ.get("TERRANET_RATE_LIMIT_RPM", "300"))
    track_webhook_token = os.environ.get("TERRANET_TRACK_WEBHOOK_TOKEN")

    channels: list[NotificationChannel] = []
    if os.environ.get("TERRANET_ALERT_WEBHOOK_URL"):
        channels.append(WebhookNotifier(os.environ["TERRANET_ALERT_WEBHOOK_URL"]))
    if os.environ.get("TERRANET_TELEGRAM_BOT_TOKEN") and os.environ.get("TERRANET_TELEGRAM_CHAT_ID"):
        channels.append(
            TelegramNotifier(
                os.environ["TERRANET_TELEGRAM_BOT_TOKEN"],
                os.environ["TERRANET_TELEGRAM_CHAT_ID"],
            )
        )
    notifications = NotificationService(channels=channels) if channels else None

    store = CoreStore.open(db_url)
    return create_app(
        store=store,
        master_key=master_key,
        rate_limit_rpm=rate_limit,
        notifications=notifications,
        track_webhook_token=track_webhook_token,
    )


def main() -> None:
    import uvicorn

    host = os.environ.get("TERRANET_CORE_HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("TERRANET_CORE_PORT", "8000"))
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
