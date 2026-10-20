"""Disk durability, immutable replay and explicit bounded queue recovery."""

from dataclasses import replace
import sqlite3
from unittest.mock import patch

import pytest

from custom_components.uteamup.telemetry_queue import QueuedEnvelope, TelemetryQueue

from .conftest import TENANT
from .test_telemetry_config import DEVICE_GUID, ENVIRONMENT_GUID


def envelope(event="event-1", device=DEVICE_GUID):
    return QueuedEnvelope(event, TENANT, device, ENVIRONMENT_GUID, "stable-mapping", b'{"eventId":"event-1","points":[{"pointId":"point-1"}]}')


async def test_restart_replays_exact_durable_bytes_and_ids(hass, tmp_path):
    path = str(tmp_path / "telemetry.db")
    first = TelemetryQueue(hass, path)
    original = envelope()
    assert await first.async_enqueue(original)
    second = TelemetryQueue(hass, path)
    assert await second.async_next(0) == original
    await second.async_defer(original, 60)
    third = TelemetryQueue(hass, path)
    assert await third.async_next(59) is None
    replayed = await third.async_next(60)
    assert replayed.payload == original.payload
    assert replayed.event_id == original.event_id
    assert replayed.attempts == 1
    await third.async_acknowledge(original.event_id)
    assert (await third.async_stats()).pending == 0


@pytest.mark.parametrize("limits", [{"max_count": 1}, {"max_bytes": len(envelope().payload)}])
async def test_overflow_retains_oldest_data_and_records_missed_capture(hass, tmp_path, limits):
    queue = TelemetryQueue(hass, str(tmp_path / "telemetry.db"), **limits)
    original = envelope()
    assert await queue.async_enqueue(original)
    assert not await queue.async_enqueue(replace(original, event_id="event-2"))
    assert await queue.async_next(0) == original
    assert (await queue.async_stats()).missed_samples == 1
    await queue.async_acknowledge(original.event_id)
    assert await queue.async_enqueue(replace(original, event_id="event-2"))


async def test_authentication_block_survives_restart_and_requires_rotated_key(hass, tmp_path):
    path = str(tmp_path / "telemetry.db")
    queue = TelemetryQueue(hass, path)
    original = envelope()
    await queue.async_configure({DEVICE_GUID: "old-key-hash"}, "")
    await queue.async_enqueue(original)
    await queue.async_block(original, "authentication")
    restarted = TelemetryQueue(hass, path)
    await restarted.async_configure({DEVICE_GUID: "old-key-hash"}, "")
    assert await restarted.async_next(100) is None
    assert (await restarted.async_stats()).quarantined == 1
    await restarted.async_configure({DEVICE_GUID: "new-key-hash"}, "")
    assert await restarted.async_next(100) == original


async def test_permanent_rejection_does_not_block_other_devices(hass, tmp_path):
    queue = TelemetryQueue(hass, str(tmp_path / "telemetry.db"))
    original = envelope()
    other = envelope("event-2", "other-device")
    await queue.async_enqueue(original)
    await queue.async_enqueue(other)
    await queue.async_block(original, "payload_rejected")
    assert await queue.async_next(0) == other
    stats = await queue.async_stats()
    assert (stats.pending, stats.quarantined, stats.blocked_devices) == (1, 1, 1)
    await queue.async_configure({DEVICE_GUID: "same-key-hash"}, "explicit-user-retry")
    assert await queue.async_next(0) == original
    # The same saved action is not repeatedly replayed on every Pi restart.
    await queue.async_block(original, "payload_rejected")
    await queue.async_configure({DEVICE_GUID: "same-key-hash"}, "explicit-user-retry")
    assert (await queue.async_blocked_devices())[DEVICE_GUID] == "payload_rejected"


async def test_each_executor_transaction_closes_its_connection(hass, tmp_path):
    opened, closed = [], []
    original_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=TrackedConnection)
        opened.append(connection)
        return connection

    queue = TelemetryQueue(hass, str(tmp_path / "telemetry.db"))
    with patch("custom_components.uteamup.telemetry_queue.sqlite3.connect", side_effect=connect):
        await queue.async_enqueue(envelope())
        await queue.async_stats()
        await queue.async_next(0)
    assert len(opened) == 3
    assert opened == closed
