"""Generic measured sources: preserve units, source identity and independent availability."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from unicodedata import category
from uuid import UUID, uuid4

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .telemetry_config import (
    MeasurementMapping, MeasurementSource, TelemetrySettings, compatible_meter, public_guid, source_unit,
)
from .telemetry_queue import QueuedEnvelope

VACUUM_STATES = ("cleaning", "docked", "returning", "idle", "paused", "error")


@dataclass(frozen=True)
class ComponentMeasurementSource:
    """A reviewed local Matter observation; it cannot name a CMMS target."""

    source_registry_id: str
    unit: str
    true_state: str = ""
    known_states: tuple[str, ...] = ()
    source_attribute: str = "state"


_SourceReading = MeasurementSource | ComponentMeasurementSource


@dataclass(frozen=True)
class ComponentMeasurementSelection:
    ha_device_id: str
    anchor_registry_id: str
    anchor_unique_id: str
    readings: tuple[ComponentMeasurementSource, ...]


def measurement_states(hass: HomeAssistant, entity: er.RegistryEntry) -> tuple[str, ...]:
    if entity.domain in {"binary_sensor", "light", "switch"}:
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
        if entity.disabled or entity.domain not in {"sensor", "binary_sensor", "vacuum", "light", "switch"}:
            continue
        state = hass.states.get(entity.entity_id)
        if measurement_states(hass, entity) or source_unit(entity, state):
            result.append(entity)
    return result


def measurement_attributes(hass: HomeAssistant, entity: er.RegistryEntry) -> tuple[str, ...]:
    """Offer explicit device health and supported light attributes without requiring a MAC."""
    choices = ["state"]
    if entity.platform == "matter" or entity.domain in {"light", "switch"}:
        choices.append("availability")
    if entity.domain == "light":
        state = hass.states.get(entity.entity_id)
        modes = state.attributes.get("supported_color_modes", ()) if state else ()
        if any(mode not in {"onoff", "unknown"} for mode in modes):
            choices.append("brightness")
        if "color_temp" in modes:
            choices.append("color_temp_kelvin")
    return tuple(choices)


def measurement_unit(hass: HomeAssistant, entity: er.RegistryEntry, source_attribute: str = "state") -> str:
    if source_attribute == "availability":
        return "0/1"
    if source_attribute == "brightness":
        return "%"
    if source_attribute == "color_temp_kelvin":
        return "K"
    return "0/1" if measurement_states(hass, entity) else source_unit(entity, hass.states.get(entity.entity_id))


def build_measurement_source(hass: HomeAssistant, entity: er.RegistryEntry, definition: dict,
                             true_state: str = "", source_attribute: str = "state") -> MeasurementSource:
    if source_attribute not in measurement_attributes(hass, entity):
        raise ValueError("invalid_value")
    states = measurement_states(hass, entity) if source_attribute == "state" else ()
    unit = measurement_unit(hass, entity, source_attribute)
    if not unit or not compatible_meter(definition, unit):
        raise ValueError("attribute_mismatch")
    if states and true_state not in states:
        raise ValueError("invalid_value")
    if not states and true_state:
        raise ValueError("invalid_value")
    return MeasurementSource(entity.id, definition["attributeDefinitionGuid"], unit, true_state, states, source_attribute)


def _attribute_value(state: State, entity: er.RegistryEntry, reading: _SourceReading) -> float:
    if reading.source_attribute == "availability":
        if reading.unit != "0/1" or state.state == STATE_UNKNOWN:
            raise ValueError("unavailable")
        return float(state.state != STATE_UNAVAILABLE)
    if (entity.domain != "light" or reading.source_attribute not in {"brightness", "color_temp_kelvin"}
            or state.state not in {"on", "off"}):
        raise ValueError("invalid_value")
    value = state.attributes.get(reading.source_attribute)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("unavailable")
    if reading.source_attribute == "brightness":
        if reading.unit != "%" or not 0 <= value <= 255:
            raise ValueError("invalid_value")
        return value * 100 / 255
    if reading.unit != "K" or not 0 < value <= 1e6:
        raise ValueError("invalid_value")
    return float(value)


def _read_source(hass: HomeAssistant, device_id: str, reading: _SourceReading) -> tuple[er.RegistryEntry, State, float]:
    entity = er.async_get(hass).async_get(reading.source_registry_id)
    if entity is None or entity.disabled or entity.device_id != device_id:
        raise ValueError("device_mismatch")
    state = hass.states.get(entity.entity_id)
    if state is None or state.state in {STATE_UNKNOWN, ""} or state.attributes.get("restored"):
        raise ValueError("unavailable")
    if reading.source_attribute != "state":
        value = _attribute_value(state, entity, reading)
    elif state.state == STATE_UNAVAILABLE:
        raise ValueError("unavailable")
    elif reading.known_states:
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
    return entity, state, value


def _source_point(hass: HomeAssistant, mapping: MeasurementMapping, reading: MeasurementSource, now: datetime) -> dict:
    entity, state, value = _read_source(hass, mapping.ha_device_id, reading)
    return {
        "pointId": str(uuid4()), "assetGuid": mapping.asset_guid,
        "attributeDefinitionGuid": reading.attribute_guid, "value": value, "unit": reading.unit,
        "observedAt": now.isoformat(), "quality": "measured",
        "metadata": {"sourceRegistryId": entity.id, "sourceStateUpdatedAt": state.last_updated.isoformat(),
                     "sourceReportedAt": state.last_reported.isoformat(), "observationKind": "current_state_sample",
                     "sourceAttribute": reading.source_attribute},
    }


def sample_measurements(hass: HomeAssistant, settings: TelemetrySettings, mapping: MeasurementMapping,
                        now: datetime) -> tuple[QueuedEnvelope, bool]:
    """One unavailable meter must not suppress the device's other actual measurements."""
    anchor = er.async_get(hass).async_get(mapping.anchor_registry_id)
    if (anchor is None or anchor.disabled or anchor.device_id != mapping.ha_device_id
            or anchor.unique_id != mapping.anchor_unique_id or anchor.platform != mapping.anchor_platform):
        raise ValueError("device_mismatch")
    anchor_state = hass.states.get(anchor.entity_id)
    if (anchor_state is None or anchor_state.state in {STATE_UNKNOWN, ""}
            or anchor_state.attributes.get("restored")):
        raise ValueError("unavailable")
    points, unavailable = [], []
    for reading in mapping.readings:
        try:
            if anchor_state.state == STATE_UNAVAILABLE and not (
                reading.source_attribute == "availability" and reading.source_registry_id == anchor.id
            ):
                raise ValueError("unavailable")
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


