"""Optional dedicated IoT export; source availability is independent of delivery health."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import DOMAIN, TELEMETRY_KEEPALIVE_SECONDS, TELEMETRY_NETWORK_KEEPALIVE_SECONDS, TELEMETRY_SAMPLE_SECONDS
from .api import UteamupClient, UteamupError
from .deco_clients import MAX_SOURCE_AGE, deco_clients, network_client_observations, resolved_client_mappings
from .telemetry_config import (
    NETWORK_METRICS, NetworkTelemetryMapping, TelemetryMapping, TelemetrySettings,
    device_has_mac, device_reference_matches, measured_wiz_sources_match, network_unit_matches, source_belongs_to_device,
    source_unit, validate_deco_tracker,
    normalize_mac,
)
from .telemetry_queue import QueuedEnvelope, QueueStats, TelemetryQueue
from .telemetry_transport import TelemetryTransport, retry_delay
from .measurements import sample_measurements

if TYPE_CHECKING:
    from .coordinator import UteamupConfigEntry

_LOGGER = logging.getLogger(__name__)
MAX_ESTIMATE_PARAMETER_WATTS = 1000
CUMULATIVE_UNITS = frozenset({"Wh", "kWh"})


def content_fingerprint(payload: bytes) -> str:
    """What a sample says, ignoring identities, capture times and energy counters that always advance."""
    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: strip(item) for key, item in value.items()
                if key not in {"eventId", "pointId"} and not key.endswith(("observedAt", "ObservedAt", "UpdatedAt"))
            }
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    body = json.loads(payload)
    for point in body.get("points", []):
        if point.get("unit") in CUMULATIVE_UNITS:
            point.pop("value", None)
    return json.dumps(strip(body), sort_keys=True, separators=(",", ":"))


def _estimate_parameters(attributes: Mapping[str, Any]) -> dict[str, float]:
    """Retain adjustable assumptions without copying arbitrary (possibly secret) attributes."""
    parameters = {}
    for attribute, name in (
        ("assumed_full_power_w", "assumedFullPowerW"),
        ("assumed_standby_power_w", "assumedStandbyPowerW"),
    ):
        value = attributes.get(attribute)
        if (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= MAX_ESTIMATE_PARAMETER_WATTS and math.isfinite(value)
        ):
            parameters[name] = value
    return parameters


class SourceUnavailable(ValueError):
    """An observation cannot truthfully represent its configured physical device."""


def sample_envelope(
    hass: HomeAssistant, settings: TelemetrySettings, mapping: TelemetryMapping, now: datetime,
) -> QueuedEnvelope:
    """Observe current HA sources; never estimate, integrate unknown gaps or create a heartbeat."""
    registry = er.async_get(hass)
    source = registry.async_get(mapping.source_registry_id)
    power = registry.async_get(mapping.power_registry_id)
    energy = registry.async_get(mapping.energy_registry_id)
    if any(item is None or item.disabled for item in (source, power, energy)):
        raise SourceUnavailable("source_missing")
    if not device_reference_matches(hass, mapping.ha_device_id, source.device_id):
        raise SourceUnavailable("device_mismatch")
    physical = dr.async_get(hass).async_get(source.device_id)
    if not device_has_mac(physical, mapping.mac_address):
        raise SourceUnavailable("device_mismatch")
    states = [hass.states.get(item.entity_id) for item in (source, power, energy)]
    if any(
        state is None or state.state in {STATE_UNAVAILABLE, STATE_UNKNOWN} or state.attributes.get("restored")
        for state in states
    ) or states[0].state not in {"on", "off"}:
        raise SourceUnavailable("unavailable")
    if mapping.power_basis == "measured" and not measured_wiz_sources_match(hass, source, (power, energy)):
        raise SourceUnavailable("measured_source_required")
    values: list[float] = []
    for entity, state, unit in ((power, states[1], "W"), (energy, states[2], "kWh")):
        if source_unit(entity, state) != unit:
            raise SourceUnavailable("unit_mismatch")
        if not source_belongs_to_device(hass, entity.entity_id, source.device_id, source.id):
            raise SourceUnavailable("device_mismatch")
        profile = state.attributes.get("estimate_profile")
        if mapping.power_basis == "estimated" and profile and profile != mapping.estimate_profile:
            raise SourceUnavailable("profile_mismatch")
        try:
            value = float(state.state)
        except (TypeError, ValueError) as err:
            raise SourceUnavailable("invalid_value") from err
        if not math.isfinite(value) or not 0 <= value <= 1e15:
            raise SourceUnavailable("invalid_value")
        values.append(value)
    timestamp = now.isoformat()
    event_id = str(uuid4())
    payload = json.dumps({
        "eventId": event_id, "tenantGuid": settings.tenant_guid, "deviceGuid": mapping.device_guid,
        "observedAt": timestamp,
        "metadata": {
            "source": "homeassistant", "powerBasis": mapping.power_basis,
            **({"estimateProfile": mapping.estimate_profile, "estimateParameters": _estimate_parameters(states[1].attributes)}
               if mapping.power_basis == "estimated" else {}),
            "sourceRegistryId": source.id, "powerSourceRegistryId": power.id, "energySourceRegistryId": energy.id,
            "powerStateUpdatedAt": states[1].last_updated.isoformat(),
            "energyStateUpdatedAt": states[2].last_updated.isoformat(),
        },
        "points": [
            {"pointId": str(uuid4()), "assetGuid": mapping.asset_guid, "attributeDefinitionGuid": attribute,
             "value": value, "observedAt": timestamp, "unit": unit, "quality": mapping.power_basis}
            for attribute, value, unit in (
                (mapping.power_attribute_guid, values[0], "W"), (mapping.energy_attribute_guid, values[1], "kWh"),
            )
        ],
    }, separators=(",", ":"), allow_nan=False).encode()
    return QueuedEnvelope(
        event_id, settings.tenant_guid, mapping.device_guid, settings.environment_guid, mapping.fingerprint, payload,
    )


def sample_network_envelope(
    hass: HomeAssistant, settings: TelemetrySettings, mapping: NetworkTelemetryMapping, now: datetime,
    client_mapping: NetworkTelemetryMapping | None = None,
) -> QueuedEnvelope:
    """Capture router traffic, client count and online states without inferring availability."""
    registry = er.async_get(hass)
    tracker = registry.async_get(mapping.tracker_registry_id)
    entities = {
        name: registry.async_get(getattr(mapping, f"{name}_registry_id"))
        for name, _, _ in NETWORK_METRICS
    }
    if tracker is None or tracker.device_id != mapping.ha_device_id or any(
        entity is None or entity.disabled or entity.device_id != tracker.device_id
        for entity in entities.values()
    ):
        raise SourceUnavailable("device_mismatch")
    try:
        tracker_state = validate_deco_tracker(hass, tracker, mapping.mac_address)
    except ValueError as err:
        raise SourceUnavailable(str(err)) from err
    states = {name: hass.states.get(entity.entity_id) for name, entity in entities.items()}
    if any(
        state is None or state.state in {STATE_UNAVAILABLE, STATE_UNKNOWN} or state.attributes.get("restored")
        for state in states.values()
    ):
        raise SourceUnavailable("unavailable")
    if now - tracker_state.last_reported > MAX_SOURCE_AGE or any(
        now - state.last_reported > MAX_SOURCE_AGE for state in states.values()
    ):
        raise SourceUnavailable("stale")
    values: dict[str, float] = {}
    for name, domain, unit in NETWORK_METRICS:
        entity = entities[name]
        state = states[name]
        if domain == "binary_sensor":
            if state.state not in {"on", "off"}:
                raise SourceUnavailable("invalid_value")
            values[name] = 1.0 if state.state == "on" else 0.0
            continue
        if not network_unit_matches(entity, state, domain, unit):
            raise SourceUnavailable("unit_mismatch")
        try:
            value = float(state.state)
        except (TypeError, ValueError) as err:
            raise SourceUnavailable("invalid_value") from err
        if not math.isfinite(value) or not 0 <= value <= 1e15:
            raise SourceUnavailable("invalid_value")
        values[name] = value
    timestamp = now.isoformat()
    event_id = str(uuid4())
    clients, client_points = network_client_observations(hass, client_mapping or mapping, now)
    payload = json.dumps({
        "eventId": event_id,
        "tenantGuid": settings.tenant_guid,
        "deviceGuid": mapping.device_guid,
        "observedAt": timestamp,
        "metadata": {
            "source": "homeassistant",
            "telemetryKind": "network",
            "trackerRegistryId": tracker.id,
            "sourceRegistryIds": {name: entities[name].id for name, _, _ in NETWORK_METRICS},
            "sourceStateUpdatedAt": {
                name: states[name].last_updated.isoformat() for name, _, _ in NETWORK_METRICS
            },
            "networkClients": clients,
            "clientCountScope": "selected_source",
            "totalConnectedClients": sum(client["connected"] is True for client in clients)
                if clients and all(client["connected"] is not None for client in clients) else None,
        },
        "points": [
            {
                "pointId": str(uuid4()),
                "assetGuid": mapping.asset_guid,
                "attributeDefinitionGuid": getattr(mapping, f"{name}_attribute_guid"),
                "value": values[name],
                "observedAt": states[name].last_reported.isoformat(),
                "unit": unit,
                "quality": "measured",
            }
            for name, _, unit in NETWORK_METRICS
        ] + client_points,
    }, separators=(",", ":"), allow_nan=False).encode()
    return QueuedEnvelope(
        event_id, settings.tenant_guid, mapping.device_guid, settings.environment_guid, mapping.fingerprint, payload,
    )


@dataclass
class TelemetryHealth:
    sender: str = "starting"
    sources: dict[str, str] = field(default_factory=dict)
    client_links: dict[str, str] = field(default_factory=dict)
    queue: QueueStats = field(default_factory=QueueStats)
    last_success: datetime | None = None


class UteamupTelemetry:
    """Two cancellable tasks share a durable spool but never depend on CMMS polling."""

    def __init__(self, hass: HomeAssistant, entry: UteamupConfigEntry, settings: TelemetrySettings,
                 client: UteamupClient | None = None) -> None:
        self.hass = hass
        self.entry = entry
        self.settings = settings
        self._client = client
        self._client_link_attempts: dict[tuple[str, str], float] = {}
        self._client_devices: list[dict[str, Any]] = []
        self._client_devices_at = 0.0
        self._last_enqueued: dict[str, tuple[str, float]] = {}
        self._network_devices = {item.device_guid for item in settings.network_mappings}
        self.health = TelemetryHealth()
        self.queue = TelemetryQueue(hass, hass.config.path(".storage", f"uteamup_telemetry_{entry.entry_id}.db"))
        self.transport = TelemetryTransport(async_get_clientsession(hass, verify_ssl=True), settings.endpoint)
        self.signal = f"{DOMAIN}_{entry.entry_id}_telemetry_health"
        self._mappings = {
            item.device_guid: item for item in (*settings.mappings, *settings.network_mappings, *settings.measurement_mappings)
        }
        self._tasks: list[asyncio.Task] = []
        self._stopped = False
        self._queue_full = False
        self._storage_error = False
        self._issues: set[str] = set()

    async def async_start(self) -> None:
        try:
            await self.queue.async_configure(
                {item.device_guid: item.credential_hash for item in self._mappings.values()},
                self.settings.retry_nonce,
            )
            await self._async_update_health()
        except (sqlite3.Error, OSError):
            self._mark_storage_error()
        self._tasks = [
            self.entry.async_create_background_task(self.hass, self._sample_loop(), "UteamUP telemetry capture"),
            self.entry.async_create_background_task(self.hass, self._send_loop(), "UteamUP telemetry delivery"),
        ]

    async def async_stop(self) -> None:
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.health.sender = "disabled"
        for issue in tuple(self._issues):
            self._set_issue(issue, False)
        self._notify()

    def diagnostics(self) -> dict[str, Any]:
        """No payloads, credential hashes or source state attributes leave this boundary."""
        return {
            "sender": self.health.sender, "sources": dict(self.health.sources),
            "client_links": dict(self.health.client_links),
            "queue": asdict(self.health.queue), "last_success": self.health.last_success,
        }

    async def async_sample_once(self) -> None:
        blocked = await self.queue.async_blocked_devices()
        overflow = False
        for mapping in self.settings.mappings:
            try:
                envelope = sample_envelope(self.hass, self.settings, mapping, dt_util.utcnow())
            except SourceUnavailable as err:
                self.health.sources[mapping.device_guid] = str(err)
                continue
            self.health.sources[mapping.device_guid] = "available"
            if mapping.device_guid not in blocked:
                overflow = not await self._async_enqueue_changed(envelope) or overflow
        for mapping in self.settings.network_mappings:
            try:
                now = dt_util.utcnow()
                resolved = await self._async_resolve_clients(mapping, now)
                # Queued observations retain the asset selected at capture time. Changing a link
                # affects future samples, not the identity or contents of historical envelopes.
                envelope = sample_network_envelope(self.hass, self.settings, mapping, now, resolved)
            except SourceUnavailable as err:
                self.health.sources[mapping.device_guid] = str(err)
                continue
            self.health.sources[mapping.device_guid] = "available"
            if mapping.device_guid not in blocked:
                overflow = not await self._async_enqueue_changed(envelope) or overflow
        for mapping in self.settings.measurement_mappings:
            try:
                envelope, partial = sample_measurements(self.hass, self.settings, mapping, dt_util.utcnow())
            except ValueError as err:
                self.health.sources[mapping.device_guid] = str(err)
                continue
            self.health.sources[mapping.device_guid] = "partial" if partial else "available"
            if mapping.device_guid not in blocked:
                overflow = not await self._async_enqueue_changed(envelope) or overflow
        self._queue_full = overflow
        await self._async_update_health()

    async def _async_enqueue_changed(self, envelope: QueuedEnvelope) -> bool:
        """Repeat an unchanged sample only as a keepalive: identical minutes spend the tenant's daily quota.

        Network envelopes keep the shorter interval because UteamUP marks a client list stale after ten minutes.
        """
        fingerprint = content_fingerprint(envelope.payload)
        previous = self._last_enqueued.get(envelope.device_guid)
        now = monotonic()
        keepalive = (TELEMETRY_NETWORK_KEEPALIVE_SECONDS if envelope.device_guid in self._network_devices
                     else TELEMETRY_KEEPALIVE_SECONDS)
        if previous and previous[0] == fingerprint and now - previous[1] < keepalive:
            return True
        if not await self.queue.async_enqueue(envelope):
            return False
        self._last_enqueued[envelope.device_guid] = (fingerprint, now)
        return True

    async def _async_resolve_clients(self, mapping: NetworkTelemetryMapping, now: datetime) -> NetworkTelemetryMapping:
        if self._client is None:
            return mapping
        try:
            links = await self._client.async_get_network_client_links(mapping.device_guid)
            self.health.client_links[mapping.device_guid] = "available"
            # Validate before attempting any writes. A foreign/duplicate response never changes associations.
            resolved_client_mappings(self.hass, mapping, now, links)
            linked = {normalize_mac(link["macAddress"]) for link in links}
            candidates = [row for row in deco_clients(self.hass, mapping.tracker_registry_id, now)
                          if row["macAddress"] not in linked]
            if candidates and now.timestamp() - self._client_devices_at >= 300:
                self._client_devices = await self._client.async_get_iot_devices()
                self._client_devices_at = now.timestamp()
            saved = {item.mac_address: item for item in mapping.clients}
            attempts = 0
            for row in candidates:
                key = (mapping.device_guid, row["macAddress"])
                if now.timestamp() - self._client_link_attempts.get(key, 0) < 300:
                    continue
                matching = []
                for device in self._client_devices:
                    try:
                        if (device.get("isActive") and device.get("assetGuid")
                            and device.get("externalGuid") != mapping.device_guid
                            and normalize_mac(device.get("macAddress")) == row["macAddress"]):
                            matching.append(device)
                    except ValueError:
                        continue
                previous = saved.get(row["macAddress"])
                if previous is None and len(matching) != 1:
                    continue
                self._client_link_attempts[key] = now.timestamp()
                attempts += 1
                try:
                    # Existing explicitly reviewed HA mappings migrate without changing their asset.
                    request = {"macAddress": row["macAddress"], **({"assetGuid": previous.asset_guid}
                               if previous is not None else {"autoMatch": True})}
                    links.append(await self._client.async_link_network_client(mapping.device_guid, request))
                except UteamupError:
                    self.health.client_links[mapping.device_guid] = "link_not_allowed"
                if attempts >= 10:
                    break
            clients = resolved_client_mappings(self.hass, mapping, now, links)
            if self.health.client_links.get(mapping.device_guid) != "link_not_allowed":
                self.health.client_links[mapping.device_guid] = "available"
            return replace(mapping, clients=clients)
        except (UteamupError, ValueError, TypeError, KeyError):
            # Router telemetry and client inventory remain usable during CMMS outages. Client
            # readings pause because a cached link may have been explicitly revoked in UteamUP.
            self.health.client_links[mapping.device_guid] = "unavailable"
            return replace(mapping, clients=())

    async def async_send_once(self) -> bool:
        """Return whether a due event was considered; a retry never mutates its payload."""
        now = dt_util.utcnow()
        envelope = await self.queue.async_next(now.timestamp())
        if envelope is None:
            return False
        mapping = self._mappings.get(envelope.device_guid)
        if (
            mapping is None or envelope.tenant_guid != self.settings.tenant_guid
            or envelope.environment_guid != self.settings.environment_guid
            or envelope.mapping_fingerprint != mapping.fingerprint
        ):
            await self.queue.async_block(envelope, "mapping_changed")
        else:
            result = await self.transport.async_send(envelope, mapping.device_key)
            if result.outcome == "accepted":
                await self.queue.async_acknowledge(envelope.event_id)
                self.health.last_success = dt_util.utcnow()
            elif result.outcome == "retrying":
                await self.queue.async_defer(
                    envelope, dt_util.utcnow().timestamp() + retry_delay(envelope.attempts, result.retry_after),
                )
            else:
                await self.queue.async_block(envelope, result.outcome)
        await self._async_update_health()
        return True

    async def _sample_loop(self) -> None:
        while not self._stopped:
            try:
                await self.async_sample_once()
            except (sqlite3.Error, OSError):
                self._mark_storage_error()
            await asyncio.sleep(TELEMETRY_SAMPLE_SECONDS)

    async def _send_loop(self) -> None:
        while not self._stopped:
            sent = False
            try:
                sent = await self.async_send_once()
                if not sent:
                    await self._async_update_health()
            except (sqlite3.Error, OSError):
                self._mark_storage_error()
            await asyncio.sleep(0 if sent else 5)

    async def _async_update_health(self) -> None:
        self.health.queue = await self.queue.async_stats()
        self._storage_error = False
        queue = self.health.queue
        self.health.sender = (
            "blocked" if queue.blocked_devices else "queue_full" if self._queue_full
            else "retrying" if queue.retrying_devices else "running"
        )
        self._set_issue("queue_full", self._queue_full)
        self._set_issue("blocked", bool(queue.blocked_devices))
        self._set_issue("storage_error", False)
        self._notify()

    def _mark_storage_error(self) -> None:
        if not self._storage_error:
            _LOGGER.error("UteamUP telemetry storage is unavailable; no unpersisted readings will be sent")
        self._storage_error = True
        self.health.sender = "storage_error"
        self._set_issue("storage_error", True)
        self._notify()

    def _set_issue(self, key: str, active: bool) -> None:
        issue_id = f"{self.entry.entry_id}_telemetry_{key}"
        if active and key not in self._issues:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=f"telemetry_{key}", translation_placeholders={"title": self.entry.title},
            )
            self._issues.add(key)
        elif not active and key in self._issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            self._issues.discard(key)

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, self.signal)
