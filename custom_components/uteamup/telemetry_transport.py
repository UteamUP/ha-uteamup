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


def _matching_ack(body: bytes, envelope: QueuedEnvelope) -> bool:
    try:
        response = json.loads(body)
        return (
            response["status"] in {"accepted", "duplicate"}
            and response.get("duplicate") is (response["status"] == "duplicate")
            and UUID(response["eventId"]) == UUID(envelope.event_id)
            and UUID(response["tenantGuid"]) == UUID(envelope.tenant_guid)
            and UUID(response["deviceGuid"]) == UUID(envelope.device_guid)
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


class TelemetryTransport:
    """Never redirect credentials, disable certificate verification or fall back to CMMS."""

    def __init__(self, session: aiohttp.ClientSession, endpoint: str) -> None:
        self._session = session
        self._endpoint = endpoint

    async def async_send(self, envelope: QueuedEnvelope, device_key: str) -> DeliveryResult:
        try:
            async with self._session.post(
                f"{self._endpoint}/v1/telemetry/batch", data=envelope.payload,
                headers={"X-Device-Key": device_key, "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT, ssl=True, allow_redirects=False,
            ) as response:
                delay = retry_after_seconds(response.headers.get("Retry-After"), datetime.now(UTC))
                if response.status == 202:
                    body = await response.content.read(MAX_ACK_BYTES + 1)
                    if len(body) <= MAX_ACK_BYTES and _matching_ack(body, envelope):
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
