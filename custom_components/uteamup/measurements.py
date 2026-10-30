"""Generic measured sources: preserve units, source identity and independent availability."""

from __future__ import annotations

import json
import math
from datetime import datetime
from uuid import uuid4

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .telemetry_config import (
    MeasurementMapping, MeasurementSource, TelemetrySettings, compatible_meter, source_unit,
)
from .telemetry_queue import QueuedEnvelope

VACUUM_STATES = ("cleaning", "docked", "returning", "idle", "paused", "error")


def measurement_states(hass: HomeAssistant, entity: er.RegistryEntry) -> tuple[str, ...]:
    if entity.domain == "binary_sensor":
        return ("on", "off")
    if entity.domain == "vacuum":
        return VACUUM_STATES
    state = hass.states.get(entity.entity_id)
    values = state.attributes.get("options", ()) if state else ()
    if isinstance(values, (list, tuple)) and 1 < len(values) <= 64 and all(isinstance(v, str) for v in values):
        return tuple(values)
    return ()


def measurement_choices(hass: HomeAssistant, device_id: str) -> list[er.RegistryEntry]:
    result = []
    for entity in er.async_entries_for_device(er.async_get(hass), device_id):
        if entity.disabled or entity.domain not in {"sensor", "binary_sensor", "vacuum"}:
            continue
        state = hass.states.get(entity.entity_id)
        if measurement_states(hass, entity) or source_unit(entity, state):
            result.append(entity)
    return result


def build_measurement_source(hass: HomeAssistant, entity: er.RegistryEntry, definition: dict,
                             true_state: str = "") -> MeasurementSource:
    states = measurement_states(hass, entity)
    unit = "0/1" if states else source_unit(entity, hass.states.get(entity.entity_id))
    if not unit or not compatible_meter(definition, unit):
        raise ValueError("attribute_mismatch")
    if states and true_state not in states:
        raise ValueError("invalid_value")
    if not states and true_state:
        raise ValueError("invalid_value")
    return MeasurementSource(entity.id, definition["attributeDefinitionGuid"], unit, true_state, states)


def _source_point(hass: HomeAssistant, mapping: MeasurementMapping, reading: MeasurementSource, now: datetime) -> dict:
    entity = er.async_get(hass).async_get(reading.source_registry_id)
    if entity is None or entity.disabled or entity.device_id != mapping.ha_device_id:
        raise ValueError("device_mismatch")
    state = hass.states.get(entity.entity_id)
    if state is None or state.state in {STATE_UNAVAILABLE, STATE_UNKNOWN, ""} or state.attributes.get("restored"):
        raise ValueError("unavailable")
    if reading.known_states:
        if measurement_states(hass, entity) != reading.known_states or state.state not in reading.known_states:
            raise ValueError("invalid_value")
        value = float(state.state == reading.true_state)
    else:
        if entity.domain != "sensor" or source_unit(entity, state) != reading.unit:
            raise ValueError("unit_mismatch")
        try:
            value = float(state.state)
        except (TypeError, ValueError) as err:
            raise ValueError("invalid_value") from err
        if not math.isfinite(value) or abs(value) > 1e15:
            raise ValueError("invalid_value")
    return {
        "pointId": str(uuid4()), "assetGuid": mapping.asset_guid,
        "attributeDefinitionGuid": reading.attribute_guid, "value": value, "unit": reading.unit,
        "observedAt": now.isoformat(), "quality": "measured",
        "metadata": {"sourceRegistryId": entity.id, "sourceStateUpdatedAt": state.last_updated.isoformat(),
                     "sourceReportedAt": state.last_reported.isoformat(), "observationKind": "current_state_sample"},
    }


def sample_measurements(hass: HomeAssistant, settings: TelemetrySettings, mapping: MeasurementMapping,
                        now: datetime) -> tuple[QueuedEnvelope, bool]:
    """One unavailable meter must not suppress the device's other actual measurements."""
    anchor = er.async_get(hass).async_get(mapping.anchor_registry_id)
    if (anchor is None or anchor.disabled or anchor.device_id != mapping.ha_device_id
            or anchor.unique_id != mapping.anchor_unique_id or anchor.platform != mapping.anchor_platform):
        raise ValueError("device_mismatch")
    anchor_state = hass.states.get(anchor.entity_id)
    if (anchor_state is None or anchor_state.state in {STATE_UNAVAILABLE, STATE_UNKNOWN}
            or anchor_state.attributes.get("restored")):
        raise ValueError("unavailable")
    points, unavailable = [], []
    for reading in mapping.readings:
        try:
            points.append(_source_point(hass, mapping, reading, now))
        except ValueError as err:
            unavailable.append({"attributeDefinitionGuid": reading.attribute_guid, "reason": str(err)})
    if not points:
        raise ValueError("unavailable")
    event_id = str(uuid4())
    payload = json.dumps({
        "eventId": event_id, "tenantGuid": settings.tenant_guid, "deviceGuid": mapping.device_guid,
        "observedAt": now.isoformat(),
        "metadata": {"source": "homeassistant", "telemetryKind": "measurements", "unavailableMeasurements": unavailable},
        "points": points,
    }, separators=(",", ":"), allow_nan=False).encode()
    envelope = QueuedEnvelope(event_id, settings.tenant_guid, mapping.device_guid,
                              settings.environment_guid, mapping.fingerprint, payload)
    return envelope, bool(unavailable)
