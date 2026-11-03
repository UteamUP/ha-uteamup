"""Only exact matching acknowledgements consume queued device readings."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import aiohttp
import pytest

from custom_components.uteamup.telemetry_queue import QueuedEnvelope
from custom_components.uteamup.telemetry_transport import (
    ComponentDeliveryTarget, TelemetryTransport, retry_after_seconds, retry_delay,
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


def setup_component_transport(status=202, receipt=None, tail=b"", bridged=False):
    _, session, event = setup_transport(status=status)
    target = ComponentDeliveryTarget(TENANT, ENVIRONMENT_GUID, DEVICE_GUID, str(uuid4()), "isolated-component-source-key")
    scope = {
        "schemaVersion": 2, "eventId": event.event_id, "tenantGuid": target.tenant_guid,
        "sourceInstallationGuid": target.source_guid, "sourceGenerationGuid": target.generation_guid,
    }
    captured_at = "2030-01-01T00:00:00Z"
    observation = {
        "observationId": str(uuid4()),
        "identity": {"namespace": "homeassistant.device-registry", "stableHardwareId": uuid4().hex},
        "channel": {"namespace": "homeassistant.entity-registry", "stableId": uuid4().hex, "attribute": "state.on"},
        "observedAt": captured_at, "value": 1, "unit": "0/1", "observationKind": "current_state_sample",
        "provenance": {"quality": "measured"},
    }
    payload = (" \n" + json.dumps({**scope, "sampledAt": captured_at, "observations": [observation]}) + "\n").encode()
    event = replace(event, payload=payload)
    acknowledgement = {
        **scope, "receivedAt": "2030-01-01T00:00:00.1234567Z", "status": "accepted", "duplicate": False,
        "projectionState": "queued", **(receipt or {}),
    }
    response = session.post.return_value.__aenter__.return_value
    response.content.read.side_effect = [json.dumps(acknowledgement).encode(), tail, b""]
    transport = TelemetryTransport(session, ENDPOINT, edge_bridge_devices=(DEVICE_GUID,) if bridged else ())
    return transport, session, event, target


@pytest.mark.parametrize("receipt", [{}, {"status": "duplicate", "duplicate": True, "projectionState": "pendingReview"}])
async def test_v2_matches_exact_generation_and_leaves_the_queued_payload_unchanged(receipt):
    transport, session, event, target = setup_component_transport(receipt=receipt)
    assert (await transport.async_send_component(event, target)).outcome == "accepted"
    assert session.post.call_args.args == (f"{ENDPOINT}/v2/telemetry/batch",)
    assert session.post.call_args.kwargs["data"] is event.payload
    assert session.post.call_args.kwargs["headers"] == {"X-Device-Key": target.source_key, "Content-Type": "application/json"}
    assert session.post.call_args.kwargs["allow_redirects"] is False
    assert session.post.call_args.kwargs["ssl"] is True
    assert target.source_key not in repr(target)


@pytest.mark.parametrize("bad_receipt,tail", [
    ({"sourceGenerationGuid": str(uuid4())}, b""),
    ({"tenantGuid": str(uuid4())}, b""),
    ({"eventId": str(uuid4())}, b""),
    ({"schemaVersion": 1}, b""),
    ({"duplicate": True}, b""),
    ({"receivedAt": "2030-01-01T00:00:00"}, b""),
    ({}, b"another-response-fragment"),
    ({}, b" " * 16385),
])
async def test_v2_never_consumes_wrong_scope_or_partial_receipts(bad_receipt, tail):
    transport, session, event, target = setup_component_transport(receipt=bad_receipt, tail=tail)
    assert (await transport.async_send_component(event, target)).outcome == "retrying"
    session.post.assert_called_once()


async def test_v2_never_reroutes_a_queued_event_to_a_different_environment_or_generation():
    transport, session, event, target = setup_component_transport()
    assert (await transport.async_send_component(event, replace(target, environment_guid=str(uuid4())))).outcome == "configuration"
    assert (await transport.async_send_component(event, replace(target, generation_guid=str(uuid4())))).outcome == "mapping_changed"
    session.post.assert_not_called()


async def test_v2_redirect_does_not_fall_back_and_bridge_selected_source_cannot_send_directly():
    transport, session, event, target = setup_component_transport(status=307)
    assert (await transport.async_send_component(event, target)).outcome == "configuration"
    session.post.assert_called_once()
    transport, session, event, target = setup_component_transport(bridged=True)
    assert (await transport.async_send_component(event, target)).outcome == "configuration"
    session.post.assert_not_called()


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


async def test_edge_bridge_handoff_uses_separate_token_and_preserves_original_envelope():
    _, session, event = setup_transport(overrides={"stage": "edgebridge_intake"})
    body = {"eventId": event.event_id, "tenantGuid": TENANT, "deviceGuid": DEVICE_GUID,
            "observedAt": "2030-01-01T00:00:00+00:00", "points": [{"value": 0, "unit": "0/1"}]}
    event = replace(event, payload=json.dumps(body).encode())
    transport = TelemetryTransport(session, ENDPOINT, edge_bridge_endpoint="https://192.168.68.13:41080",
                                   edge_bridge_token="home-assistant-intake-token-1234567890",
                                   edge_bridge_devices=(DEVICE_GUID,))
    assert (await transport.async_send(event, "cloud-key-must-stay-on-out")).outcome == "accepted"
    args, kwargs = session.post.call_args
    assert args == ("https://192.168.68.13:41080/v1/intake/telemetry",)
    assert kwargs["headers"] == {"Authorization": "Bearer home-assistant-intake-token-1234567890", "Content-Type": "application/json"}
    assert json.loads(kwargs["data"]) == {"source": "homeassistant", "kind": "telemetry",
                                          "observedAt": body["observedAt"], "envelope": body}
    assert "cloud-key-must-stay-on-out" not in str(kwargs)
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("status,stage,expected", [(503, "edgebridge_intake", "retrying"),
    (202, "cloud", "retrying"), (401, "edgebridge_intake", "authentication"),
    (307, "edgebridge_intake", "configuration")])
async def test_edge_bridge_failure_never_falls_back_to_cloud(status, stage, expected):
    _, session, event = setup_transport(status=status, overrides={"stage": stage})
    event = replace(event, payload=b'{"observedAt":"2030-01-01T00:00:00Z"}')
    transport = TelemetryTransport(session, ENDPOINT, edge_bridge_endpoint="https://192.168.68.13:41080",
                                   edge_bridge_token="ha-intake-test-token-1234567890123456", edge_bridge_devices=(DEVICE_GUID,))
    assert (await transport.async_send(event, "unused-cloud-key")).outcome == expected
    session.post.assert_called_once()
    assert session.post.call_args.args[0].startswith("https://192.168.68.13:41080/")


async def test_unselected_devices_retain_their_reviewed_direct_route():
    _, session, event = setup_transport()
    transport = TelemetryTransport(session, ENDPOINT, edge_bridge_endpoint="https://192.168.68.13:41080",
                                   edge_bridge_token="ha-intake-test-token-1234567890123456", edge_bridge_devices=(str(uuid4()),))
    assert (await transport.async_send(event, DEVICE_KEY)).outcome == "accepted"
    assert session.post.call_args.args[0] == f"{ENDPOINT}/v1/telemetry/batch"


@pytest.mark.parametrize("endpoint", ["http://192.168.68.13:41080", "https://@192.168.68.13:41080"])
async def test_unsafe_bridge_configuration_sends_neither_lan_nor_cloud_credentials(endpoint):
    _, session, event = setup_transport()
    transport = TelemetryTransport(session, ENDPOINT, edge_bridge_endpoint=endpoint,
                                   edge_bridge_token="ha-intake-test-token-1234567890123456", edge_bridge_devices=(DEVICE_GUID,))
    assert (await transport.async_send(event, DEVICE_KEY)).outcome == "configuration"
    session.post.assert_not_called()
