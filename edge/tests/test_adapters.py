"""Testes unitários dos adaptadores LoRaWAN, Modbus e TRACK (E04, E05, E06)."""

from contracts.devices import Device
from edge.adapters.lorawan.chirpstack import ChirpStackAdapter
from edge.adapters.lorawan.decoders import encode_level_v1
from edge.adapters.modbus.adapter import ModbusAdapter
from edge.adapters.modbus.client import SimulatedPumpPLC
from edge.adapters.tracker.adapter import TrackerAdapter


def test_two_different_lorawan_manufacturers_produce_same_canonical_model():
    """Critério §33.1: dois dispositivos de fabricantes diferentes -> mesmo modelo canônico."""
    adapter = ChirpStackAdapter()

    # Fabricante A: envia nível em cm no campo object.nivel_cm
    dev_a = Device(
        device_id="NODE-HYDRO-A",
        tenant_id="CLIENTE-001",
        site_id="FAZENDA-001",
        category="NODE-S",
        source_type="lorawan",
        source_identifier="0102030405060708",
        adapter_id="lorawan_chirpstack",
        manufacturer="FabricanteA",
        model="HydroSense-100",
        operational_status="ACTIVE",
        metadata={
            "decoder": "chirpstack_object",
            "field_map": {
                "water_level_m": {"field": "nivel_cm", "unit": "cm"},
                "battery_voltage_v": {"field": "vbat_mv", "unit": "mV"},
            },
            "ranges": {"water_level_m": [0, 20]},
        },
    )
    raw_a = {
        "deduplicationId": "dedup-a-1",
        "time": "2026-11-22T12:00:00Z",
        "deviceInfo": {"devEui": "0102030405060708"},
        "object": {"nivel_cm": 245, "vbat_mv": 12700},
        "rxInfo": [{"rssi": -88, "snr": 7.5}],
    }

    # Fabricante B: envia payload binário (mm + mV)
    dev_b = Device(
        device_id="NODE-HYDRO-B",
        tenant_id="CLIENTE-001",
        site_id="FAZENDA-001",
        category="NODE-S",
        source_type="lorawan",
        source_identifier="aabbccddeeff0011",
        adapter_id="lorawan_chirpstack",
        manufacturer="FabricanteB",
        model="RiverProbe-Bin",
        operational_status="ACTIVE",
        metadata={"decoder": "terranet_level_v1", "ranges": {"water_level_m": [0, 20]}},
    )
    raw_b = {
        "deduplicationId": "dedup-b-1",
        "time": "2026-11-22T12:00:00Z",
        "deviceInfo": {"devEui": "AABBCCDDEEFF0011"},
        "data": encode_level_v1(level_mm=2450, battery_mv=12700),
        "rxInfo": [{"rssi": -92, "snr": 5.0}],
    }

    ev_a = adapter.to_events(raw_a, dev_a, received_at="2026-11-22T12:00:01Z")[0]
    ev_b = adapter.to_events(raw_b, dev_b, received_at="2026-11-22T12:00:01Z")[0]

    assert ev_a.measurements == ev_b.measurements == {
        "water_level_m": 2.45,
        "battery_voltage_v": 12.7,
    }
    assert ev_a.quality["water_level_m"] == ev_b.quality["water_level_m"] == "valid"
    ev_a.validate()
    ev_b.validate()


def test_modbus_adapter_polling_and_bench_plc():
    plc = SimulatedPumpPLC(pump_running=False, suction_level_m=1.80)
    dev = Device(
        device_id="PUMP-001",
        tenant_id="CLIENTE-001",
        site_id="FAZENDA-001",
        category="NODE-C",
        source_type="modbus",
        source_identifier="plc-p01-01",
        adapter_id="modbus_plc",
        operational_status="ACTIVE",
        communication_profile={"type": "modbus", "unit_id": 1},
        metadata={"allowed_operations": ["start_pump", "stop_pump"]},
    )
    adapter = ModbusAdapter()
    ev = adapter.poll(dev, plc, poll_id="poll-1", observed_at="2026-11-22T12:05:00Z")
    ev.validate()
    assert ev.measurements["pump_running_state"] is False
    assert ev.measurements["suction_level_m"] == 1.8
    assert ev.measurements["dry_run_ok_state"] is True


def test_tracker_adapter_gnss_and_idempotent_id():
    dev = Device(
        device_id="TRACK-VEH-001",
        tenant_id="CLIENTE-001",
        site_id="FAZENDA-001",
        category="TRACK-B",
        source_type="tracker",
        source_identifier="356789012345678",
        adapter_id="tracker_gnss_lte",
        manufacturer="Teltonika",
        model="FMC130",
        operational_status="ACTIVE",
    )
    raw = {
        "imei": "356789012345678",
        "timestamp": "2026-11-22T12:10:00Z",
        "gnss": {"lat": -23.55052, "lon": -46.633308, "speed_kmh": 42.5, "heading_deg": 180, "satellites": 11},
        "io": {"battery_mv": 4100, "ignition": True, "odometer_m": 152300},
    }
    adapter = TrackerAdapter()
    ev1 = adapter.to_events(raw, dev, received_at="2026-11-22T12:10:01Z")[0]
    ev2 = adapter.to_events(raw, dev, received_at="2026-11-22T12:10:05Z")[0]
    ev1.validate()
    assert ev1.event_id == ev2.event_id  # chave estável (§9.2)
    assert ev1.event_type == "position"
    assert ev1.location["lat"] == -23.55052
    assert ev1.measurements["battery_voltage_v"] == 4.1
    assert ev1.measurements["odometer_km"] == 152.3
    assert ev1.measurements["ignition_state"] is True
