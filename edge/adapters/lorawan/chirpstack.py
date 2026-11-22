"""Adaptador LoRaWAN para eventos MQTT do ChirpStack v4 (PIT-001 §6).

Tópico de entrada: ``application/{application_id}/device/{dev_eui}/event/{event}``.
O protocolo LoRaWAN (OTAA, sessões, contadores, downlinks) permanece no Network Server;
este adaptador apenas interpreta eventos já validados por ele.

O DevEUI identifica o dispositivo na rede, mas não é credencial de autenticação: o vínculo
DevEUI -> cliente/instalação é obtido no Device Registry.
"""

from __future__ import annotations

import hashlib
from typing import Any

from contracts.canonical import CanonicalEvent, EventType, Quality, TimestampSource
from contracts.devices import Device, normalize_identifier
from edge.adapters import Adapter, AdapterError, register_adapter
from edge.adapters.lorawan.decoders import get_decoder
from edge.normalization import DEFAULT_NORMALIZER, NormalizationError

HANDLED_EVENTS = {"up", "status", "join"}


class ChirpStackAdapter(Adapter):
    adapter_id = "lorawan_chirpstack"
    version = "1.0"
    source_type = "lorawan"

    def identify(self, raw: dict[str, Any]) -> str:
        dev_eui = (raw.get("deviceInfo") or {}).get("devEui")
        if not dev_eui:
            raise AdapterError("Evento sem deviceInfo.devEui")
        try:
            return normalize_identifier("lorawan", dev_eui)
        except ValueError as exc:
            raise AdapterError(str(exc)) from exc

    @staticmethod
    def _event_id(dev_eui: str, raw: dict[str, Any], kind: str) -> str:
        dedup = raw.get("deduplicationId")
        if dedup:
            suffix = "" if kind == "up" else f":{kind}"
            return f"chirpstack:{dev_eui}:{dedup}{suffix}"
        if kind == "up":
            # Contador de quadros NÃO é usado isoladamente como identificador global (§9.2).
            raise AdapterError("Uplink sem deduplicationId")
        digest = hashlib.sha256(
            f"{dev_eui}|{kind}|{raw.get('time')}".encode()
        ).hexdigest()[:24]
        return f"chirpstack:{dev_eui}:{kind}:{digest}"

    @staticmethod
    def _radio(raw: dict[str, Any]) -> dict[str, Any]:
        rx = raw.get("rxInfo") or []
        rssi = [g.get("rssi") for g in rx if isinstance(g.get("rssi"), int | float)]
        snr = [g.get("snr") for g in rx if isinstance(g.get("snr"), int | float)]
        radio: dict[str, Any] = {"gateways": len(rx)}
        if rssi:
            radio["rssi_dbm"] = max(rssi)
        if snr:
            radio["snr_db"] = max(snr)
        return radio

    def to_events(
        self,
        raw: dict[str, Any],
        device: Device,
        received_at: str,
        context: dict[str, Any] | None = None,
    ) -> list[CanonicalEvent]:
        context = context or {}
        kind = context.get("event", "up")
        if kind not in HANDLED_EVENTS:
            return []
        dev_eui = self.identify(raw)
        if dev_eui != device.source_identifier:
            raise AdapterError("DevEUI não corresponde ao dispositivo resolvido")
        event_id = self._event_id(dev_eui, raw, kind)
        observed_at, ts_source, ts_suspect = DEFAULT_NORMALIZER.resolve_timestamp(
            raw.get("time"), received_at, TimestampSource.NETWORK_SERVER
        )
        source = self.source(
            network_server="chirpstack",
            event=kind,
            f_cnt=raw.get("fCnt"),
            f_port=raw.get("fPort"),
            radio=self._radio(raw),
        )
        base = {
            "event_id": event_id,
            "tenant_id": device.tenant_id,
            "site_id": device.site_id,
            "device_id": device.device_id,
            "observed_at": observed_at,
            "received_at": received_at,
            "source": {k: v for k, v in source.items() if v is not None},
        }

        if kind == "up":
            decoder_name = device.metadata.get("decoder", "chirpstack_object")
            raw_measurements, hints = get_decoder(decoder_name)(raw, device)
            try:
                measurements, quality = DEFAULT_NORMALIZER.normalize(
                    raw_measurements, device, hints
                )
            except NormalizationError as exc:
                raise AdapterError(str(exc)) from exc
            event_type = EventType.TELEMETRY
        elif kind == "status":
            raw_measurements = {
                "battery_level_pct": (raw.get("batteryLevel"), "%"),
                "link_margin_db": (raw.get("margin"), "dB"),
            }
            measurements, quality = DEFAULT_NORMALIZER.normalize(raw_measurements, device)
            event_type = EventType.DIAGNOSTIC
        else:  # join
            measurements, quality = {}, {}
            event_type = EventType.DIAGNOSTIC
            base["attributes"] = {"lorawan_event": "join", "dev_addr": raw.get("devAddr")}

        quality["timestamp_source"] = ts_source
        if ts_suspect:
            base.setdefault("attributes", {})["timestamp_quality"] = Quality.SUSPECT.value
        event = CanonicalEvent(
            event_type=event_type.value, measurements=measurements, quality=quality, **base
        )
        return [event]


ADAPTER = register_adapter(ChirpStackAdapter(), "lorawan_modbus")
