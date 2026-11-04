"""Generic sensors retain actual observations, independent availability and physical identity."""

from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
import json
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.uteamup.measurements import (
    build_measurement_source, measurement_choices, sample_component_batch, sample_component_measurements, sample_measurements,
)
from custom_components.uteamup.telemetry import UteamupTelemetry
from custom_components.uteamup.telemetry_config import (
    ComponentMeasurementSelection, ComponentMeasurementSource, ComponentTelemetrySettings,
    MeasurementMapping, MeasurementSource, TelemetrySettings,
)
from custom_components.uteamup.telemetry_config import edge_bridge_endpoint
from custom_components.uteamup.telemetry_transport import DeliveryResult
from .conftest import ASSET_GUID, ATTRIBUTE_GUID, TENANT
from .test_telemetry_config import DEVICE_GUID, DEVICE_KEY, ENDPOINT, ENVIRONMENT_GUID, ENERGY_GUID

MATTER_DEFINITIONS = [
    {"attributeDefinitionGuid": str(uuid5(NAMESPACE_URL, "matter-" + name)), "externalGuid": str(uuid5(NAMESPACE_URL, "matter-" + name)),
     "name": name, "isActive": True, "isMeter": True, "allowIoTUpdates": True, "dataType": 3, "unit": unit}
    for name, unit in (("Light is on", "0/1"), ("Available", "0/1"), ("Brightness", "%"), ("Color temperature", "K"))
]


def configured_matter(hass, config_entry, node="fabric-node-1"):
    if hass.config_entries.async_get_entry(config_entry.entry_id) is None:
        config_entry.add_to_hass(hass)
    physical = dr.async_get(hass).async_get_or_create(config_entry_id=config_entry.entry_id,
                                                    identifiers={("matter", node)})
    light = er.async_get(hass).async_get_or_create("light", "matter", f"{node}-light", device_id=physical.id)
    hass.states.async_set(light.entity_id, "on", {"supported_color_modes": ["color_temp", "xy"],
                                               "brightness": 127.5, "color_temp_kelvin": 2475})
    readings = tuple(build_measurement_source(hass, light, definition, "on" if attribute == "state" else "", attribute)
                     for definition, attribute in zip(MATTER_DEFINITIONS,
                         ("state", "availability", "brightness", "color_temp_kelvin"), strict=True))
    mapping = MeasurementMapping(DEVICE_GUID, ASSET_GUID, "IKEA light", physical.id, light.id, light.unique_id,
                                 light.platform, readings, DEVICE_KEY)
    settings = TelemetrySettings(ENDPOINT, TENANT, ENVIRONMENT_GUID, (), measurement_mappings=(mapping,))
    return settings, mapping, light


def component_selection(mapping: MeasurementMapping) -> ComponentMeasurementSelection:
    return ComponentMeasurementSelection(mapping.ha_device_id, mapping.anchor_registry_id, mapping.anchor_unique_id, tuple(
        ComponentMeasurementSource(reading.source_registry_id, reading.unit, reading.true_state,
                                   reading.known_states, reading.source_attribute)
        for reading in mapping.readings
    ))


def capture_component(hass, selection, now=None):
    return sample_component_measurements(hass, selection, tenant_guid=TENANT, source_guid=DEVICE_GUID,
                                         generation_guid=ENVIRONMENT_GUID, now=now or dt_util.utcnow())


