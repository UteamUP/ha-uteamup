"""A bounded, restart-safe spool. Payloads are immutable and contain no credentials."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import TELEMETRY_QUEUE_MAX_BYTES, TELEMETRY_QUEUE_MAX_COUNT


@dataclass(frozen=True)
class QueuedEnvelope:
    event_id: str
    tenant_guid: str
    device_guid: str
    environment_guid: str
    mapping_fingerprint: str
    payload: bytes
    attempts: int = 0


@dataclass(frozen=True)
class QueueStats:
    pending: int = 0
    quarantined: int = 0
    bytes: int = 0
    blocked_devices: int = 0
    missed_samples: int = 0
    retrying_devices: int = 0


class TelemetryQueue:
    """Serialize durable transactions in HA's executor, never block its event loop."""

    def __init__(
        self, hass: HomeAssistant, path: str, *,
        max_count: int = TELEMETRY_QUEUE_MAX_COUNT, max_bytes: int = TELEMETRY_QUEUE_MAX_BYTES,
    ) -> None:
        self._hass = hass
        self._path = Path(path)
        self._max_count = max_count
        self._max_bytes = max_bytes
        self._lock = asyncio.Lock()

    def _transaction(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        # A connection is used only in the executor thread that opened it.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path, timeout=10)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE, tenant_guid TEXT NOT NULL,
                    device_guid TEXT NOT NULL, environment_guid TEXT NOT NULL,
                    mapping_fingerprint TEXT NOT NULL, payload BLOB NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, quarantine_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS devices (
                    device_guid TEXT PRIMARY KEY, blocked_reason TEXT,
                    credential_hash TEXT, next_attempt REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            return operation(connection)

    async def _async_transaction(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        async with self._lock:
            # Shield a disk commit from task cancellation. Unload waits for it before returning.
            operation_task = asyncio.ensure_future(self._hass.async_add_executor_job(self._transaction, operation))
            try:
                return await asyncio.shield(operation_task)
            except asyncio.CancelledError:
                await operation_task
                raise

    async def async_configure(self, credential_hashes: dict[str, str], retry_nonce: str) -> None:
        """Only rotated keys or explicit user recovery release permanently blocked devices."""
        def configure(connection: sqlite3.Connection) -> None:
            previous = connection.execute("SELECT value FROM metadata WHERE key='retry_nonce'").fetchone()
            retry = bool(retry_nonce) and (previous is None or previous[0] != retry_nonce)
            for device_guid, credential_hash in credential_hashes.items():
                row = connection.execute("SELECT * FROM devices WHERE device_guid=?", (device_guid,)).fetchone()
                rotated = row and row["blocked_reason"] == "authentication" and row["credential_hash"] != credential_hash
                if retry or rotated:
                    connection.execute(
                        "UPDATE events SET quarantine_reason=NULL WHERE device_guid=?", (device_guid,),
                    )
                    connection.execute(
                        "UPDATE devices SET blocked_reason=NULL, next_attempt=0 WHERE device_guid=?", (device_guid,),
                    )
                connection.execute(
                    "INSERT INTO devices(device_guid, credential_hash) VALUES(?, ?) "
                    "ON CONFLICT(device_guid) DO UPDATE SET credential_hash=excluded.credential_hash",
                    (device_guid, credential_hash),
                )
            connection.execute("INSERT OR REPLACE INTO metadata VALUES('retry_nonce', ?)", (retry_nonce,))
        await self._async_transaction(configure)

    async def async_enqueue(self, envelope: QueuedEnvelope) -> bool:
        """Commit exact bytes before the caller may transmit them; never evict old data."""
        def enqueue(connection: sqlite3.Connection) -> bool:
            count, size = connection.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM events").fetchone()
            if count >= self._max_count or size + len(envelope.payload) > self._max_bytes:
                connection.execute(
                    "INSERT INTO metadata VALUES('missed_samples', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1",
                )
                return False
            connection.execute(
                "INSERT INTO events(event_id, tenant_guid, device_guid, environment_guid, mapping_fingerprint, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (envelope.event_id, envelope.tenant_guid, envelope.device_guid, envelope.environment_guid,
                 envelope.mapping_fingerprint, envelope.payload),
            )
            return True
        return await self._async_transaction(enqueue)

    async def async_next(self, now: float) -> QueuedEnvelope | None:
        def next_envelope(connection: sqlite3.Connection) -> QueuedEnvelope | None:
            row = connection.execute(
                "SELECT e.* FROM events e LEFT JOIN devices d ON d.device_guid=e.device_guid "
                "WHERE e.quarantine_reason IS NULL AND d.blocked_reason IS NULL "
                "AND COALESCE(d.next_attempt, 0)<=? ORDER BY e.ordinal LIMIT 1", (now,),
            ).fetchone()
            return None if row is None else QueuedEnvelope(**{
                name: row[name] for name in QueuedEnvelope.__dataclass_fields__
            })
        return await self._async_transaction(next_envelope)

    async def async_acknowledge(self, event_id: str) -> None:
        def acknowledge(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE devices SET next_attempt=0 WHERE device_guid=(SELECT device_guid FROM events WHERE event_id=?)",
                (event_id,),
            )
            connection.execute("DELETE FROM events WHERE event_id=?", (event_id,))
        await self._async_transaction(acknowledge)

    async def async_defer(self, envelope: QueuedEnvelope, next_attempt: float) -> None:
        def defer(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE events SET attempts=attempts+1 WHERE event_id=?", (envelope.event_id,))
            connection.execute(
                "INSERT INTO devices(device_guid, next_attempt) VALUES(?, ?) "
                "ON CONFLICT(device_guid) DO UPDATE SET next_attempt=excluded.next_attempt",
                (envelope.device_guid, next_attempt),
            )
        await self._async_transaction(defer)

    async def async_block(self, envelope: QueuedEnvelope, reason: str) -> None:
        """Keep rejected data for review, and stop adding data for its affected device."""
        def block(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE events SET quarantine_reason=? WHERE event_id=?", (reason, envelope.event_id))
            connection.execute(
                "INSERT INTO devices(device_guid, blocked_reason) VALUES(?, ?) "
                "ON CONFLICT(device_guid) DO UPDATE SET blocked_reason=excluded.blocked_reason",
                (envelope.device_guid, reason),
            )
        await self._async_transaction(block)

    async def async_blocked_devices(self) -> dict[str, str]:
        return await self._async_transaction(lambda connection: {
            row["device_guid"]: row["blocked_reason"]
            for row in connection.execute("SELECT device_guid, blocked_reason FROM devices WHERE blocked_reason IS NOT NULL")
        })

    async def async_stats(self) -> QueueStats:
        def stats(connection: sqlite3.Connection) -> QueueStats:
            row = connection.execute(
                "SELECT COUNT(*) FILTER (WHERE quarantine_reason IS NULL), "
                "COUNT(*) FILTER (WHERE quarantine_reason IS NOT NULL), COALESCE(SUM(LENGTH(payload)), 0) FROM events"
            ).fetchone()
            blocked = connection.execute("SELECT COUNT(*) FROM devices WHERE blocked_reason IS NOT NULL").fetchone()[0]
            missed = connection.execute("SELECT value FROM metadata WHERE key='missed_samples'").fetchone()
            retrying = connection.execute("SELECT COUNT(*) FROM devices WHERE next_attempt>0 AND blocked_reason IS NULL").fetchone()[0]
            return QueueStats(row[0], row[1], row[2], blocked, int(missed[0]) if missed else 0, retrying)
        return await self._async_transaction(stats)
