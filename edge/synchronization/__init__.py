"""Sincronização assíncrona e idempotente EDGE -> CORE (PIT-001 §12-13, E08)."""

from edge.synchronization.agent import SyncAgent, SyncResult
from edge.synchronization.client import HttpSyncTransport, SyncTransport, TransportError

__all__ = ["HttpSyncTransport", "SyncAgent", "SyncResult", "SyncTransport", "TransportError"]
