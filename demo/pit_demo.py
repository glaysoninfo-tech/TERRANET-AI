import json
import math
import sqlite3
from datetime import datetime, timezone


# Registro local de dispositivos autorizados.
# No produto final, será consultado no Device Registry.

DEVICE_REGISTRY = {
    "0102030405060708": {
        "tenant_id": "CLIENTE-001",
        "site_id": "FAZENDA-001",
        "device_id": "NODE-HYDRO-001",
    }
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        raise ValueError("Timestamp sem fuso horário")

    return dt.astimezone(timezone.utc).isoformat()


def normalize_chirpstack(event):
    """
    Normaliza um uplink ChirpStack previamente
    decodificado no campo 'object'.

    Exemplo restrito à medição de nível de água.
    """

    info = event.get("deviceInfo", {})
    dev_eui = info.get("devEui")

    if dev_eui not in DEVICE_REGISTRY:
        raise ValueError("Dispositivo não cadastrado")

    dedup_id = event.get("deduplicationId")

    if not dedup_id:
        raise ValueError("Identificador de evento ausente")

    decoded = event.get("object", {})
    value = decoded.get("water_level_m")

    if type(value) not in (int, float):
        raise ValueError("Medição inválida")

    if not math.isfinite(value):
        raise ValueError("Medição não finita")

    # Limites ilustrativos de uma instalação.
    if not 0 <= value <= 20:
        raise ValueError("Medição fora da faixa")

    device = DEVICE_REGISTRY[dev_eui]

    return {
        "schema_version": "1.0",
        "event_id": f"chirpstack:{dev_eui}:{dedup_id}",
        "tenant_id": device["tenant_id"],
        "site_id": device["site_id"],
        "device_id": device["device_id"],
        "event_type": "telemetry",
        "observed_at": normalize_timestamp(event["time"]),
        "received_at": utc_now(),
        "measurements": {
            "water_level_m": float(value)
        },
        "quality": {
            "water_level_m": "valid",
            "timestamp_source": "network_server",
        },
    }


def create_database():
    db = sqlite3.connect(":memory:")

    db.execute("""
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE outbox (
            event_id TEXT PRIMARY KEY,
            confirmed INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(event_id)
                REFERENCES events(event_id)
        )
    """)

    return db


def ingest(edge_db, event):
    """
    Persiste evento e pendência na mesma transação.
    A duplicação do event_id não cria nova ocorrência.
    """

    payload = json.dumps(event, sort_keys=True)

    with edge_db:
        cursor = edge_db.execute(
            """
            INSERT OR IGNORE INTO events
            (event_id, payload)
            VALUES (?, ?)
            """,
            (event["event_id"], payload),
        )

        if cursor.rowcount == 1:
            edge_db.execute(
                """
                INSERT INTO outbox (event_id)
                VALUES (?)
                """,
                (event["event_id"],),
            )


def synchronize(edge_db, cloud_db, simulate_failure=False):
    """
    Simula sincronização com o CORE.

    A confirmação local ocorre apenas após
    a persistência no destino.
    """

    pending = edge_db.execute("""
        SELECT e.event_id, e.payload
        FROM events e
        JOIN outbox o
          ON e.event_id = o.event_id
        WHERE o.confirmed = 0
    """).fetchall()

    for event_id, payload in pending:
        # Simulação de persistência remota.
        with cloud_db:
            cloud_db.execute(
                """
                INSERT OR IGNORE INTO events
                (event_id, payload)
                VALUES (?, ?)
                """,
                (event_id, payload),
            )

        # Simula perda de comunicação depois
        # do commit remoto e antes do ACK local.
        if simulate_failure:
            raise ConnectionError("Conexão perdida antes da confirmação")

        with edge_db:
            edge_db.execute(
                """
                UPDATE outbox
                SET confirmed = 1
                WHERE event_id = ?
                """,
                (event_id,),
            )


def count_events(db):
    return db.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def count_pending(db):
    return db.execute(
        """
        SELECT COUNT(*)
        FROM outbox
        WHERE confirmed = 0
        """
    ).fetchone()[0]


def main():
    edge = create_database()
    cloud = create_database()

    # Evento de exemplo, sem dados reais.
    raw_event = {
        "deduplicationId": "event-001",
        "time": "2026-11-22T12:00:00Z",
        "deviceInfo": {
            "devEui": "0102030405060708"
        },
        "object": {
            "water_level_m": 2.45
        },
    }

    event = normalize_chirpstack(raw_event)

    # Recepção original.
    ingest(edge, event)

    # Duplicação do mesmo uplink.
    ingest(edge, event)

    assert count_events(edge) == 1

    # Simula perda de conexão após gravação
    # no CORE, antes da confirmação no EDGE.
    try:
        synchronize(edge, cloud, simulate_failure=True)
    except ConnectionError:
        pass

    assert count_events(cloud) == 1
    assert count_pending(edge) == 1

    # Reconexão e retransmissão.
    synchronize(edge, cloud)

    # Verificação de idempotência.
    assert count_events(cloud) == 1
    assert count_pending(edge) == 0

    print("Eventos no EDGE:", count_events(edge))
    print("Eventos no CORE:", count_events(cloud))
    print("Pendências:", count_pending(edge))
    print("Sincronização validada.")


if __name__ == "__main__":
    main()