async def test_v2_matter_uses_actual_mac_free_registry_ids_through_an_entity_rename(hass, config_entry):
    _, mapping, light = configured_matter(hass, config_entry)
    assert not dr.async_get(hass).async_get(mapping.ha_device_id).connections
    selection = component_selection(mapping)
    now = dt_util.utcnow()
    wire, partial = capture_component(hass, selection, now)
    result = json.loads(wire)
    assert not partial
    assert result["schemaVersion"] == 2
    assert result["tenantGuid"] == TENANT
    assert result["sourceInstallationGuid"] == DEVICE_GUID
    assert result["sourceGenerationGuid"] == ENVIRONMENT_GUID
    assert result["sampledAt"] == now.isoformat()
    before = result["observations"]
    assert {tuple(row["identity"].values()) for row in before} == {("homeassistant.device-registry", mapping.ha_device_id)}
    assert {row["channel"]["stableId"] for row in before} == {light.id}
    assert {row["channel"]["attribute"]: row["value"] for row in before} == {
        "state.on": 1, "availability": 1, "brightness": 50, "color_temp_kelvin": 2475,
    }
    assert {row["observedAt"] for row in before} == {now.isoformat()}
    assert not any(field in wire for field in (b"assetGuid", b"attributeDefinitionGuid", b"deviceGuid", DEVICE_KEY.encode()))

    registry = er.async_get(hass)
    renamed = registry.async_update_entity(light.entity_id, new_entity_id="light.renamed_component")
    hass.states.async_set(renamed.entity_id, "on", {"brightness": 127.5, "color_temp_kelvin": 2475})
    after = json.loads(capture_component(hass, selection)[0])["observations"]
    assert [row["channel"] for row in before] == [row["channel"] for row in after]
    assert {row["suggestions"]["entityId"] for row in after} == {renamed.entity_id}


async def test_v2_matter_distinguishes_explicit_availability_from_missing_or_stale_values(hass, config_entry):
    _, mapping, light = configured_matter(hass, config_entry)
    selection = component_selection(mapping)
    hass.states.async_set(light.entity_id, "unavailable", {"brightness": 125, "color_temp_kelvin": 2700})
    wire, partial = capture_component(hass, selection)
    observations = json.loads(wire)["observations"]
    assert partial and len(observations) == 1
    assert observations[0]["channel"]["attribute"] == "availability"
    assert observations[0]["value"] == 0

    with pytest.raises(ValueError, match="unavailable"):
        capture_component(hass, selection, dt_util.utcnow() - timedelta(minutes=1))
    hass.states.async_set(light.entity_id, "unknown")
    with pytest.raises(ValueError, match="unavailable"):
        capture_component(hass, selection)
    er.async_get(hass).async_update_entity(light.entity_id, device_id=None)
    with pytest.raises(ValueError, match="device_mismatch"):
        capture_component(hass, selection)


def component_settings(*mappings: MeasurementMapping) -> ComponentTelemetrySettings:
    return ComponentTelemetrySettings(str(uuid5(NAMESPACE_URL, "collector")), str(uuid5(NAMESPACE_URL, "generation")),
                                      ENVIRONMENT_GUID, tuple(component_selection(mapping) for mapping in mappings),
                                      "isolated-component-key")


async def test_v2_configuration_is_explicit_target_free_and_cannot_move_to_another_environment(hass, config_entry):
    existing, mapping, _ = configured_matter(hass, config_entry)
    components = component_settings(mapping)
    assert TelemetrySettings.from_dict(asdict(existing), TENANT).component_telemetry is None
    saved = {**asdict(existing), "measurement_mappings": [], "component_telemetry": asdict(components)}
    assert TelemetrySettings.from_dict(saved, TENANT).component_telemetry == components
    assert components.device_key not in repr(components)
    assert replace(components, device_key="isolated-new-key").fingerprint == components.fingerprint
    assert replace(components, device_key="isolated-new-key").credential_hash != components.credential_hash
    assert replace(components, source_generation_guid=DEVICE_GUID).fingerprint != components.fingerprint

    with pytest.raises(ValueError, match="device_mismatch"):
        TelemetrySettings.from_dict({**saved, "environment_guid": DEVICE_GUID}, TENANT)
    with pytest.raises(ValueError, match="device_mismatch"):
        TelemetrySettings.from_dict({**saved, "measurement_mappings": [asdict(mapping)]}, TENANT)
    malformed = asdict(components)
    malformed["selections"][0]["readings"][0]["asset_guid"] = ASSET_GUID
    with pytest.raises(ValueError, match="invalid_mapping"):
        TelemetrySettings.from_dict({**saved, "component_telemetry": malformed}, TENANT)


