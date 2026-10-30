"""Retained WiZ mappings survive HA's recorded split of a shared registry device."""

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from custom_components.uteamup.telemetry import SourceUnavailable, sample_envelope
from custom_components.uteamup.telemetry_config import (
    TelemetrySettings, build_mapping, environment_settings, mapping_source_choices,
)

from .conftest import TENANT
from .test_telemetry_config import (
    ATTRIBUTES, DEVICE, environment_response, register_socket_sources, register_sources,
)

COMPOSITE_ID = "11111111111111111111111111111111"


@contextmanager
def registry_split(hass, source, *, composite_id=COMPOSITE_ID, mac=None):
    """Expose the public migration metadata added in HA 2026.8 on older test cores."""
    registry = dr.async_get(hass)
    original_get = registry.async_get
    current = original_get(source.device_id)
    split = SimpleNamespace(
        id=current.id,
        composite_device_id=composite_id,
        connections=current.connections if mac is None else {(dr.CONNECTION_NETWORK_MAC, mac)},
    )
    with patch.object(type(registry), "async_get", lambda _, device_id:
                      split if device_id == current.id else original_get(device_id)):
        yield


def retained_mapping(hass, config_entry, basis="estimated"):
    register = register_socket_sources if basis == "measured" else register_sources
    source, power, energy, inputs = register(hass, config_entry)
    mapping = replace(build_mapping(hass, DEVICE, ATTRIBUTES, inputs), ha_device_id=COMPOSITE_ID)
    if basis == "estimated":
        state = hass.states.get(power.entity_id)
        hass.states.async_set(power.entity_id, state.state, {
            **state.attributes, "source_device_id": COMPOSITE_ID,
        })
    settings = TelemetrySettings.from_dict({
        **environment_settings(environment_response(), TENANT), "mappings": [asdict(mapping)],
    }, TENANT)
    return settings, mapping, source, power, energy, inputs


@pytest.mark.parametrize("basis", ["estimated", "measured"])
async def test_recorded_split_preserves_cloud_identity_sources_and_reading_basis(hass, config_entry, basis):
    settings, mapping, source, power, energy, _ = retained_mapping(hass, config_entry, basis)
    before = asdict(mapping)
    fingerprint = mapping.fingerprint
    with registry_split(hass, source):
        envelope = sample_envelope(hass, settings, mapping, dt_util.utcnow())
    body = json.loads(envelope.payload)
    assert body["deviceGuid"] == mapping.device_guid
    assert {point["assetGuid"] for point in body["points"]} == {mapping.asset_guid}
    assert {point["attributeDefinitionGuid"] for point in body["points"]} == {
        mapping.power_attribute_guid, mapping.energy_attribute_guid,
    }
    assert body["metadata"]["sourceRegistryId"] == source.id
    assert body["metadata"]["powerSourceRegistryId"] == power.id
    assert body["metadata"]["energySourceRegistryId"] == energy.id
    assert {point["quality"] for point in body["points"]} == {basis}
    assert [point["value"] for point in body["points"]] == (
        [88, 11.937] if basis == "measured" else [0.3, 1.2]
    )
    assert asdict(mapping) == before
    assert mapping.fingerprint == fingerprint == envelope.mapping_fingerprint


@pytest.mark.parametrize("composite_id,mac", [
    (None, None),
    ("22222222222222222222222222222222", None),
    (COMPOSITE_ID, "00:11:22:33:44:55"),
])
async def test_unrelated_replacement_or_wrong_current_mac_is_rejected(hass, config_entry, composite_id, mac):
    settings, mapping, source, *_ = retained_mapping(hass, config_entry)
    with registry_split(hass, source, composite_id=composite_id, mac=mac):
        with pytest.raises(SourceUnavailable, match="device_mismatch"):
            sample_envelope(hass, settings, mapping, dt_util.utcnow())


async def test_wrong_helper_provenance_is_rejected_after_a_valid_source_split(hass, config_entry):
    settings, mapping, source, power, *_ = retained_mapping(hass, config_entry)
    state = hass.states.get(power.entity_id)
    hass.states.async_set(power.entity_id, state.state, {
        **state.attributes, "source_device_id": "unrelated-device",
    })
    with registry_split(hass, source):
        with pytest.raises(SourceUnavailable, match="device_mismatch"):
            sample_envelope(hass, settings, mapping, dt_util.utcnow())


async def test_options_retain_legacy_helper_choices_and_validate_the_current_device(hass, config_entry):
    _, mapping, source, power, energy, inputs = retained_mapping(hass, config_entry)
    with registry_split(hass, source):
        choices = mapping_source_choices(hass, DEVICE)
        assert power.entity_id in choices["power_entity"]
        assert energy.entity_id in choices["energy_entity"]
        reviewed = build_mapping(hass, DEVICE, ATTRIBUTES, inputs, mapping)
    assert reviewed == replace(mapping, ha_device_id=source.device_id)


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_split_does_not_turn_missing_observations_into_readings(hass, config_entry, state):
    settings, mapping, source, power, *_ = retained_mapping(hass, config_entry)
    hass.states.async_set(power.entity_id, state, dict(hass.states.get(power.entity_id).attributes))
    with registry_split(hass, source):
        with pytest.raises(SourceUnavailable, match="unavailable"):
            sample_envelope(hass, settings, mapping, dt_util.utcnow())
