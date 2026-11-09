"""Delivery to the dedicated stamp with exact, versioned acknowledgements."""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from uuid import UUID

import aiohttp

from .telemetry_queue import QueuedEnvelope
from .telemetry_config import edge_bridge_certificate_fingerprint, edge_bridge_endpoint, ingestion_endpoint, public_guid, valid_device_key

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_ACK_BYTES = 16 * 1024
MAX_COMPONENT_PAYLOAD_BYTES = 1024 * 1024
# EdgeBridge refuses telemetry more than ten minutes from its clock; one minute is left for clock skew.
EDGE_BRIDGE_MAX_AGE = timedelta(minutes=9)


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str
    retry_after: float = 0


@dataclass(frozen=True)
class ComponentDeliveryTarget:
    tenant_guid: str
    environment_guid: str
    source_guid: str
    generation_guid: str
    source_key: str = field(repr=False)


def retry_after_seconds(value: str | None, now: datetime) -> float:
    """Honor both HTTP Retry-After forms, including a delay longer than normal backoff."""
    if not value:
        return 0
    try:
        delay = float(value)
    except ValueError:
        try:
            requested = parsedate_to_datetime(value)
            delay = (requested.replace(tzinfo=requested.tzinfo or UTC) - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


def retry_delay(attempts: int, requested_delay: float) -> float:
    return max(requested_delay, min(3600, 5 * 2 ** min(attempts, 10)) * random.uniform(0.8, 1.2))


def _matching_ack(body: bytes, envelope: QueuedEnvelope, stage: str | None = None) -> bool:
    try:
        response = json.loads(body)
        return (
            response["status"] in {"accepted", "duplicate"}
            and response.get("duplicate") is (response["status"] == "duplicate")
            and UUID(response["eventId"]) == UUID(envelope.event_id)
            and UUID(response["tenantGuid"]) == UUID(envelope.tenant_guid)
            and UUID(response["deviceGuid"]) == UUID(envelope.device_guid)
            and (stage is None or response.get("stage") == stage)
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate_field")
    return result


def _same_component_scope(body, envelope: QueuedEnvelope, target: ComponentDeliveryTarget) -> bool:
    return (type(body["schemaVersion"]) is int and body["schemaVersion"] == 2
            and public_guid(body["eventId"]) == public_guid(envelope.event_id)
            and public_guid(body["tenantGuid"]) == public_guid(target.tenant_guid) == public_guid(envelope.tenant_guid)
            and public_guid(body["sourceInstallationGuid"]) == public_guid(target.source_guid) == public_guid(envelope.device_guid)
            and public_guid(body["sourceGenerationGuid"]) == public_guid(target.generation_guid))


def _matching_component_ack(payload: bytes, envelope: QueuedEnvelope, target: ComponentDeliveryTarget) -> bool:
    try:
        body = json.loads(payload, object_pairs_hook=_strict_object)
        if set(body) != {"schemaVersion", "eventId", "tenantGuid", "sourceInstallationGuid", "sourceGenerationGuid",
                         "receivedAt", "status", "duplicate", "projectionState"}:
            return False
        return (_same_component_scope(body, envelope, target)
                and body["status"] in {"accepted", "duplicate"}
                and body["duplicate"] is (body["status"] == "duplicate")
                and body["projectionState"] in {"queued", "pendingReview", "projected", "quarantined", "observedOnly"}
                and datetime.fromisoformat(body["receivedAt"]).utcoffset() == timedelta(0))
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return False


async def _complete_ack(content: aiohttp.StreamReader) -> bytes | None:
    body = bytearray()
    while len(body) <= MAX_ACK_BYTES:
        chunk = await content.read(MAX_ACK_BYTES + 1 - len(body))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
    return None


class TelemetryTransport:
    """Use the reviewed cloud or LAN route; never redirect credentials or fall back between routes."""

    def __init__(self, session: aiohttp.ClientSession, endpoint: str, *, edge_bridge_endpoint: str = "",
                 edge_bridge_token: str = "", edge_bridge_devices: tuple[str, ...] = (),
                 edge_bridge_certificate_fingerprint: str = "") -> None:
        self._session = session
        self._endpoint = endpoint
        self._bridge_endpoint = edge_bridge_endpoint
        self._bridge_token = edge_bridge_token
        self._bridge_devices = frozenset(edge_bridge_devices)
        self._bridge_certificate_fingerprint = edge_bridge_certificate_fingerprint

    async def async_send(self, envelope: QueuedEnvelope, device_key: str) -> DeliveryResult:
        bridged = envelope.device_guid in self._bridge_devices
        endpoint = f"{self._endpoint}/v1/telemetry/batch"
        payload = envelope.payload
        headers = {"X-Device-Key": device_key, "Content-Type": "application/json"}
        tls_verification: bool | aiohttp.Fingerprint = True
        if bridged:
            if not self._bridge_endpoint or not self._bridge_token:
                return DeliveryResult("configuration")
            try:
                edge_bridge_endpoint(self._bridge_endpoint)
                fingerprint = edge_bridge_certificate_fingerprint(self._bridge_certificate_fingerprint)
                if fingerprint:
                    tls_verification = aiohttp.Fingerprint(bytes.fromhex(fingerprint))
            except ValueError:
                return DeliveryResult("configuration")
            try:
                body = json.loads(payload)
                stale = datetime.now(UTC) - datetime.fromisoformat(body["observedAt"]) > EDGE_BRIDGE_MAX_AGE
                payload = json.dumps({"source": "homeassistant", "kind": "telemetry",
                                      "observedAt": body["observedAt"], "envelope": body},
                                     separators=(",", ":"), allow_nan=False).encode()
            except (ValueError, KeyError, TypeError):
                return DeliveryResult("payload_rejected")
            if stale:
                # A replay the bridge would refuse must not be sent, or its 400 blocks the whole device.
                return DeliveryResult("stale")
            endpoint = f"{self._bridge_endpoint}/v1/intake/telemetry"
            headers = {"Authorization": f"Bearer {self._bridge_token}", "Content-Type": "application/json"}
        try:
            async with self._session.post(
                endpoint, data=payload, headers=headers,
                timeout=REQUEST_TIMEOUT, ssl=tls_verification, allow_redirects=False,
            ) as response:
                delay = retry_after_seconds(response.headers.get("Retry-After"), datetime.now(UTC))
                if response.status == 202:
                    body = await response.content.read(MAX_ACK_BYTES + 1)
                    if len(body) <= MAX_ACK_BYTES and _matching_ack(body, envelope, "edgebridge_intake" if bridged else None):
                        return DeliveryResult("accepted")
                    # Ambiguous acknowledgements must replay the same event, never a new UUID.
                    return DeliveryResult("retrying", delay)
                if response.status in {408, 425, 429} or response.status >= 500:
                    return DeliveryResult("retrying", delay)
                if response.status in {401, 403}:
                    return DeliveryResult("authentication")
                if 300 <= response.status < 400 or response.status in {404, 405, 410}:
                    return DeliveryResult("configuration")
                return DeliveryResult("payload_rejected")
        except (aiohttp.ClientError, TimeoutError):
            # Neither response bodies nor exception strings are safe to log with device credentials.
            return DeliveryResult("retrying")

    async def async_send_component(self, envelope: QueuedEnvelope, target: ComponentDeliveryTarget) -> DeliveryResult:
        """Only an exact direct V2 receipt can consume a queued component event."""
        try:
            endpoint = ingestion_endpoint(self._endpoint)
            if (public_guid(envelope.environment_guid) != public_guid(target.environment_guid)
                    or public_guid(target.source_guid) in self._bridge_devices
                    or not valid_device_key(target.source_key)):
                return DeliveryResult("configuration")
        except (TypeError, ValueError):
            return DeliveryResult("configuration")
        if not 0 < len(envelope.payload) <= MAX_COMPONENT_PAYLOAD_BYTES:
            return DeliveryResult("payload_rejected")
        try:
            body = json.loads(envelope.payload, object_pairs_hook=_strict_object)
            if not _same_component_scope(body, envelope, target):
                return DeliveryResult("mapping_changed")
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return DeliveryResult("payload_rejected")

        try:
            async with self._session.post(
                f"{endpoint}/v2/telemetry/batch", data=envelope.payload,
                headers={"X-Device-Key": target.source_key, "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT, ssl=True, allow_redirects=False,
            ) as response:
                delay = retry_after_seconds(response.headers.get("Retry-After"), datetime.now(UTC))
                if response.status == 202:
                    receipt = await _complete_ack(response.content)
                    if receipt is not None and _matching_component_ack(receipt, envelope, target):
                        return DeliveryResult("accepted")
                    return DeliveryResult("retrying", delay)
                if response.status in {408, 425, 429} or response.status >= 500:
                    return DeliveryResult("retrying", delay)
                if response.status in {401, 403}:
                    return DeliveryResult("authentication")
                if 300 <= response.status < 400 or response.status in {404, 405, 410}:
                    return DeliveryResult("configuration")
                return DeliveryResult("payload_rejected")
        except (aiohttp.ClientError, TimeoutError):
            return DeliveryResult("retrying")