async def test_v2_batch_combines_current_physical_devices_and_does_not_replace_an_unknown_value(hass, config_entry):
    _, first, _ = configured_matter(hass, config_entry)
    _, second, second_light = configured_matter(hass, config_entry, "fabric-node-2")
    settings = component_settings(first, second)
    now = dt_util.utcnow()
    wire, partial = sample_component_batch(hass, settings, TENANT, now)
    result = json.loads(wire)
    assert not partial and len(result["observations"]) == 8
    assert len(wire) < 60_000
    assert result["sourceGenerationGuid"] == settings.source_generation_guid
    assert {row["identity"]["stableHardwareId"] for row in result["observations"]} == {first.ha_device_id, second.ha_device_id}
    assert {row["observedAt"] for row in result["observations"]} == {now.isoformat()}

    hass.states.async_set(second_light.entity_id, "unknown")
    wire, partial = sample_component_batch(hass, settings, TENANT, dt_util.utcnow())
    result = json.loads(wire)
    assert partial and len(result["observations"]) == 4
    assert {row["identity"]["stableHardwareId"] for row in result["observations"]} == {first.ha_device_id}


async def test_v2_durable_queue_retries_original_bytes_after_restart_and_consumes_only_acceptance(hass, config_entry, monkeypatch):
    base, mapping, _ = configured_matter(hass, config_entry)
    collector = component_settings(mapping)
    settings = replace(base, measurement_mappings=(), component_telemetry=collector)
    initial = UteamupTelemetry(hass, config_entry, settings)
    await initial.async_sample_once()
    await initial.async_sample_once()
    assert (await initial.queue.async_stats()).pending == 1  # Repeated state did not enqueue a new event.
    original = await initial.queue.async_next(dt_util.utcnow().timestamp())
    assert original is not None

    restarted = UteamupTelemetry(hass, config_entry, settings)
    direct = AsyncMock(side_effect=[DeliveryResult("retrying"), DeliveryResult("accepted")])
    legacy = AsyncMock()
    monkeypatch.setattr(restarted.transport, "async_send_component", direct)
    monkeypatch.setattr(restarted.transport, "async_send", legacy)
    monkeypatch.setattr("custom_components.uteamup.telemetry.retry_delay", lambda *_: 0)
    assert await restarted.async_send_once()
    assert (await restarted.queue.async_stats()).pending == 1
    assert await restarted.async_send_once()
    assert (await restarted.queue.async_stats()).pending == 0
    queued = [call.args[0] for call in direct.await_args_list]
    assert all(item.event_id == original.event_id and item.payload == original.payload for item in queued)
    assert [item.attempts for item in queued] == [0, 1]
    assert all(call.args[1].generation_guid == collector.source_generation_guid for call in direct.await_args_list)
    legacy.assert_not_awaited()


async def test_v2_old_generation_is_quarantined_without_blocking_the_new_generation(hass, config_entry, monkeypatch):
    base, mapping, _ = configured_matter(hass, config_entry)
    prior = component_settings(mapping)
    settings = replace(base, measurement_mappings=(), component_telemetry=prior)
    initial = UteamupTelemetry(hass, config_entry, settings)
    await initial.async_sample_once()

    current = replace(prior, source_generation_guid=DEVICE_GUID)
    restarted = UteamupTelemetry(hass, config_entry, replace(settings, component_telemetry=current))
    direct = AsyncMock(return_value=DeliveryResult("accepted"))
    legacy = AsyncMock()
    monkeypatch.setattr(restarted.transport, "async_send_component", direct)
    monkeypatch.setattr(restarted.transport, "async_send", legacy)
    assert await restarted.async_send_once()
    direct.assert_not_awaited()
    stats = await restarted.queue.async_stats()
    assert stats.quarantined == 1 and stats.blocked_devices == 0

    await restarted.async_sample_once()
    assert await restarted.async_send_once()
    assert direct.await_count == 1
    assert direct.await_args.args[1].generation_guid == current.source_generation_guid
    assert json.loads(direct.await_args.args[0].payload)["sourceGenerationGuid"] == current.source_generation_guid
    legacy.assert_not_awaited()
    stats = await restarted.queue.async_stats()
    assert stats.pending == 0 and stats.quarantined == 1


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


