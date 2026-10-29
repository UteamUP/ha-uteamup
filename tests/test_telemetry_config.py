"""Physical source identity, unit and tenant validation for optional telemetry."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

import pytest

from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.const import STATE_UNAVAILABLE

from custom_components.uteamup.const import CONF_TENANT_GUID
from custom_components.uteamup.telemetry_config import (
    NETWORK_METRICS, TelemetrySettings, build_mapping, build_network_mapping,
    environment_settings, ingestion_endpoint, mapping_source_choices,
)

from .conftest import ASSET_GUID, ATTRIBUTE_GUID, TENANT

DEVICE_GUID = "55555555-5555-4555-8555-555555555555"
ENERGY_GUID = "66666666-6666-4666-8666-666666666666"
ENVIRONMENT_GUID = "77777777-7777-4777-8777-777777777777"
ASSET_TYPE_GUID = "88888888-8888-4888-8888-888888888888"
ENDPOINT = "https://ingestion.example.test"
DEVICE_KEY = "device-only-secret"
DEVICE = {
    "externalGuid": DEVICE_GUID, "name": "Entrance", "assetGuid": ASSET_GUID,
    "macAddress": "AA:BB:CC:DD:EE:FF", "isActive": True,
}
ATTRIBUTES = [{"assetTypeGuid": ASSET_TYPE_GUID, "definitions": [
    {"attributeDefinitionGuid": ATTRIBUTE_GUID, "name": "Estimated power", "unit": "W", "isMeter": True, "isActive": True, "allowIoTUpdates": True, "dataType": 3},
    {"attributeDefinitionGuid": ENERGY_GUID, "name": "Cumulative estimated energy", "unit": "kWh", "isMeter": True, "isActive": True, "allowIoTUpdates": True, "dataType": 3},
]}]
ASSET_TYPE = {"isActive": True, "attributeDefinitions": [
    {**item, "externalGuid": item["attributeDefinitionGuid"]} for item in ATTRIBUTES[0]["definitions"]
]}


def environment_response():
    return {"isEnabled": True, "status": "Active", "environment": {
        "environmentGuid": ENVIRONMENT_GUID, "tenantGuid": TENANT,
        "status": "Active", "isHealthy": True, "httpBaseUrl": ENDPOINT,
    }}


def register_sources(hass, config_entry):
    """A physical WiZ bulb plus a template W source and native Integral helper."""
    if hass.config_entries.async_get_entry(config_entry.entry_id) is None:
        config_entry.add_to_hass(hass)
    physical = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, DEVICE["macAddress"])},
    )
    registry = er.async_get(hass)
    light = registry.async_get_or_create("light", "wiz", "aabbccddeeff", device_id=physical.id)
    power = registry.async_get_or_create("sensor", "template", "entrance_power", unit_of_measurement="W")
    energy = registry.async_get_or_create("sensor", "integration", "entrance_energy", unit_of_measurement="kWh")
    hass.states.async_set(light.entity_id, "off")
    hass.states.async_set(power.entity_id, "0.3", {
        "unit_of_measurement": "W", "source_device_id": physical.id,
        "power_basis": "estimated", "estimate_profile": "demo-linear-v1",
    })
    hass.states.async_set(energy.entity_id, "1.2", {"unit_of_measurement": "kWh", "source": power.entity_id})
    inputs = {
        "source_entity": light.entity_id, "power_entity": power.entity_id, "energy_entity": energy.entity_id,
        "power_attribute_guid": ATTRIBUTE_GUID, "energy_attribute_guid": ENERGY_GUID,
        "estimate_profile": "demo-linear-v1", "device_key": DEVICE_KEY,
    }
    return light, power, energy, inputs


def register_socket_sources(hass, config_entry):
    """Native WiZ socket watts, with cumulative kWh from its Integral helper."""
    light, old_power, _, inputs = register_sources(hass, config_entry)
    registry = er.async_get(hass)
    socket = registry.async_get_or_create("switch", "wiz", "socket_aabbccddeeff", device_id=light.device_id)
    power = registry.async_get_or_create("sensor", "wiz", "socket_power", device_id=light.device_id, unit_of_measurement="W")
    energy = registry.async_get(inputs["energy_entity"])
    hass.states.async_set(socket.entity_id, "on")
    hass.states.async_set(power.entity_id, "88", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(energy.entity_id, "11.937", {"unit_of_measurement": "kWh", "source": power.entity_id})
    registry.async_remove(light.entity_id)
    registry.async_remove(old_power.entity_id)
    inputs.update({"source_entity": socket.entity_id, "power_entity": power.entity_id, "power_basis": "measured", "estimate_profile": ""})
    return socket, power, energy, inputs


NETWORK_DEVICE_GUID = "55555555-5555-4555-8555-555555555556"
NETWORK_ASSET_GUID = "22222222-2222-4222-8222-222222222223"
NETWORK_DEVICE = {
    "externalGuid": NETWORK_DEVICE_GUID,
    "name": "Main Deco",
    "assetGuid": NETWORK_ASSET_GUID,
    "macAddress": "A8:42:A1:D9:37:3E",
    "isActive": True,
}
NETWORK_ATTRIBUTE_GUIDS = {
    name: f"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa{index:02d}"
    for index, (name, _, _) in enumerate(NETWORK_METRICS, start=1)
}
NETWORK_ATTRIBUTES = [{"assetTypeGuid": ASSET_TYPE_GUID, "definitions": [
    {
        "attributeDefinitionGuid": NETWORK_ATTRIBUTE_GUIDS[name],
        "name": name,
        "unit": unit,
        "isMeter": True,
        "isActive": True,
        "allowIoTUpdates": True,
        "dataType": 3,
    }
    for name, _, unit in NETWORK_METRICS
]}]


def register_deco_sources(hass, config_entry):
    """A Deco tracker authenticates the router through state attributes, not registry MACs."""
    if hass.config_entries.async_get_entry(config_entry.entry_id) is None:
        config_entry.add_to_hass(hass)
    physical = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("tplink_deco", "main_deco")},
    )
    registry = er.async_get(hass)
    tracker = registry.async_get_or_create(
        "device_tracker", "tplink_deco", "main_deco", device_id=physical.id, config_entry=config_entry,
    )
    hass.states.async_set(tracker.entity_id, "not_home", {
        "device_type": "deco",
        "mac": NETWORK_DEVICE["macAddress"],
    })
    entities = {}
    values = {"download": "32658.875", "upload": "740.875", "clients": "19"}
    for name, domain, unit in NETWORK_METRICS:
        native_unit = None if name == "clients" else unit
        entity = registry.async_get_or_create(
            domain,
            "tplink_deco",
            f"main_deco_{name}",
            device_id=physical.id,
            unit_of_measurement=native_unit if domain == "sensor" else None,
        )
        state = values.get(name, "on" if name == "internet" else "off")
        attributes = {"unit_of_measurement": native_unit} if domain == "sensor" and native_unit else {}
        hass.states.async_set(entity.entity_id, state, attributes)
        entities[name] = entity
    inputs = {
        "tracker_entity": tracker.entity_id,
        **{f"{name}_entity": entities[name].entity_id for name, _, _ in NETWORK_METRICS},
        **{f"{name}_attribute_guid": NETWORK_ATTRIBUTE_GUIDS[name] for name, _, _ in NETWORK_METRICS},
        "device_key": DEVICE_KEY,
    }
    return tracker, entities, inputs


async def test_native_socket_mapping_offers_its_sources_and_needs_no_estimate_profile(hass, config_entry):
    socket, power, energy, inputs = register_socket_sources(hass, config_entry)
    unrelated = er.async_get(hass).async_get_or_create("switch", "wiz", "another_socket")
    choices = mapping_source_choices(hass, DEVICE)
    assert choices == {"source_entity": [socket.entity_id], "power_entity": [power.entity_id], "energy_entity": [energy.entity_id]}
    assert unrelated.entity_id not in choices["source_entity"]
    mapping = build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    assert mapping.power_basis == "measured"
    assert mapping.estimate_profile == ""
    saved = {**environment_settings(environment_response(), TENANT), "mappings": [asdict(mapping)]}
    assert TelemetrySettings.from_dict(saved, TENANT).mappings[0] == mapping


async def test_estimated_helpers_cannot_be_labelled_as_measured(hass, config_entry):
    *_, inputs = register_sources(hass, config_entry)
    with pytest.raises(ValueError, match="measured_source_required"):
        build_mapping(hass, DEVICE, ATTRIBUTES, {**inputs, "power_basis": "measured"})


async def test_measured_energy_cannot_come_from_another_meter_on_the_device(hass, config_entry):
    socket, _, energy, inputs = register_socket_sources(hass, config_entry)
    other = er.async_get(hass).async_get_or_create("sensor", "wiz", "other_meter", device_id=socket.device_id)
    hass.states.async_set(energy.entity_id, "11.937", {"unit_of_measurement": "kWh", "source": other.entity_id})
    with pytest.raises(ValueError, match="measured_source_required"):
        build_mapping(hass, DEVICE, ATTRIBUTES, inputs)


async def test_existing_estimated_queue_fingerprints_survive_mapping_upgrade(hass, config_entry):
    *_, inputs = register_sources(hass, config_entry)
    mapping = build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    legacy_mapping = asdict(mapping)
    legacy_mapping.pop("power_basis")
    legacy_identity = {key: value for key, value in legacy_mapping.items() if key not in {"device_key", "name", "estimate_profile"}}
    old_fingerprint = hashlib.sha256(json.dumps(legacy_identity, sort_keys=True).encode()).hexdigest()
    saved = {**environment_settings(environment_response(), TENANT), "mappings": [legacy_mapping]}
    restored = TelemetrySettings.from_dict(saved, TENANT).mappings[0]
    assert restored.power_basis == "estimated"
    assert restored.fingerprint == old_fingerprint


async def test_mapping_stores_registry_ids_and_accepts_integral_provenance(hass, config_entry):
    light, power, energy, inputs = register_sources(hass, config_entry)
    mapping = build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    assert (mapping.source_registry_id, mapping.power_registry_id, mapping.energy_registry_id) == (light.id, power.id, energy.id)
    renamed = er.async_get(hass).async_update_entity(light.entity_id, new_entity_id="light.renamed_bulb")
    assert er.async_get(hass).async_get(mapping.source_registry_id).entity_id == renamed.entity_id
    assert DEVICE_KEY not in repr(mapping)


@pytest.mark.parametrize("field,value,error", [
    ("source_entity", "light.missing", "source_missing"),
    ("power_attribute_guid", ENERGY_GUID, "attribute_mismatch"),
    ("energy_attribute_guid", ATTRIBUTE_GUID, "attribute_mismatch"),
    ("estimate_profile", "", "invalid_mapping"),
])
async def test_invalid_mapping_is_rejected(hass, config_entry, field, value, error):
    *_, inputs = register_sources(hass, config_entry)
    with pytest.raises(ValueError, match=error):
        build_mapping(hass, DEVICE, ATTRIBUTES, {**inputs, field: value})


async def test_cross_bulb_and_wrong_units_are_rejected(hass, config_entry):
    _, power, _, inputs = register_sources(hass, config_entry)
    with pytest.raises(ValueError, match="device_mismatch"):
        build_mapping(hass, {**DEVICE, "macAddress": "00:11:22:33:44:55"}, ATTRIBUTES, inputs)
    hass.states.async_set(power.entity_id, "1", {"unit_of_measurement": "kW"})
    with pytest.raises(ValueError, match="unit_mismatch"):
        build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    hass.states.async_set(power.entity_id, "1", {"unit_of_measurement": "W", "source_device_id": "another-bulb"})
    with pytest.raises(ValueError, match="device_mismatch"):
        build_mapping(hass, DEVICE, ATTRIBUTES, inputs)


async def test_existing_masked_key_is_preserved_when_left_blank(hass, config_entry):
    *_, inputs = register_sources(hass, config_entry)
    previous = build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    updated = build_mapping(hass, DEVICE, ATTRIBUTES, {**inputs, "device_key": ""}, previous)
    assert updated.device_key == DEVICE_KEY
    assert updated.fingerprint == previous.fingerprint


@pytest.mark.parametrize("register", [register_sources, register_socket_sources])
async def test_unchanged_unavailable_mapping_can_be_retained_but_not_replaced(hass, config_entry, register):
    source, power, energy, inputs = register(hass, config_entry)
    previous = build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    for entity in (source, power, energy):
        hass.states.async_set(entity.entity_id, STATE_UNAVAILABLE)
    unchanged = {**inputs, "device_key": ""}
    assert build_mapping(hass, DEVICE, ATTRIBUTES, unchanged, previous) == previous
    with pytest.raises(ValueError, match="device_mismatch"):
        build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    with pytest.raises(ValueError, match="device_mismatch"):
        build_mapping(hass, {**DEVICE, "assetGuid": DEVICE_GUID}, ATTRIBUTES, unchanged, previous)
    with pytest.raises(ValueError, match="device_mismatch"):
        build_mapping(hass, DEVICE, ATTRIBUTES, {**inputs, "device_key": "changed-key"}, previous)
    disabled = [{"definitions": [{**item, "allowIoTUpdates": False} for item in ATTRIBUTES[0]["definitions"]]}]
    with pytest.raises(ValueError, match="attribute_mismatch"):
        build_mapping(hass, DEVICE, disabled, unchanged, previous)


@pytest.mark.parametrize("flag", ["isActive", "allowIoTUpdates"])
async def test_disabled_or_non_iot_meter_is_rejected(hass, config_entry, flag):
    *_, inputs = register_sources(hass, config_entry)
    definitions = [{"definitions": [{**item, flag: False} for item in ATTRIBUTES[0]["definitions"]]}]
    with pytest.raises(ValueError, match="attribute_mismatch"):
        build_mapping(hass, DEVICE, definitions, inputs)


async def test_device_key_cannot_inject_http_headers(hass, config_entry):
    *_, inputs = register_sources(hass, config_entry)
    with pytest.raises(ValueError, match="invalid_mapping"):
        build_mapping(hass, DEVICE, ATTRIBUTES, {**inputs, "device_key": "key\r\nAnother: value"})


async def test_string_meter_is_not_a_numeric_telemetry_target(hass, config_entry):
    *_, inputs = register_sources(hass, config_entry)
    definitions = [{"definitions": [{**item, "dataType": 1} for item in ATTRIBUTES[0]["definitions"]]}]
    with pytest.raises(ValueError, match="attribute_mismatch"):
        build_mapping(hass, DEVICE, definitions, inputs)


@pytest.mark.parametrize("address", [
    "http://ingestion.example.test", "https://user:secret@ingestion.example.test",
    "https://ingestion.example.test/other", "https://ingestion.example.test?key=secret",
    "https://ingestion.example.test#fragment", "None",
])
def test_endpoint_rejects_insecure_or_ambiguous_addresses(address):
    with pytest.raises(ValueError):
        ingestion_endpoint(address)


@pytest.mark.parametrize("field,value", [("status", "AgreementPending"), ("isHealthy", False), ("httpBaseUrl", None)])
def test_entitlement_alone_never_enables_sender(field, value):
    response = environment_response()
    response["environment"][field] = value
    with pytest.raises(ValueError):
        environment_settings(response, TENANT)


async def test_cached_settings_remain_tenant_bound(hass, config_entry):
    *_, inputs = register_sources(hass, config_entry)
    saved = {**environment_settings(environment_response(), TENANT), "mappings": [asdict(build_mapping(hass, DEVICE, ATTRIBUTES, inputs))]}
    assert TelemetrySettings.from_dict(saved, TENANT).endpoint == ENDPOINT
    with pytest.raises(ValueError, match="device_mismatch"):
        TelemetrySettings.from_dict({**saved, CONF_TENANT_GUID: DEVICE_GUID}, TENANT)


async def test_deco_mapping_round_trip_preserves_energy_fingerprint(hass, config_entry):
    *_, energy_inputs = register_sources(hass, config_entry)
    *_, network_inputs = register_deco_sources(hass, config_entry)
    energy = build_mapping(hass, DEVICE, ATTRIBUTES, energy_inputs)
    network = build_network_mapping(hass, NETWORK_DEVICE, NETWORK_ATTRIBUTES, network_inputs)
    saved = {
        **environment_settings(environment_response(), TENANT),
        "mappings": [asdict(energy)],
        "network_mappings": [asdict(network)],
    }
    restored = TelemetrySettings.from_dict(saved, TENANT)
    assert restored.mappings[0] == energy
    assert restored.mappings[0].fingerprint == energy.fingerprint
    assert restored.network_mappings[0] == network
    assert DEVICE_KEY not in repr(network)
    assert hass.states.get(network_inputs["tracker_entity"]).state == "not_home"


async def test_deco_mapping_rejects_wrong_mac_and_source_unit(hass, config_entry):
    _, entities, inputs = register_deco_sources(hass, config_entry)
    with pytest.raises(ValueError, match="device_mismatch"):
        build_network_mapping(
            hass,
            {**NETWORK_DEVICE, "macAddress": "00:11:22:33:44:55"},
            NETWORK_ATTRIBUTES,
            inputs,
        )
    hass.states.async_set(entities["download"].entity_id, "32", {"unit_of_measurement": "MB/s"})
    with pytest.raises(ValueError, match="unit_mismatch"):
        build_network_mapping(hass, NETWORK_DEVICE, NETWORK_ATTRIBUTES, inputs)
