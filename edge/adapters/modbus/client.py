"""Clientes Modbus TCP e controlador industrial simulado em bancada (P01).

Conforme PIT-001 §14.4 e §25:
* Os intertravamentos de segurança física permanecem no controlador (CLP).
* Os testes iniciais ocorrem em bancada com controlador e carga simulados.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ModbusTransportError(IOError):
    """Falha de comunicação ou controlador indisponível."""


class ModbusClient(ABC):
    @abstractmethod
    def read_holding_registers(self, address: int, count: int, unit_id: int = 1) -> list[int]:
        ...

    @abstractmethod
    def read_coils(self, address: int, count: int, unit_id: int = 1) -> list[bool]:
        ...

    @abstractmethod
    def write_coil(self, address: int, value: bool, unit_id: int = 1) -> bool:
        ...

    @abstractmethod
    def write_register(self, address: int, value: int, unit_id: int = 1) -> bool:
        ...

    def close(self) -> None:
        return None


class PyModbusClient(ModbusClient):
    """Wrapper opcional sobre ``pymodbus`` para integração real via Modbus TCP."""

    def __init__(self, host: str, port: int = 502, timeout: float = 3.0):
        try:
            from pymodbus.client import ModbusTcpClient
        except ImportError as exc:
            raise ImportError(
                "Instale a dependência opcional 'edge' (pymodbus>=3.6) para Modbus TCP real"
            ) from exc
        self._client = ModbusTcpClient(host=host, port=port, timeout=timeout)
        self._lock = threading.Lock()

    def _ensure_connected(self) -> None:
        if not self._client.connected and not self._client.connect():
            raise ModbusTransportError("Não foi possível conectar ao controlador Modbus TCP")

    def read_holding_registers(self, address: int, count: int, unit_id: int = 1) -> list[int]:
        with self._lock:
            self._ensure_connected()
            resp = self._client.read_holding_registers(address=address, count=count, slave=unit_id)
            if resp.isError():
                raise ModbusTransportError(f"Erro Modbus na leitura de registradores: {resp}")
            return list(resp.registers)

    def read_coils(self, address: int, count: int, unit_id: int = 1) -> list[bool]:
        with self._lock:
            self._ensure_connected()
            resp = self._client.read_coils(address=address, count=count, slave=unit_id)
            if resp.isError():
                raise ModbusTransportError(f"Erro Modbus na leitura de bobinas: {resp}")
            return list(resp.bits[:count])

    def write_coil(self, address: int, value: bool, unit_id: int = 1) -> bool:
        with self._lock:
            self._ensure_connected()
            resp = self._client.write_coil(address=address, value=value, slave=unit_id)
            if resp.isError():
                raise ModbusTransportError(f"Erro Modbus na escrita de bobina: {resp}")
            return True

    def write_register(self, address: int, value: int, unit_id: int = 1) -> bool:
        with self._lock:
            self._ensure_connected()
            resp = self._client.write_register(address=address, value=value, slave=unit_id)
            if resp.isError():
                raise ModbusTransportError(f"Erro Modbus na escrita de registrador: {resp}")
            return True

    def close(self) -> None:
        with self._lock:
            self._client.close()


class PyModbusRtuClient(ModbusClient):
    """Cliente Modbus RTU sobre serial RS-485 (inversores, medidores e CLPs rurais)."""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        parity: str = "N",
        stopbits: int = 1,
        bytesize: int = 8,
        timeout: float = 2.0,
    ):
        try:
            from pymodbus.client import ModbusSerialClient
        except ImportError as exc:
            raise ImportError(
                "Instale a dependência opcional 'edge' (pymodbus>=3.6) para Modbus RTU/RS-485"
            ) from exc
        self._port = port
        self._client = ModbusSerialClient(
            port=port,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            bytesize=bytesize,
            timeout=timeout,
        )
        self._lock = threading.Lock()

    def _ensure_connected(self) -> None:
        if not self._client.connected and not self._client.connect():
            raise ModbusTransportError(f"Não foi possível abrir porta RS-485 {self._port}")

    def read_holding_registers(self, address: int, count: int, unit_id: int = 1) -> list[int]:
        with self._lock:
            self._ensure_connected()
            resp = self._client.read_holding_registers(address=address, count=count, slave=unit_id)
            if resp.isError():
                raise ModbusTransportError(f"Erro Modbus RTU na leitura de registradores: {resp}")
            return list(resp.registers)

    def read_coils(self, address: int, count: int, unit_id: int = 1) -> list[bool]:
        with self._lock:
            self._ensure_connected()
            resp = self._client.read_coils(address=address, count=count, slave=unit_id)
            if resp.isError():
                raise ModbusTransportError(f"Erro Modbus RTU na leitura de bobinas: {resp}")
            return list(resp.bits[:count])

    def write_coil(self, address: int, value: bool, unit_id: int = 1) -> bool:
        with self._lock:
            self._ensure_connected()
            resp = self._client.write_coil(address=address, value=value, slave=unit_id)
            if resp.isError():
                raise ModbusTransportError(f"Erro Modbus RTU na escrita de bobina: {resp}")
            return True

    def write_register(self, address: int, value: int, unit_id: int = 1) -> bool:
        with self._lock:
            self._ensure_connected()
            resp = self._client.write_register(address=address, value=value, slave=unit_id)
            if resp.isError():
                raise ModbusTransportError(f"Erro Modbus RTU na escrita de registrador: {resp}")
            return True

    def close(self) -> None:
        with self._lock:
            self._client.close()


def create_modbus_client(comm_profile: dict) -> ModbusClient:
    """Fábrica de cliente Modbus TCP ou RTU (RS-485) a partir do perfil de comunicação."""
    kind = str(comm_profile.get("type", "modbus_tcp")).lower()
    if kind == "modbus_rtu" or comm_profile.get("serial_port"):
        port = comm_profile.get("serial_port") or comm_profile.get("port")
        if not port:
            raise ValueError("Perfil Modbus RTU exige 'serial_port' (ex.: /dev/ttyUSB0)")
        return PyModbusRtuClient(
            port=str(port),
            baudrate=int(comm_profile.get("baudrate", 9600)),
            parity=str(comm_profile.get("parity", "N")),
            stopbits=int(comm_profile.get("stopbits", 1)),
            bytesize=int(comm_profile.get("bytesize", 8)),
            timeout=float(comm_profile.get("timeout", 2.0)),
        )
    host = comm_profile.get("host")
    if not host:
        raise ValueError("Perfil Modbus TCP exige 'host'")
    return PyModbusClient(
        host=str(host),
        port=int(comm_profile.get("port", 502)),
        timeout=float(comm_profile.get("timeout", 3.0)),
    )


@dataclass
class SimulatedPumpPLC(ModbusClient):
    """CLP simulado em bancada para a aplicação P01 (bomba supervisionada).

    Mapa de bobinas (coils):
        0: pump_running_state (R/W sujeito aos intertravamentos locais do CLP)
        1: remote_mode_enabled (permissivo: seletor físico em modo REMOTO)
        2: dry_run_interlock_ok (permissivo físico: há água na sucção)
        3: overload_fault (alarme térmico / sobrecarga; True impede partida)
        4: emergency_stop_active (botoeira de emergência; True impede partida)

    Mapa de registradores (holding registers):
        0: discharge_pressure_bar * 100  (ex.: 320 -> 3.20 bar)
        1: motor_current_a * 10          (ex.: 145 -> 14.5 A)
        2: flow_rate_m3h * 10            (ex.: 420 -> 42.0 m3/h)
        3: suction_level_m * 100         (ex.: 180 -> 1.80 m)
        4: motor_temperature_c * 10      (ex.: 480 -> 48.0 degC)
    """

    pump_running: bool = False
    remote_mode_enabled: bool = True
    dry_run_interlock_ok: bool = True
    overload_fault: bool = False
    emergency_stop_active: bool = False
    suction_level_m: float = 1.80
    discharge_pressure_bar: float = 0.0
    motor_current_a: float = 0.0
    flow_rate_m3h: float = 0.0
    motor_temperature_c: float = 32.0
    online: bool = True
    physical_actuations: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _check_online(self) -> None:
        if not self.online:
            raise ModbusTransportError("CLP offline / sem comunicação")

    def interlocks_ok(self) -> tuple[bool, str]:
        """Intertravamentos físicos residentes no CLP (não contornáveis por software)."""
        if self.emergency_stop_active:
            return False, "emergency_stop_active"
        if not self.remote_mode_enabled:
            return False, "selector_not_in_remote_mode"
        if not self.dry_run_interlock_ok or self.suction_level_m < 0.30:
            return False, "dry_run_protection_active"
        if self.overload_fault:
            return False, "motor_overload_fault"
        return True, "ok"

    def _sync_physics(self) -> None:
        if self.pump_running:
            self.discharge_pressure_bar = 3.20
            self.motor_current_a = 14.5
            self.flow_rate_m3h = 42.0
            self.motor_temperature_c = 48.0
        else:
            self.discharge_pressure_bar = 0.15
            self.motor_current_a = 0.0
            self.flow_rate_m3h = 0.0
            self.motor_temperature_c = 32.0

    def trigger_interlock_trip(self) -> None:
        """Simula desarme físico imediato no CLP (ex.: falta de água ou emergência)."""
        with self._lock:
            ok, _ = self.interlocks_ok()
            if not ok and self.pump_running:
                self.pump_running = False
                self._sync_physics()

    def read_coils(self, address: int, count: int, unit_id: int = 1) -> list[bool]:
        with self._lock:
            self._check_online()
            coils = [
                self.pump_running,
                self.remote_mode_enabled,
                self.dry_run_interlock_ok,
                self.overload_fault,
                self.emergency_stop_active,
            ]
            end = address + count
            if address < 0 or end > len(coils):
                raise ModbusTransportError(f"Endereço de bobina fora da faixa: {address}..{end}")
            return coils[address:end]

    def read_holding_registers(self, address: int, count: int, unit_id: int = 1) -> list[int]:
        with self._lock:
            self._check_online()
            self._sync_physics()
            regs = [
                int(round(self.discharge_pressure_bar * 100)),
                int(round(self.motor_current_a * 10)),
                int(round(self.flow_rate_m3h * 10)),
                int(round(self.suction_level_m * 100)),
                int(round(self.motor_temperature_c * 10)),
            ]
            end = address + count
            if address < 0 or end > len(regs):
                raise ModbusTransportError(
                    f"Endereço de registrador fora da faixa: {address}..{end}"
                )
            return regs[address:end]

    def write_coil(self, address: int, value: bool, unit_id: int = 1) -> bool:
        with self._lock:
            self._check_online()
            if address != 0:
                raise ModbusTransportError(
                    f"Bobina {address} protegida: somente bobina de comando 0 é gravável"
                )
            if value:
                ok, reason = self.interlocks_ok()
                if not ok:
                    # Intertravamento físico impede energização; bomba permanece desligada.
                    return False
            if self.pump_running != bool(value):
                self.pump_running = bool(value)
                self.physical_actuations += 1
                self._sync_physics()
            return True

    def write_register(self, address: int, value: int, unit_id: int = 1) -> bool:
        with self._lock:
            self._check_online()
            raise ModbusTransportError(f"Escrita direta no registrador {address} não permitida")