async def test_matter_light_without_mac_exports_real_state_and_attributes(hass, config_entry):
    settings, mapping, light = configured_matter(hass, config_entry)
    assert not dr.async_get(hass).async_get(mapping.ha_device_id).connections
    assert light in measurement_choices(hass, mapping.ha_device_id)
    event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
    points = json.loads(event.payload)["points"]
    assert not partial
    assert [point["value"] for point in points] == [1, 1, 50, 2475]
    assert [point["unit"] for point in points] == ["0/1", "0/1", "%", "K"]
    assert all(point["assetGuid"] == ASSET_GUID and point["quality"] == "measured" for point in points)
    assert [point["metadata"]["sourceAttribute"] for point in points] == [
        "state", "availability", "brightness", "color_temp_kelvin",
    ]
    assert TelemetrySettings.from_dict(asdict(settings), TENANT) == settings
    er.async_get(hass).async_update_entity(light.entity_id, new_entity_id="light.renamed_matter_light")
    hass.states.async_set("light.renamed_matter_light", "on", {"brightness": 255, "color_temp_kelvin": 3000})
    event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
    assert not partial
    assert [point["value"] for point in json.loads(event.payload)["points"]] == [1, 1, 100, 3000]


async def test_matter_unavailable_is_health_zero_without_false_light_off(hass, config_entry):
    settings, mapping, light = configured_matter(hass, config_entry)
    hass.states.async_set(light.entity_id, "unavailable", {"brightness": 255, "color_temp_kelvin": 2475})
    event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
    body = json.loads(event.payload)
    assert partial
    assert [(point["attributeDefinitionGuid"], point["value"]) for point in body["points"]] == [
        (MATTER_DEFINITIONS[1]["attributeDefinitionGuid"], 0),
    ]
    assert len(body["metadata"]["unavailableMeasurements"]) == 3


@pytest.mark.parametrize("state,attributes", [("unknown", {}), ("on", {"restored": True}), ("", {})])
async def test_matter_unknown_or_restored_never_fabricates_availability(hass, config_entry, state, attributes):
    settings, mapping, light = configured_matter(hass, config_entry)
    hass.states.async_set(light.entity_id, state, attributes)
    with pytest.raises(ValueError, match="unavailable"):
        sample_measurements(hass, settings, mapping, dt_util.utcnow())


@pytest.mark.parametrize("brightness", [None, True, -1, 256, "255", float("nan"), float("inf")])
async def test_invalid_brightness_omits_only_that_reading(hass, config_entry, brightness):
    settings, mapping, light = configured_matter(hass, config_entry)
    hass.states.async_set(light.entity_id, "on", {"brightness": brightness, "color_temp_kelvin": 2475})
    event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
    assert partial
    assert [point["value"] for point in json.loads(event.payload)["points"]] == [1, 1, 2475]


async def test_off_light_has_known_state_but_no_invented_brightness_or_temperature(hass, config_entry):
    settings, mapping, light = configured_matter(hass, config_entry)
    hass.states.async_set(light.entity_id, "off", {"brightness": None, "color_temp_kelvin": None})
    event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
    assert partial
    assert [point["value"] for point in json.loads(event.payload)["points"]] == [0, 1]


async def test_matter_identity_replacement_cannot_reuse_light_mapping(hass, config_entry):
    settings, mapping, light = configured_matter(hass, config_entry)
    for changed in (replace(mapping, anchor_unique_id="another-node"), replace(mapping, anchor_platform="wiz")):
        with pytest.raises(ValueError, match="device_mismatch"):
            sample_measurements(hass, settings, changed, dt_util.utcnow())
    er.async_get(hass).async_update_entity(light.entity_id, device_id=None)
    with pytest.raises(ValueError, match="device_mismatch"):
        sample_measurements(hass, settings, mapping, dt_util.utcnow())


