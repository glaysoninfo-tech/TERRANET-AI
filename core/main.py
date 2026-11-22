"""Ponto de entrada ASGI/Uvicorn do TERRANET CORE."""

from __future__ import annotations

import os

from core.api.app import create_app
from core.storage.repository import CoreStore


def build_app():
    db_url = os.environ.get("TERRANET_CORE_DB_URL", "sqlite:///data/core.db")
    master_key = os.environ.get("TERRANET_MASTER_KEY", "dev-insecure-change-me")
    rate_limit = int(os.environ.get("TERRANET_RATE_LIMIT_RPM", "300"))
    store = CoreStore.open(db_url)
    return create_app(store=store, master_key=master_key, rate_limit_rpm=rate_limit)


def main() -> None:
    import uvicorn

    host = os.environ.get("TERRANET_CORE_HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("TERRANET_CORE_PORT", "8000"))
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
