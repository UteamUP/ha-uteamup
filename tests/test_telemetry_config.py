"""Physical source identity, unit and tenant validation for optional telemetry."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.uteamup.const import CONF_TENANT_GUID
from custom_components.uteamup.telemetry_config import (
    TelemetrySettings, build_mapping, environment_settings, ingestion_endpoint,
)

from .conftest import ASSET_GUID, ATTRIBUTE_GUID, TENANT

DEVICE_GUID = "55555555-5555-4555-8555-555555555555"
ENERGY_GUID = "66666666-6666-4666-8666-666666666666"
ENVIRONMENT_GUID = "77777777-7777-4777-8777-777777777777"
ENDPOINT = "https://ingestion.example.test"
DEVICE_KEY = "device-only-secret"
DEVICE = {
    "externalGuid": DEVICE_GUID, "name": "Entrance", "assetGuid": ASSET_GUID,
    "macAddress": "AA:BB:CC:DD:EE:FF", "isActive": True,
}
ATTRIBUTES = [{"definitions": [
    {"attributeDefinitionGuid": ATTRIBUTE_GUID, "name": "Estimated power", "unit": "W", "isMeter": True},
    {"attributeDefinitionGuid": ENERGY_GUID, "name": "Cumulative estimated energy", "unit": "kWh", "isMeter": True},
]}]


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
