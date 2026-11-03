"""The dedicated stamp's v1 contract, using only its narrow device credential."""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from uuid import UUID

import aiohttp

from .telemetry_queue import QueuedEnvelope
from .telemetry_config import edge_bridge_endpoint

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_ACK_BYTES = 16 * 1024


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str
    retry_after: float = 0


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


class TelemetryTransport:
    """Use the reviewed cloud or LAN route; never redirect credentials or fall back between routes."""

    def __init__(self, session: aiohttp.ClientSession, endpoint: str, *, edge_bridge_endpoint: str = "",
                 edge_bridge_token: str = "", edge_bridge_devices: tuple[str, ...] = ()) -> None:
        self._session = session
        self._endpoint = endpoint
        self._bridge_endpoint = edge_bridge_endpoint
        self._bridge_token = edge_bridge_token
        self._bridge_devices = frozenset(edge_bridge_devices)

    async def async_send(self, envelope: QueuedEnvelope, device_key: str) -> DeliveryResult:
        bridged = envelope.device_guid in self._bridge_devices
        endpoint = f"{self._endpoint}/v1/telemetry/batch"
        payload = envelope.payload
        headers = {"X-Device-Key": device_key, "Content-Type": "application/json"}
        if bridged:
            if not self._bridge_endpoint or not self._bridge_token:
                return DeliveryResult("configuration")
            try:
                edge_bridge_endpoint(self._bridge_endpoint)
            except ValueError:
                return DeliveryResult("configuration")
            try:
                body = json.loads(payload)
                payload = json.dumps({"source": "homeassistant", "kind": "telemetry",
                                      "observedAt": body["observedAt"], "envelope": body},
                                     separators=(",", ":"), allow_nan=False).encode()
            except (ValueError, KeyError, TypeError):
                return DeliveryResult("payload_rejected")
            endpoint = f"{self._bridge_endpoint}/v1/intake/telemetry"
            headers = {"Authorization": f"Bearer {self._bridge_token}", "Content-Type": "application/json"}
        try:
            async with self._session.post(
                endpoint, data=payload, headers=headers,
                timeout=REQUEST_TIMEOUT, ssl=True, allow_redirects=False,
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
