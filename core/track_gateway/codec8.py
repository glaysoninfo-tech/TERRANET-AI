"""Parser e codificador binário Teltonika Codec 8 (0x08) e Codec 8 Extended (0x8E).

Suporta rastreadores comerciais como FMC130 (TRACK-B) e FMC250 (TRACK-I com CAN/J1939)
conforme POC-001 §4.3, §6.4 e §15, incluindo verificação CRC-16/IBM e mapeamento dos
principais AVL IO IDs e parâmetros CAN/J1939 somente leitura.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from typing import Any

CODEC_8 = 0x08
CODEC_8E = 0x8E


class Codec8Error(ValueError):
    pass


# Mapa de AVL IO IDs padrão Teltonika (FMC130 / FMC250 / LV-CAN / ALL-CAN) -> campos do TrackerAdapter
TELTONIKA_IO_MAP: dict[int, tuple[str, float]] = {
    239: ("ignition", 1.0),          # 0/1
    66: ("external_mv", 1.0),        # mV
    67: ("battery_mv", 1.0),         # mV
    16: ("odometer_m", 1.0),         # metros
    85: ("rpm", 1.0),                # CAN Engine RPM
    89: ("fuel_pct", 1.0),           # CAN Fuel Level %
    84: ("fuel_pct", 1.0),           # OBD/CAN Fuel Level %
    115: ("coolant_temp_c", 0.1),    # CAN Engine Coolant Temp (0.1 degC)
    102: ("engine_hours_h", 1 / 60), # CAN Engine Worktime (minutos -> horas)
    103: ("engine_hours_h", 1.0),    # CAN Engine Hours (h)
    107: ("fuel_rate_lh", 0.1),      # CAN Fuel Rate (0.1 L/h)
}


def crc16_ibm(data: bytes) -> int:
    """CRC-16/IBM (polinômio 0xA001, valor inicial 0x0000) usado nos frames Teltonika."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


def decode_avl_packet(packet: bytes) -> list[dict[str, Any]]:
    """Decodifica um pacote TCP Teltonika completo (preamble + length + data + CRC-16)."""
    if len(packet) < 12:
        raise Codec8Error("Pacote AVL muito curto")
    preamble, data_len = struct.unpack(">II", packet[:8])
    if preamble != 0:
        raise Codec8Error(f"Preamble inválido: {preamble:#x}")
    if len(packet) != 8 + data_len + 4:
        raise Codec8Error(
            f"Comprimento inconsistente: esperado {8 + data_len + 4}, recebido {len(packet)}"
        )
    body = packet[8 : 8 + data_len]
    crc_actual = struct.unpack(">I", packet[8 + data_len :])[0]
    crc_expected = crc16_ibm(body)
    if crc_actual != crc_expected:
        raise Codec8Error(f"CRC-16 inválido: esperado {crc_expected:#06x}, recebido {crc_actual:#06x}")

    codec_id = body[0]
    if codec_id not in (CODEC_8, CODEC_8E):
        raise Codec8Error(f"Codec não suportado: {codec_id:#04x}")
    count1 = body[1]
    count2 = body[-1]
    if count1 != count2:
        raise Codec8Error(f"Contagem de registros divergente ({count1} != {count2})")

    extended = codec_id == CODEC_8E
    pos = 2
    records: list[dict[str, Any]] = []
    for _ in range(count1):
        ts_ms, priority, lon_raw, lat_raw, alt_m, angle, sats, speed = struct.unpack_from(
            ">QBiiHHBH", body, pos
        )
        pos += 24
        if extended:
            event_io_id, total_io = struct.unpack_from(">HH", body, pos)
            pos += 4
            id_fmt, id_size = ">H", 2
            cnt_fmt, cnt_size = ">H", 2
        else:
            event_io_id, total_io = struct.unpack_from(">BB", body, pos)
            pos += 2
            id_fmt, id_size = ">B", 1
            cnt_fmt, cnt_size = ">B", 1

        io_raw: dict[int, int | bytes] = {}
        for val_fmt, val_size in ((">B", 1), (">H", 2), (">I", 4), (">Q", 8)):
            n = struct.unpack_from(cnt_fmt, body, pos)[0]
            pos += cnt_size
            for _ in range(n):
                io_id = struct.unpack_from(id_fmt, body, pos)[0]
                pos += id_size
                io_val = struct.unpack_from(val_fmt, body, pos)[0]
                pos += val_size
                io_raw[io_id] = io_val

        if extended:
            # Elementos X de tamanho variável (Codec 8E)
            nx = struct.unpack_from(">H", body, pos)[0]
            pos += 2
            for _ in range(nx):
                io_id, xlen = struct.unpack_from(">HH", body, pos)
                pos += 4
                io_raw[io_id] = body[pos : pos + xlen]
                pos += xlen

        records.append(
            {
                "codec": codec_id,
                "timestamp_ms": ts_ms,
                "timestamp": datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).isoformat(),
                "priority": priority,
                "gnss": {
                    "lat": round(lat_raw / 1e7, 7),
                    "lon": round(lon_raw / 1e7, 7),
                    "alt_m": float(alt_m),
                    "heading_deg": float(angle),
                    "satellites": int(sats),
                    "speed_kmh": float(speed),
                },
                "event_io_id": event_io_id,
                "total_io": total_io,
                "io_raw": io_raw,
            }
        )
    return records


