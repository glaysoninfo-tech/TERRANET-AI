"""Adaptador Modbus industrial e simulador de bancada (PIT-001 §2.1 P01, §14.4, E06)."""

from edge.adapters.modbus.adapter import ModbusAdapter
from edge.adapters.modbus.client import ModbusClient, PyModbusClient, SimulatedPumpPLC

__all__ = ["ModbusAdapter", "ModbusClient", "PyModbusClient", "SimulatedPumpPLC"]
