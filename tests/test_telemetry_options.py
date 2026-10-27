"""The telemetry options use friendly selectors and retain existing settings."""

from dataclasses import asdict
from unittest.mock import patch

import pytest
import voluptuous_serialize

from homeassistant.const import CONF_SCAN_INTERVAL, STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from custom_components.uteamup.const import CONF_ENABLE_TELEMETRY, CONF_ENABLE_WEBHOOKS, CONF_TELEMETRY
from custom_components.uteamup.telemetry import SourceUnavailable, sample_envelope
from custom_components.uteamup.telemetry_config import TelemetrySettings, build_mapping, environment_settings

from .conftest import BASE, TENANT, mock_login
from .test_telemetry_config import (
    ASSET_GUID, ASSET_TYPE, ASSET_TYPE_GUID, ATTRIBUTES, DEVICE, DEVICE_GUID, DEVICE_KEY, ENDPOINT,
    environment_response, register_socket_sources, register_sources,
)


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.uteamup.async_setup_entry", return_value=True):
        yield


def renderable(result):
    return voluptuous_serialize.convert(result["data_schema"], custom_serializer=cv.custom_serializer)


async def begin(hass, config_entry, aioclient_mock, environment=None, asset_type=None):
    config_entry.add_to_hass(hass)
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/iot/environment", json=environment or environment_response())
    aioclient_mock.get(f"{BASE}/api/iot/devices", json={"items": [DEVICE], "totalItems": 1})
    aioclient_mock.get(f"{BASE}/api/asset/by-guid/{ASSET_GUID}/effective-attribute-definitions", json=ATTRIBUTES)
    aioclient_mock.get(f"{BASE}/api/asset-type/{ASSET_TYPE_GUID}/with-inherited-attributes", json=asset_type or ASSET_TYPE)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    return await hass.config_entries.options.async_configure(result["flow_id"], {
        CONF_SCAN_INTERVAL: 60, CONF_ENABLE_WEBHOOKS: False, CONF_ENABLE_TELEMETRY: True,
    })


async def test_complete_mapping_flow_keeps_secret_out_of_form_defaults(hass, config_entry, aioclient_mock):
    result = await begin(hass, config_entry, aioclient_mock)
    assert result["step_id"] == "telemetry_devices"
    assert DEVICE["name"] in str(renderable(result))
    _, _, _, inputs = register_sources(hass, config_entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"devices": [DEVICE_GUID]})
    assert result["step_id"] == "telemetry_mapping"
    assert "password" in str(renderable(result))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**inputs, "estimate_profile": ""})
    assert result["errors"] == {"base": "invalid_mapping"}
    assert DEVICE_KEY not in str(renderable(result))
    result = await hass.config_entries.options.async_configure(result["flow_id"], inputs)
    assert result["step_id"] == "telemetry_confirm"
    assert result["description_placeholders"]["endpoint"] == ENDPOINT
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_ENABLE_WEBHOOKS] is False
    assert config_entry.options[CONF_TELEMETRY]["mappings"][0]["device_key"] == DEVICE_KEY


async def test_measured_socket_can_be_selected_and_saved_without_an_estimate_profile(hass, config_entry, aioclient_mock):
    result = await begin(hass, config_entry, aioclient_mock)
    socket, _, _, inputs = register_socket_sources(hass, config_entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"devices": [DEVICE_GUID]})
    rendered = renderable(result)
    source = next(field for field in rendered if field["name"] == "source_entity")
    assert source["selector"]["entity"]["domain"] == ["light", "switch"]
    assert source["selector"]["entity"]["include_entities"] == [socket.entity_id]
    result = await hass.config_entries.options.async_configure(result["flow_id"], inputs)
    assert result["step_id"] == "telemetry_confirm"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    mapping = config_entry.options[CONF_TELEMETRY]["mappings"][0]
    assert mapping["power_basis"] == "measured"
    assert mapping["estimate_profile"] == ""


async def test_options_can_keep_an_offline_device_without_emitting_readings(hass, config_entry, aioclient_mock):
    result = await begin(hass, config_entry, aioclient_mock)
    light, power, energy, inputs = register_sources(hass, config_entry)
    previous = build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    saved = {**environment_settings(environment_response(), TENANT), "mappings": [asdict(previous)]}
    hass.config_entries.async_update_entry(config_entry, options={CONF_TELEMETRY: saved})
    for entity in (light, power, energy):
        hass.states.async_set(entity.entity_id, STATE_UNAVAILABLE)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"devices": [DEVICE_GUID]})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**inputs, "device_key": ""})
    assert result["step_id"] == "telemetry_confirm"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    settings = TelemetrySettings.from_dict(config_entry.options[CONF_TELEMETRY], TENANT)
    assert settings.mappings[0] == previous
    with pytest.raises(SourceUnavailable, match="unavailable"):
        sample_envelope(hass, settings, settings.mappings[0], dt_util.utcnow())


async def test_unhealthy_environment_cannot_reach_mapping_step(hass, config_entry, aioclient_mock):
    environment = environment_response()
    environment["environment"]["isHealthy"] = False
    result = await begin(hass, config_entry, aioclient_mock, environment)
    assert result["errors"] == {"base": "environment_unavailable"}
    assert not any("telemetry/batch" in str(call[1]) for call in aioclient_mock.mock_calls)


async def test_non_iot_meter_is_hidden_from_attribute_selector(hass, config_entry, aioclient_mock):
    disabled = {**ASSET_TYPE, "attributeDefinitions": [
        {**item, "allowIoTUpdates": item["unit"] == "kWh"} for item in ASSET_TYPE["attributeDefinitions"]
    ]}
    result = await begin(hass, config_entry, aioclient_mock, asset_type=disabled)
    register_sources(hass, config_entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"devices": [DEVICE_GUID]})
    rendered = str(renderable(result))
    assert "Estimated power" not in rendered
    assert "Cumulative estimated energy" in rendered


async def test_disabling_telemetry_needs_no_cmms_connection_and_retains_mappings(hass, config_entry):
    config_entry.add_to_hass(hass)
    saved = {"mappings": [{"device_guid": DEVICE_GUID}]}
    hass.config_entries.async_update_entry(config_entry, options={CONF_TELEMETRY: saved, CONF_ENABLE_TELEMETRY: True})
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        CONF_SCAN_INTERVAL: 60, CONF_ENABLE_WEBHOOKS: False, CONF_ENABLE_TELEMETRY: False,
    })
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_TELEMETRY] == saved