async def test_state_only_measurement_fingerprint_survives_matter_upgrade(hass, config_entry):
    _, mapping, *_ = configured(hass, config_entry)
    old = asdict(mapping)
    old.pop("device_key")
    old.pop("name")
    for source in old["readings"]:
        source.pop("source_attribute")
    expected = hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()
    legacy = asdict(mapping)
    for source in legacy["readings"]:
        source.pop("source_attribute")
    assert MeasurementMapping.from_dict(legacy).fingerprint == expected == mapping.fingerprint
    changed = replace(mapping, readings=(replace(mapping.readings[0], unit="0/1", source_attribute="availability"),))
    assert changed.fingerprint != mapping.fingerprint


@pytest.mark.parametrize("attribute,unit", [("private_text", "0/1"), ("brightness", "W"), ("availability", "%")])
async def test_saved_matter_mapping_rejects_unsupported_attribute_or_unit(hass, config_entry, attribute, unit):
    _, mapping, _ = configured_matter(hass, config_entry)
    changed = replace(mapping, readings=(replace(mapping.readings[1], source_attribute=attribute, unit=unit),))
    with pytest.raises(ValueError, match="invalid_mapping"):
        MeasurementMapping.from_dict(asdict(changed))


@pytest.mark.parametrize("endpoint", ["http://192.168.68.13:41080", "https://@192.168.68.13:41080", "http://127.0.0.1:41080", "http://8.8.8.8:41080",
    "http://collector.local:41080", "http://192.168.68.13:80", "http://192.168.68.13:99999",
    "http://user:password@192.168.68.13:41080", "https://192.168.68.13:41080/other", "https://192.168.68.13:41080?secret=1"])
def test_edge_bridge_origin_is_an_explicit_private_lan_address(endpoint):
    with pytest.raises(ValueError, match="invalid_mapping"):
        edge_bridge_endpoint(endpoint)


async def test_bridge_measurements_do_not_need_cloud_keys_on_home_assistant(hass, config_entry):
    settings, mapping, _ = configured_matter(hass, config_entry)
    settings = replace(settings, measurement_mappings=(replace(mapping, device_key=""),),
                       edge_bridge_endpoint="https://192.168.68.13:41080",
                       edge_bridge_token="ha-intake-token-12345678901234567890", edge_bridge_devices=(DEVICE_GUID,))
    assert TelemetrySettings.from_dict(asdict(settings), TENANT) == settings
    assert settings.edge_bridge_token not in repr(settings)
    direct = asdict(replace(settings, edge_bridge_endpoint="", edge_bridge_token="", edge_bridge_devices=()))
    with pytest.raises(ValueError, match="invalid_mapping"):
        TelemetrySettings.from_dict(direct, TENANT)


async def test_matter_battery_and_contact_report_actual_zero_and_binary_state(hass, config_entry):
    settings, mapping, light = configured_matter(hass, config_entry)
    registry = er.async_get(hass)
    battery = registry.async_get_or_create("sensor", "matter", "battery", device_id=mapping.ha_device_id, unit_of_measurement="%")
    contact = registry.async_get_or_create("binary_sensor", "matter", "contact", device_id=mapping.ha_device_id)
    hass.states.async_set(battery.entity_id, "0", {"unit_of_measurement": "%"})
    hass.states.async_set(contact.entity_id, "off")
    battery_source = build_measurement_source(hass, battery, MATTER_DEFINITIONS[2])
    contact_source = build_measurement_source(hass, contact, MATTER_DEFINITIONS[0], "on")
    availability = build_measurement_source(hass, contact, MATTER_DEFINITIONS[1], source_attribute="availability")
    mapping = replace(mapping, anchor_registry_id=contact.id, anchor_unique_id=contact.unique_id,
                      readings=(battery_source, contact_source, availability))
    event, partial = sample_measurements(hass, settings, mapping, dt_util.utcnow())
    assert not partial
    assert [point["value"] for point in json.loads(event.payload)["points"]] == [0, 0, 1]
