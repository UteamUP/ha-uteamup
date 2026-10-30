"""Generic sensors retain actual observations, independent availability and physical identity."""

from dataclasses import asdict, replace
import json

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.uteamup.measurements import build_measurement_source, measurement_choices, sample_measurements
from custom_components.uteamup.telemetry import UteamupTelemetry
from custom_components.uteamup.telemetry_config import MeasurementMapping, MeasurementSource, TelemetrySettings
from .conftest import ASSET_GUID, ATTRIBUTE_GUID, TENANT
from .test_telemetry_config import DEVICE_GUID, DEVICE_KEY, ENDPOINT, ENVIRONMENT_GUID, ENERGY_GUID


def configured(hass, config_entry):
    config_entry.add_to_hass(hass)
    physical = dr.async_get(hass).async_get_or_create(config_entry_id=config_entry.entry_id,
                                                    identifiers={("xiaomi_home", "test-robot")})
    registry = er.async_get(hass)
    anchor = registry.async_get_or_create("vacuum", "xiaomi_home", "test-robot", device_id=physical.id)
    battery = registry.async_get_or_create("sensor", "xiaomi_home", "test-battery", device_id=physical.id, unit_of_measurement="%")
    filter_life = registry.async_get_or_create("sensor", "xiaomi_home", "test-filter", device_id=physical.id, unit_of_measurement="%")
    hass.states.async_set(anchor.entity_id, "docked")
    hass.states.async_set(battery.entity_id, "43", {"unit_of_measurement": "%", "private": "must-not-export"})
    hass.states.async_set(filter_life.entity_id, "0", {"unit_of_measurement": "%"})
    mapping = MeasurementMapping(DEVICE_GUID, ASSET_GUID, "Robot", physical.id, anchor.id, anchor.unique_id,
                                 anchor.platform, (MeasurementSource(battery.id, ATTRIBUTE_GUID, "%"),
                                                   MeasurementSource(filter_life.id, ENERGY_GUID, "%")), DEVICE_KEY)
    settings = TelemetrySettings(ENDPOINT, TENANT, ENVIRONMENT_GUID, (), measurement_mappings=(mapping,))
    return settings, mapping, anchor, battery, filter_life


async def test_generic_sources_keep_zero_units_timestamps_and_no_unmapped_tank(hass, config_entry):
    settings, mapping, _, battery, filter_life = configured(hass, config_entry)
    now = dt_util.utcnow()
    envelope, partial = sample_measurements(hass, settings, mapping, now)
    body = json.loads(envelope.payload)
    assert not partial
    assert [p["value"] for p in body["points"]] == [43, 0]
    assert {p["assetGuid"] for p in body["points"]} == {ASSET_GUID}
    assert {p["unit"] for p in body["points"]} == {"%"}
    assert body["points"][0]["observedAt"] == now.isoformat()
    assert body["points"][0]["metadata"]["sourceReportedAt"] == hass.states.get(battery.entity_id).last_reported.isoformat()
    assert "must-not-export" not in envelope.payload.decode()
    assert "tank" not in envelope.payload.decode()
    saved = asdict(settings)
    assert TelemetrySettings.from_dict(saved, TENANT) == settings
    assert DEVICE_KEY not in repr(mapping)
    assert replace(mapping, device_key="rotated-key").fingerprint == mapping.fingerprint
    assert battery in measurement_choices(hass, mapping.ha_device_id)
    assert filter_life in measurement_choices(hass, mapping.ha_device_id)


@pytest.mark.parametrize("state", ["unknown", "unavailable", "", "nan", "inf", "invalid"])
async def test_one_bad_reading_does_not_drop_other_readings_or_invent_zero(hass, config_entry, state):
    settings, mapping, _, battery, _ = configured(hass, config_entry)
    hass.states.async_set(battery.entity_id, state, {"unit_of_measurement": "%"})
    event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
    points = json.loads(event.payload)["points"]
    assert partial and len(points) == 1
    assert points[0]["attributeDefinitionGuid"] == ENERGY_GUID
    assert points[0]["value"] == 0


async def test_unavailable_device_blocks_liveness_and_moved_sensor_is_omitted(hass, config_entry):
    settings, mapping, anchor, battery, _ = configured(hass, config_entry)
    er.async_get(hass).async_update_entity(battery.entity_id, device_id=None)
    event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
    assert partial and len(json.loads(event.payload)["points"]) == 1
    hass.states.async_set(anchor.entity_id, "unavailable")
    with pytest.raises(ValueError, match="unavailable"):
        sample_measurements(hass, settings, mapping, dt_util.utcnow())
    with pytest.raises(ValueError, match="device_mismatch"):
        sample_measurements(hass, settings, replace(mapping, anchor_unique_id="another-robot"), dt_util.utcnow())


