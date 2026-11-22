"""Adaptador TRACK para telemetria móvel GNSS/CAN/LTE (E05 / V01).

Pode ser executado tanto na borda quanto diretamente na camada de recepção do TERRANET CORE
(§3: rastreadores celulares não dependem de um Gateway EDGE fixo).

Suporta payloads de rastreadores de fabricantes distintos via mapeamento de campos
(``generic_gnss_v1`` e ``teltonika_json_v1``), produzindo sempre o mesmo evento canônico
``position`` com identificador estável (§9.2).
"""

from __future__ import annotations

import hashlib
from typing import Any

from common.timeutil import parse_ts
from contracts.canonical import CanonicalEvent, EventType, Quality, TimestampSource
from contracts.devices import Device
from edge.adapters import Adapter, AdapterError, register_adapter
from edge.normalization import DEFAULT_NORMALIZER, NormalizationError

DELAYED_THRESHOLD_S = 120.0


class TrackerAdapter(Adapter):
    adapter_id = "tracker_gnss_lte"
    version = "1.1"
    source_type = "tracker"

    @staticmethod
    def _is_traccar_payload(raw: dict[str, Any]) -> bool:
        return isinstance(raw.get("position"), dict) and isinstance(raw.get("device"), dict)

    def identify(self, raw: dict[str, Any]) -> str:
        dev = raw.get("device") or {}
        imei = (
            raw.get("imei")
            or raw.get("device_imei")
            or dev.get("imei")
            or dev.get("uniqueId")
        )
        if not imei:
            raise AdapterError("Pacote TRACK sem IMEI/identificador")
        return str(imei).strip()

    @classmethod
    def _extract_location(cls, raw: dict[str, Any]) -> dict[str, Any]:
        is_traccar = cls._is_traccar_payload(raw)
        gnss = raw.get("position") if is_traccar else (raw.get("gnss") or raw.get("gps") or raw.get("location") or raw)
        lat = gnss.get("lat") if "lat" in gnss else gnss.get("latitude")
        lon = gnss.get("lon") if "lon" in gnss else gnss.get("longitude")
        if not isinstance(lat, int | float) or isinstance(lat, bool) or not (-90 <= lat <= 90):
            raise AdapterError(f"Latitude inválida: {lat!r}")
        if not isinstance(lon, int | float) or isinstance(lon, bool) or not (-180 <= lon <= 180):
            raise AdapterError(f"Longitude inválida: {lon!r}")

        loc: dict[str, Any] = {"lat": float(lat), "lon": float(lon)}
        alt = gnss.get("alt_m") if "alt_m" in gnss else gnss.get("altitude")
        if isinstance(alt, int | float) and not isinstance(alt, bool):
            loc["alt_m"] = float(alt)
        speed = gnss.get("speed_kmh") if "speed_kmh" in gnss else gnss.get("speed")
        if isinstance(speed, int | float) and not isinstance(speed, bool):
            # Traccar reporta velocidade em nós (knots)
            loc["speed_kmh"] = round(float(speed) * 1.852, 3) if is_traccar else float(speed)
        heading = (
            gnss.get("heading_deg")
            if "heading_deg" in gnss
            else gnss.get("angle", gnss.get("course"))
        )
        if isinstance(heading, int | float) and not isinstance(heading, bool):
            loc["heading_deg"] = float(heading)
        attrs = gnss.get("attributes") if isinstance(gnss.get("attributes"), dict) else {}
        sats = gnss.get("satellites") if "satellites" in gnss else gnss.get("sat", attrs.get("sat"))
        if isinstance(sats, int) and not isinstance(sats, bool):
            loc["satellites"] = sats
        hdop = gnss.get("hdop", attrs.get("hdop"))
        if isinstance(hdop, int | float) and not isinstance(hdop, bool):
            loc["hdop"] = float(hdop)
        return loc

    @classmethod
    def _extract_measurements(
        cls, raw: dict[str, Any], location: dict[str, Any], device: Device
    ) -> dict[str, tuple[Any, str | None]]:
        measurements: dict[str, tuple[Any, str | None]] = {}
        if "speed_kmh" in location:
            measurements["speed_kmh"] = (location["speed_kmh"], "km/h")
        if "heading_deg" in location:
            measurements["heading_deg"] = (location["heading_deg"], "deg")

        # Suporte a Codec 8E/Teltonika (`io`), Traccar (`position.attributes`), CAN (`can`) ou raiz
        io_data: dict[str, Any] = {}
        if cls._is_traccar_payload(raw):
            io_data.update((raw.get("position") or {}).get("attributes") or {})
        else:
            io_data.update(raw.get("io") or raw.get("telemetry") or raw)
        if isinstance(raw.get("can"), dict):
            io_data.update(raw["can"])

        field_map = device.metadata.get("field_map") or {
            "battery_voltage_v": {"field": "battery_mv", "unit": "mV"},
            "external_voltage_v": {"field": "external_mv", "unit": "mV"},
            "ignition_state": {"field": "ignition"},
            "odometer_km": {"field": "odometer_m", "unit": "m"},
            "fuel_level_pct": {"field": "fuel_pct", "unit": "%"},
            "fuel_rate_lh": {"field": "fuel_rate_lh", "unit": "L/h"},
            "engine_rpm": {"field": "rpm", "unit": "rpm"},
            "coolant_temperature_c": {"field": "coolant_temp_c", "unit": "degC"},
            "engine_hours_h": {"field": "engine_hours_h", "unit": "h"},
        }
        for canonical, spec in field_map.items():
            spec = {"field": spec} if isinstance(spec, str) else spec
            src_field = spec["field"]
            if src_field in io_data and io_data[src_field] is not None:
                measurements[canonical] = (io_data[src_field], spec.get("unit"))
            elif canonical in io_data and io_data[canonical] is not None:
                measurements[canonical] = (io_data[canonical], None)
        # Traccar usa `battery` em V ou `power` em V
        if "battery_voltage_v" not in measurements and isinstance(io_data.get("battery"), int | float):
            measurements["battery_voltage_v"] = (io_data["battery"], "V")
        if "external_voltage_v" not in measurements and isinstance(io_data.get("power"), int | float):
            measurements["external_voltage_v"] = (io_data["power"], "V")
        return measurements

    @staticmethod
    def _event_id(imei: str, raw: dict[str, Any], observed_at: str, location: dict[str, Any]) -> str:
        record_id = raw.get("record_id") or raw.get("seq") or raw.get("packet_id")
        if record_id is not None and str(record_id).strip():
            return f"track:{imei}:{record_id}"
        # Chave estável construída de informações documentadas (§9.2)
        key = f"{imei}|{observed_at}|{location['lat']:.6f}|{location['lon']:.6f}"
        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        return f"track:{imei}:{digest}"

    def to_events(
        self,
        raw: dict[str, Any],
        device: Device,
        received_at: str,
        context: dict[str, Any] | None = None,
    ) -> list[CanonicalEvent]:
        imei = self.identify(raw)
        if imei != device.source_identifier:
            raise AdapterError("IMEI do pacote não corresponde ao dispositivo cadastrado")

        location = self._extract_location(raw)
        pos = raw.get("position") if self._is_traccar_payload(raw) else {}
        ts_candidate = (
            (pos or {}).get("fixTime")
            or (pos or {}).get("deviceTime")
            or raw.get("timestamp")
            or raw.get("time")
            or raw.get("fix_time")
        )
        observed_at, ts_source, ts_suspect = DEFAULT_NORMALIZER.resolve_timestamp(
            ts_candidate, received_at, TimestampSource.DEVICE
        )
        raw_measurements = self._extract_measurements(raw, location, device)
        try:
            measurements, quality = DEFAULT_NORMALIZER.normalize(raw_measurements, device)
        except NormalizationError as exc:
            raise AdapterError(str(exc)) from exc

        quality["timestamp_source"] = ts_source
        attrs: dict[str, Any] = {}
        if ts_suspect:
            attrs["timestamp_quality"] = Quality.SUSPECT.value

        # POC-T12: identificação explícita de registros atrasados / buffer offline
        delay_s = round((parse_ts(received_at) - parse_ts(observed_at)).total_seconds(), 1)
        if raw.get("buffered") is True or delay_s > DELAYED_THRESHOLD_S:
            attrs["offline_buffered"] = True
            attrs["delayed"] = True
            attrs["delay_s"] = max(0.0, delay_s)

        if raw.get("can") or any(
            k in measurements for k in ("engine_rpm", "coolant_temperature_c", "engine_hours_h")
        ):
            attrs["can_j1939"] = True

        event = CanonicalEvent(
            event_id=self._event_id(imei, raw, observed_at, location),
            tenant_id=device.tenant_id,
            site_id=device.site_id,
            device_id=device.device_id,
            event_type=EventType.POSITION.value,
            observed_at=observed_at,
            received_at=received_at,
            measurements=measurements,
            quality=quality,
            location=location,
            source=self.source(
                transport=raw.get("transport", "traccar_webhook" if self._is_traccar_payload(raw) else "lte"),
                manufacturer=device.manufacturer or "generic",
            ),
            attributes=attrs or None,
        )
        return [event]


ADAPTER = register_adapter(TrackerAdapter(), "tracker_lte", "teltonika_json", "traccar_webhook")
