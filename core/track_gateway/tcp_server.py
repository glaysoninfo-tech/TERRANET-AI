"""Servidor TCP nativo para rastreadores Teltonika Codec 8 / 8E (V01 / TRACK-B / TRACK-I).

Protocolo TCP Teltonika:
1. Rastreador envia 2 bytes (tamanho) + IMEI em ASCII.
2. Servidor verifica se o IMEI está cadastrado no Device Registry -> responde 0x01 (aceito) ou 0x00.
3. Rastreador envia 1 ou mais pacotes AVL (em lote quando esvazia buffer offline — POC-T12).
4. Servidor persiste de forma idempotente no TERRANET CORE e responde 4 bytes big-endian
   com a quantidade de registros aceitos (ACK somente após persistência!).
"""

from __future__ import annotations

import logging
import socket
import socketserver
import struct
import threading

from core.storage.repository import CoreStore
from core.telemetry.service import TelemetryIngestionService
from core.track_gateway.codec8 import Codec8Error, avl_to_track_packet, decode_avl_packet

log = logging.getLogger("terranet.track_gateway")


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


class TeltonikaTcpServer:
    def __init__(self, store: CoreStore, host: str = "0.0.0.0", port: int = 5027):  # noqa: S104
        self.store = store
        self.telemetry = TelemetryIngestionService(store)
        self.host = host
        self.port = port
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def handle_avl_bytes(self, imei: str, packet: bytes) -> int:
        """Processa um pacote binário AVL e retorna o número de registros persistidos."""
        records = decode_avl_packet(packet)
        accepted = 0
        for rec in records:
            payload = avl_to_track_packet(imei, rec)
            self.telemetry.ingest_track_packet(payload)
            accepted += 1
        return accepted

    def start(self) -> int:
        gateway = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                sock: socket.socket = self.request
                sock.settimeout(30.0)
                hdr = _recv_exact(sock, 2)
                if not hdr:
                    return
                imei_len = struct.unpack(">H", hdr)[0]
                if not (8 <= imei_len <= 24):
                    sock.sendall(b"\x00")
                    return
                imei_bytes = _recv_exact(sock, imei_len)
                if not imei_bytes:
                    return
                imei = imei_bytes.decode("ascii", errors="ignore").strip()
                device = gateway.store.devices.find_by_source("tracker", imei)
                if device is None:
                    log.warning("IMEI não cadastrado rejeitado: %s", imei)
                    sock.sendall(b"\x00")
                    return
                sock.sendall(b"\x01")

                while True:
                    head = _recv_exact(sock, 8)
                    if not head:
                        break
                    preamble, data_len = struct.unpack(">II", head)
                    if preamble != 0 or data_len > 65536:
                        break
                    rest = _recv_exact(sock, data_len + 4)
                    if not rest:
                        break
                    try:
                        count = gateway.handle_avl_bytes(imei, head + rest)
                    except (Codec8Error, ValueError, PermissionError, LookupError) as exc:
                        log.warning("Pacote AVL inválido de %s: %s", imei, exc)
                        break
                    sock.sendall(struct.pack(">I", count))

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server((self.host, self.port), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def main() -> None:
    import os
    import time

    db_url = os.environ.get("TERRANET_CORE_DB_URL", "sqlite:///data/core.db")
    host = os.environ.get("TERRANET_TRACK_HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("TERRANET_TRACK_PORT", "5027"))
    store = CoreStore.open(db_url)
    server = TeltonikaTcpServer(store, host=host, port=port)
    bound = server.start()
    log.info("Teltonika Codec 8/8E TCP Gateway escutando em %s:%s", host, bound)
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        server.stop()
        store.db.close()


if __name__ == "__main__":
    main()
