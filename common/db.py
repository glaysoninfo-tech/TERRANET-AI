"""Camada mínima de acesso a dados (ADR-007 / ADR-010).

Suporta PostgreSQL (referência de produção) e SQLite (EDGE leve, testes e demonstrações)
com as mesmas consultas SQL. Convenções para manter portabilidade:

* placeholders ``?`` (convertidos para ``%s`` no PostgreSQL);
* ``INSERT ... ON CONFLICT DO NOTHING`` para idempotência;
* identificadores gerados na aplicação (sem SERIAL/AUTOINCREMENT);
* instantes passados como ISO-8601 UTC (``common.timeutil.iso``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from common.timeutil import iso


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def loads(value: Any) -> Any:
    if value is None or isinstance(value, dict | list):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        value = bytes(value).decode()
    return json.loads(value) if value != "" else None


class Database:
    """Conexão única protegida por lock reentrante (suficiente para EDGE e MVP do CORE)."""

    def __init__(self, url: str):
        self.url = url
        self._lock = threading.RLock()
        self._depth = 0
        if url.startswith("sqlite:"):
            self.dialect = "sqlite"
            path = url.split("sqlite:///", 1)[1] if "sqlite:///" in url else ":memory:"
            path = path or ":memory:"
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                path, isolation_level=None, check_same_thread=False, timeout=30
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = FULL")
            self.integrity_errors: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,)
            self.transient_errors: tuple[type[BaseException], ...] = (sqlite3.OperationalError,)
        elif url.startswith(("postgresql://", "postgres://")):
            import psycopg
            from psycopg.rows import dict_row

            self.dialect = "postgresql"
            self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
            self.integrity_errors = (psycopg.IntegrityError,)
            self.transient_errors = (psycopg.OperationalError,)
        else:
            raise ValueError(f"URL de banco não suportada: {url}")

    # ------------------------------------------------------------------ SQL
    def _sql(self, sql: str) -> str:
        if self.dialect == "postgresql":
            return sql.replace("%", "%%").replace("?", "%s")
        return sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            return self._conn.execute(self._sql(sql), tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(self._sql(sql), tuple(params)).fetchall()
        return [self._row(r) for r in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return next(iter(row.values())) if row else None

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        data = dict(row)
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = iso(value)
        return data

    # ---------------------------------------------------------- transações
    @contextmanager
    def transaction(self) -> Iterator[Database]:
        """Transação atômica; chamadas aninhadas usam SAVEPOINT."""
        with self._lock:
            if self.dialect == "postgresql":
                with self._conn.transaction():
                    yield self
                return
            depth = self._depth
            savepoint = f"sp_{depth}"
            self._conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if depth == 0:
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self._depth -= 1
                self._conn.execute("COMMIT" if depth == 0 else f"RELEASE SAVEPOINT {savepoint}")

    def executescript(self, script: str) -> None:
        with self._lock:
            if self.dialect == "sqlite":
                self._conn.executescript(script)
            else:
                self._conn.execute(script)

    def apply_schema(self, migrations_dir: Path) -> None:
        """Aplica ``<dialeto>.sql`` (idempotente: CREATE ... IF NOT EXISTS)."""
        path = Path(migrations_dir) / f"{self.dialect}.sql"
        self.executescript(path.read_text(encoding="utf-8"))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
