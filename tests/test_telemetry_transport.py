"""Only exact matching acknowledgements consume queued device readings."""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import aiohttp
import pytest

from custom_components.uteamup.telemetry_queue import QueuedEnvelope
from custom_components.uteamup.telemetry_transport import (
    TelemetryTransport, retry_after_seconds, retry_delay,
)

from .conftest import TENANT
from .test_telemetry_config import DEVICE_GUID, DEVICE_KEY, ENDPOINT, ENVIRONMENT_GUID


def setup_transport(status=202, overrides=None, headers=None, raw=None):
    event = QueuedEnvelope(str(uuid4()), TENANT, DEVICE_GUID, ENVIRONMENT_GUID, "mapping", b'{"exact":"bytes"}')
    ack = {"eventId": event.event_id, "tenantGuid": TENANT, "deviceGuid": DEVICE_GUID, "status": "accepted", "duplicate": False}
    ack.update(overrides or {})
    response = MagicMock(status=status, headers=headers or {})
    response.content.read = AsyncMock(return_value=raw if raw is not None else json.dumps(ack).encode())
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(return_value=response)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return TelemetryTransport(session, ENDPOINT), session, event


@pytest.mark.parametrize("ack", [{}, {"status": "duplicate", "duplicate": True}])
async def test_verified_tls_exact_bytes_narrow_key_and_matching_ack(ack):
    transport, session, event = setup_transport(overrides=ack)
    assert (await transport.async_send(event, DEVICE_KEY)).outcome == "accepted"
    session.post.assert_called_once()
    args, kwargs = session.post.call_args
    assert args == (f"{ENDPOINT}/v1/telemetry/batch",)
    assert kwargs["data"] is event.payload
    assert kwargs["headers"] == {"X-Device-Key": DEVICE_KEY, "Content-Type": "application/json"}
    assert kwargs["ssl"] is True
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("overrides", [
    {"eventId": str(uuid4())}, {"tenantGuid": str(uuid4())}, {"deviceGuid": str(uuid4())},
    {"status": "unknown"}, {"status": "duplicate", "duplicate": False}, {"duplicate": True},
])
async def test_mismatched_ack_is_ambiguous_and_retried(overrides):
    transport, _, event = setup_transport(overrides=overrides)
    assert (await transport.async_send(event, DEVICE_KEY)).outcome == "retrying"


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"x" * 16385])
async def test_malformed_or_oversized_ack_is_retried(raw):
    transport, _, event = setup_transport(raw=raw)
    assert (await transport.async_send(event, DEVICE_KEY)).outcome == "retrying"


@pytest.mark.parametrize("status,outcome", [
    (429, "retrying"), (503, "retrying"), (408, "retrying"),
    (401, "authentication"), (403, "authentication"), (400, "payload_rejected"),
    (413, "payload_rejected"), (422, "payload_rejected"), (404, "configuration"), (307, "configuration"),
])
async def test_transient_and_permanent_failures_are_distinct(status, outcome):
    transport, _, event = setup_transport(status=status, headers={"Retry-After": "7200"})
    result = await transport.async_send(event, DEVICE_KEY)
    assert result.outcome == outcome
    if outcome == "retrying":
        assert result.retry_after == 7200


async def test_network_exception_never_exposes_credential():
    transport, session, event = setup_transport()
    session.post.side_effect = aiohttp.ClientError(DEVICE_KEY)
    assert (await transport.async_send(event, DEVICE_KEY)).outcome == "retrying"


def test_retry_after_http_date_and_backoff_cap():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert retry_after_seconds(format_datetime(now + timedelta(hours=2)), now) == 7200
    assert retry_delay(10000, 7200) == 7200
    assert 4 <= retry_delay(0, 0) <= 6
    assert retry_after_seconds("nan", now) == 0
    assert retry_after_seconds("unparseable", now) == 0