def _registry_id(value: str) -> str:
    identifier = UUID(value)
    if identifier.int == 0 or value not in {identifier.hex, str(identifier)}:
        raise ValueError("invalid_registry_identity")
    return value


def _label(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    clean = "".join(character for character in value if category(character) not in {"Cc", "Cf", "Cs"})
    # The receiver measures string length in UTF-16 code units. Preserve complete characters.
    return clean.encode("utf-16-le")[:limit * 2].decode("utf-16-le", errors="ignore") or None


def _component_channel_attribute(reading: ComponentMeasurementSource) -> str:
    if (not reading.unit or reading.unit != reading.unit.strip() or _label(reading.unit, 50) != reading.unit
            or reading.source_attribute not in {"state", "availability", "brightness", "color_temp_kelvin"}
            or (reading.source_attribute != "state" and (reading.true_state or reading.known_states))):
        raise ValueError("invalid_mapping")
    if reading.true_state or reading.known_states:
        if (reading.unit != "0/1" or reading.known_states != ("on", "off")
                or reading.true_state not in reading.known_states):
            raise ValueError("invalid_mapping")
        return f"state.{reading.true_state}"
    return reading.source_attribute


def _component_observation(hass: HomeAssistant, device: dr.DeviceEntry, anchor: er.RegistryEntry,
                           anchor_state: State, reading: ComponentMeasurementSource, attribute: str,
                           now: datetime) -> dict:
    if anchor_state.state == STATE_UNAVAILABLE and not (
        reading.source_attribute == "availability" and reading.source_registry_id == anchor.id
    ):
        raise ValueError("unavailable")
    entity, state, value = _read_source(hass, device.id, reading)
    if entity.platform != "matter" or state.last_updated > now or state.last_reported > now:
        raise ValueError("unavailable")
    area_id = entity.area_id or device.area_id
    area = ar.async_get(hass).async_get_area(area_id) if area_id else None
    hints = {
        "displayName": _label(device.name_by_user or device.name, 200),
        "areaName": _label(area.name if area else None, 200),
        "entityId": _label(entity.entity_id, 256),
        "manufacturer": _label(device.manufacturer, 200),
        "model": _label(device.model, 200),
    }
    return {
        "observationId": str(uuid4()),
        "identity": {"namespace": "homeassistant.device-registry", "stableHardwareId": _registry_id(device.id)},
        "channel": {"namespace": "homeassistant.entity-registry", "stableId": _registry_id(entity.id), "attribute": attribute},
        "observedAt": now.isoformat(), "value": value, "unit": reading.unit, "observationKind": "current_state_sample",
        "provenance": {"quality": "measured", "sourceStateUpdatedAt": state.last_updated.isoformat(),
                       "sourceReportedAt": state.last_reported.isoformat()},
        "suggestions": {name: value for name, value in hints.items() if value is not None},
    }


def sample_component_measurements(hass: HomeAssistant, selection: ComponentMeasurementSelection, *,
                                  tenant_guid: str, source_guid: str, generation_guid: str,
                                  now: datetime) -> tuple[bytes, bool]:
    """Encode one V2 capture from current HA registry evidence, without choosing a fixture or meter."""
    if now.utcoffset() != timedelta(0) or not 1 <= len(selection.readings) <= 64:
        raise ValueError("invalid_mapping")
    tenant, source, generation = map(public_guid, (tenant_guid, source_guid, generation_guid))
    device = dr.async_get(hass).async_get(_registry_id(selection.ha_device_id))
    anchor = er.async_get(hass).async_get(_registry_id(selection.anchor_registry_id))
    if (device is None or device.disabled_by is not None or anchor is None or anchor.disabled
            or anchor.platform != "matter" or anchor.device_id != device.id or anchor.unique_id != selection.anchor_unique_id):
        raise ValueError("device_mismatch")
    anchor_state = hass.states.get(anchor.entity_id)
    if (anchor_state is None or anchor_state.state in {STATE_UNKNOWN, ""} or anchor_state.attributes.get("restored")
            or anchor_state.last_updated > now or anchor_state.last_reported > now):
        raise ValueError("unavailable")

    channels = [(_registry_id(reading.source_registry_id), _component_channel_attribute(reading)) for reading in selection.readings]
    if len(set(channels)) != len(channels):
        raise ValueError("invalid_mapping")
    observations = []
    for reading, (_, attribute) in zip(selection.readings, channels, strict=True):
        try:
            observations.append(_component_observation(hass, device, anchor, anchor_state, reading, attribute, now))
        except ValueError:
            continue  # No substitute reading when the real source is unavailable or has moved.
    if not observations:
        raise ValueError("unavailable")
    return json.dumps({
        "schemaVersion": 2, "eventId": str(uuid4()), "tenantGuid": tenant,
        "sourceInstallationGuid": source, "sourceGenerationGuid": generation, "sampledAt": now.isoformat(),
        "observations": observations,
    }, separators=(",", ":"), allow_nan=False).encode(), len(observations) != len(selection.readings)