def encode_avl_packet(records: list[dict[str, Any]], codec_id: int = CODEC_8E) -> bytes:
    """Codifica registros em frame binário Teltonika Codec 8 ou 8E (útil para bancada e testes)."""
    if codec_id not in (CODEC_8, CODEC_8E):
        raise Codec8Error(f"Codec não suportado: {codec_id:#04x}")
    extended = codec_id == CODEC_8E
    id_fmt = ">H" if extended else ">B"
    cnt_fmt = ">H" if extended else ">B"

    buf = bytearray([codec_id, len(records)])
    for rec in records:
        ts_ms = int(rec["timestamp_ms"])
        g = rec["gnss"]
        buf += struct.pack(
            ">QBiiHHBH",
            ts_ms,
            int(rec.get("priority", 0)),
            int(round(float(g["lon"]) * 1e7)),
            int(round(float(g["lat"]) * 1e7)),
            int(round(float(g.get("alt_m", 0)))),
            int(round(float(g.get("heading_deg", 0)))),
            int(g.get("satellites", 10)),
            int(round(float(g.get("speed_kmh", 0)))),
        )
        io_raw: dict[int, int] = dict(rec.get("io_raw") or {})
        b1, b2, b4, b8 = [], [], [], []
        for k, v in io_raw.items():
            iv = int(v)
            if 0 <= iv <= 0xFF:
                b1.append((k, iv))
            elif 0 <= iv <= 0xFFFF:
                b2.append((k, iv))
            elif 0 <= iv <= 0xFFFFFFFF:
                b4.append((k, iv))
            else:
                b8.append((k, iv))
        buf += struct.pack(id_fmt, int(rec.get("event_io_id", 0)))
        buf += struct.pack(cnt_fmt, len(io_raw))
        for bucket, vfmt in ((b1, ">B"), (b2, ">H"), (b4, ">I"), (b8, ">Q")):
            buf += struct.pack(cnt_fmt, len(bucket))
            for k, iv in bucket:
                buf += struct.pack(id_fmt, k)
                buf += struct.pack(vfmt, iv)
        if extended:
            buf += struct.pack(">H", 0)  # 0 elementos de tamanho variável
    buf.append(len(records))
    body = bytes(buf)
    return struct.pack(">II", 0, len(body)) + body + struct.pack(">I", crc16_ibm(body))


def avl_to_track_packet(imei: str, record: dict[str, Any]) -> dict[str, Any]:
    """Converte um registro AVL decodificado para o envelope esperado pelo TrackerAdapter."""
    io: dict[str, Any] = {}
    can: dict[str, Any] = {}
    for io_id, raw_val in (record.get("io_raw") or {}).items():
        if isinstance(raw_val, bytes):
            continue
        mapped = TELTONIKA_IO_MAP.get(int(io_id))
        if not mapped:
            continue
        field, scale = mapped
        value: Any = bool(raw_val) if field == "ignition" else round(float(raw_val) * scale, 4)
        if field in ("rpm", "coolant_temp_c", "engine_hours_h", "fuel_rate_lh", "fuel_pct"):
            can[field] = value
        else:
            io[field] = value
    return {
        "imei": imei,
        "timestamp": record["timestamp"],
        "gnss": record["gnss"],
        "io": io,
        "can": can or None,
        "transport": "teltonika_codec8e" if record.get("codec") == CODEC_8E else "teltonika_codec8",
    }
