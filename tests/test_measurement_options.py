"""Actual options flow preserves other integrations and reviews each asset measurement."""

from dataclasses import asdict, replace
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.data_entry_flow import FlowResultType

from custom_components.uteamup.const import CONF_ENABLE_TELEMETRY, CONF_ENABLE_WEBHOOKS, CONF_TELEMETRY
from .conftest import ASSET_GUID, BASE, TENANT, mock_login
from .test_measurements import configured
from .test_telemetry_config import (DEVICE, DEVICE_KEY, ASSET_TYPE_GUID, ENVIRONMENT_GUID, ATTRIBUTES,
                                   environment_response, register_sources)
from custom_components.uteamup.telemetry_config import build_mapping
from .test_telemetry_options import renderable


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.uteamup.async_setup_entry", return_value=True):
        yield


async def test_measurement_flow_saves_reviewed_meters_without_changing_existing_options(hass, config_entry, aioclient_mock):
    settings, mapping, anchor, battery, filter_life = configured(hass, config_entry)
    saved = asdict(settings)
    *_, energy_input = register_sources(hass, config_entry)
    old_energy = replace(build_mapping(hass, DEVICE, ATTRIBUTES, energy_input), device_guid=ENVIRONMENT_GUID)
    saved["mappings"] = [asdict(old_energy)]
    # Previous generic association is editable with the stored key, which must never appear in a form.
    options = {CONF_TELEMETRY: saved, CONF_ENABLE_WEBHOOKS: False, CONF_SCAN_INTERVAL: 180}
    hass.config_entries.async_update_entry(config_entry, options=options)
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/iot/environment", json=environment_response())
    aioclient_mock.get(f"{BASE}/api/iot/devices", json={"items": [DEVICE], "totalItems": 1})
    definitions = [{"attributeDefinitionGuid": reading.attribute_guid, "externalGuid": reading.attribute_guid,
                    "name": name, "isActive": True, "isMeter": True, "allowIoTUpdates": True, "dataType": 3, "unit": "%"}
                   for reading, name in zip(mapping.readings, ("Battery", "Filter life"), strict=True)]
    aioclient_mock.get(f"{BASE}/api/asset/by-guid/{ASSET_GUID}/effective-attribute-definitions",
                       json=[{"assetTypeGuid": ASSET_TYPE_GUID, "definitions": definitions}])
    aioclient_mock.get(f"{BASE}/api/asset-type/{ASSET_TYPE_GUID}/with-inherited-attributes",
                       json={"isActive": True, "attributeDefinitions": definitions})
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        CONF_SCAN_INTERVAL: 180, CONF_ENABLE_WEBHOOKS: False, CONF_ENABLE_TELEMETRY: True,
        "configure_measurements": True,
    })
    assert result["step_id"] == "measurement_device"
    assert DEVICE_KEY not in str(renderable(result))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "device_guid": DEVICE["externalGuid"], "source_entity": anchor.entity_id,
    })
    assert result["step_id"] == "measurement_sources"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"entities": [battery.entity_id, filter_life.entity_id]})
    for reading in mapping.readings:
        assert result["step_id"] == "measurement_meter"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"attribute_guid": reading.attribute_guid})
    assert result["step_id"] == "measurement_confirm"
    assert result["description_placeholders"]["asset"] == ASSET_GUID
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_SCAN_INTERVAL] == 180
    assert config_entry.options[CONF_TELEMETRY]["measurement_mappings"] == [asdict(replace(mapping, name=DEVICE["name"]))]
    assert config_entry.options[CONF_TELEMETRY]["mappings"] == saved["mappings"]
    assert config_entry.options[CONF_TELEMETRY]["network_mappings"] == saved["network_mappings"]
    assert "configure_measurements" not in config_entry.options