async def test_enum_is_explicit_binary_condition_and_unknown_never_means_no(hass, config_entry):
    settings, mapping, _, battery, _ = configured(hass, config_entry)
    attrs = {"options": ["Charging", "Not Charging", "Not Chargeable"]}
    hass.states.async_set(battery.entity_id, "Charging", attrs)
    definition = {"attributeDefinitionGuid": ATTRIBUTE_GUID, "isMeter": True, "isActive": True,
                  "allowIoTUpdates": True, "dataType": 3, "unit": "0/1"}
    source = build_measurement_source(hass, battery, definition, "Charging")
    mapping = replace(mapping, readings=(source,))
    for value, expected in [("Charging", 1), ("Not Charging", 0)]:
        hass.states.async_set(battery.entity_id, value, attrs)
        event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
        assert not partial and json.loads(event.payload)["points"][0]["value"] == expected
    hass.states.async_set(battery.entity_id, "unknown", attrs)
    with pytest.raises(ValueError, match="unavailable"):
        sample_measurements(hass, settings, mapping, dt_util.utcnow())
    with pytest.raises(ValueError, match="attribute_mismatch"):
        build_measurement_source(hass, battery, {**definition, "unit": "%"}, "Charging")


async def test_generic_capture_reuses_durable_queue(hass, config_entry):
    settings, mapping, *_ = configured(hass, config_entry)
    telemetry = UteamupTelemetry(hass, config_entry, settings)
    await telemetry.queue.async_configure({DEVICE_GUID: mapping.credential_hash}, "")
    await telemetry.async_sample_once()
    assert (await telemetry.queue.async_stats()).pending == 1
    assert telemetry.health.sources[DEVICE_GUID] == "available"


async def test_saved_mapping_rejects_duplicate_or_foreign_tenant(hass, config_entry):
    settings, mapping, *_ = configured(hass, config_entry)
    saved = asdict(settings)
    with pytest.raises(ValueError, match="device_mismatch"):
        TelemetrySettings.from_dict(saved, ASSET_GUID)
    with pytest.raises(ValueError, match="invalid_mapping"):
        MeasurementMapping.from_dict(asdict(replace(mapping, readings=(mapping.readings[0], mapping.readings[0]))))


@pytest.mark.parametrize("state,expected", [("cleaning", [1, 0]), ("error", [0, 1]), ("docked", [0, 0])])
async def test_vacuum_exports_independent_cleaning_and_fault_conditions(hass, config_entry, state, expected):
    settings, mapping, anchor, *_ = configured(hass, config_entry)
    definition = {"attributeDefinitionGuid": ATTRIBUTE_GUID, "isMeter": True, "isActive": True,
                  "allowIoTUpdates": True, "dataType": 3, "unit": "0/1"}
    cleaning = build_measurement_source(hass, anchor, definition, "cleaning")
    fault = build_measurement_source(hass, anchor, {**definition, "attributeDefinitionGuid": ENERGY_GUID}, "error")
    mapping = MeasurementMapping.from_dict(asdict(replace(mapping, readings=(cleaning, fault))))
    hass.states.async_set(anchor.entity_id, state)
    event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
    points = json.loads(event.payload)["points"]
    assert not partial
    assert [point["value"] for point in points] == expected
    assert [point["attributeDefinitionGuid"] for point in points] == [ATTRIBUTE_GUID, ENERGY_GUID]
    assert all(point["metadata"]["sourceRegistryId"] == anchor.id for point in points)


@pytest.mark.parametrize("state", ["unknown", "unavailable", "vendor-unknown-state"])
async def test_unavailable_multi_condition_vacuum_never_exports_false_recovery(hass, config_entry, state):
    settings, mapping, anchor, *_ = configured(hass, config_entry)
    definition = {"attributeDefinitionGuid": ATTRIBUTE_GUID, "isMeter": True, "isActive": True,
                  "allowIoTUpdates": True, "dataType": 3, "unit": "0/1"}
    cleaning = build_measurement_source(hass, anchor, definition, "cleaning")
    fault = replace(cleaning, attribute_guid=ENERGY_GUID, true_state="error")
    mapping = replace(mapping, readings=(cleaning, fault))
    hass.states.async_set(anchor.entity_id, state)
    with pytest.raises(ValueError, match="unavailable"):
        sample_measurements(hass, settings, mapping, dt_util.utcnow())


async def test_multi_condition_mapping_rejects_duplicate_target_or_condition(hass, config_entry):
    _, mapping, anchor, *_ = configured(hass, config_entry)
    definition = {"attributeDefinitionGuid": ATTRIBUTE_GUID, "isMeter": True, "isActive": True,
                  "allowIoTUpdates": True, "dataType": 3, "unit": "0/1"}
    cleaning = build_measurement_source(hass, anchor, definition, "cleaning")
    for duplicate in (replace(cleaning, true_state="error"), replace(cleaning, attribute_guid=ENERGY_GUID)):
        with pytest.raises(ValueError, match="invalid_mapping"):
            MeasurementMapping.from_dict(asdict(replace(mapping, readings=(cleaning, duplicate))))
