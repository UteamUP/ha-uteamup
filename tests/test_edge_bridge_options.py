"""A reviewed LAN route changes only selected devices and never exposes its token."""

from dataclasses import asdict, replace
from unittest.mock import patch
from uuid import uuid4

import pytest
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.uteamup.const import (
    CONF_EDGE_BRIDGE_DEVICES, CONF_EDGE_BRIDGE_ENDPOINT, CONF_EDGE_BRIDGE_TOKEN,
    CONF_ENABLE_TELEMETRY, CONF_ENABLE_WEBHOOKS, CONF_TELEMETRY,
)
from custom_components.uteamup.telemetry_config import TelemetrySettings

from .conftest import BASE, TENANT, mock_login
from .test_measurements import configured_matter
from .test_telemetry_config import DEVICE, DEVICE_GUID, environment_response
from .test_telemetry_options import renderable

TOKEN = "home-assistant-intake-token-0123456789abcdef"
ENDPOINT = "http://192.168.68.13:41080"


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.uteamup.async_setup_entry", return_value=True):
        yield


async def begin(hass, config_entry, aioclient_mock, saved=None):
    if saved is None:
        settings, _, _ = configured_matter(hass, config_entry)
        saved = asdict(settings)
    hass.config_entries.async_update_entry(config_entry, options={CONF_TELEMETRY: saved})
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/iot/environment", json=environment_response())
    aioclient_mock.get(f"{BASE}/api/iot/devices", json={"items": [DEVICE], "totalItems": 1})
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    return await hass.config_entries.options.async_configure(result["flow_id"], {
        CONF_SCAN_INTERVAL: 180, CONF_ENABLE_WEBHOOKS: False, CONF_ENABLE_TELEMETRY: True,
        "configure_edge_bridge": True,
    })


async def test_route_is_explicit_preserves_mappings_and_never_echoes_token(hass, config_entry, aioclient_mock):
    result = await begin(hass, config_entry, aioclient_mock)
    original = config_entry.options[CONF_TELEMETRY]
    assert result["step_id"] == "edge_bridge"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "devices": [DEVICE_GUID], CONF_EDGE_BRIDGE_ENDPOINT: ENDPOINT, CONF_EDGE_BRIDGE_TOKEN: TOKEN,
    })
    assert result["type"] == FlowResultType.CREATE_ENTRY
    saved = config_entry.options[CONF_TELEMETRY]
    settings = TelemetrySettings.from_dict(saved, TENANT)
    assert settings.edge_bridge_devices == (DEVICE_GUID,)
    assert saved[CONF_EDGE_BRIDGE_TOKEN] == TOKEN
    assert saved["measurement_mappings"] == original["measurement_mappings"]
    # Reopening can reuse the credential without including it in the rendered form.
    result = await begin(hass, config_entry, aioclient_mock, saved)
    assert TOKEN not in str(renderable(result))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "devices": [DEVICE_GUID], CONF_EDGE_BRIDGE_ENDPOINT: ENDPOINT,
    })
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_TELEMETRY][CONF_EDGE_BRIDGE_TOKEN] == TOKEN


@pytest.mark.parametrize("changes", [
    {"devices": [str(uuid4())]}, {"devices": [DEVICE_GUID, DEVICE_GUID]},
    {CONF_EDGE_BRIDGE_ENDPOINT: "https://public.example.test"}, {CONF_EDGE_BRIDGE_TOKEN: "short"},
])
async def test_unreviewed_or_invalid_route_does_not_change_saved_settings(hass, config_entry, aioclient_mock, changes):
    result = await begin(hass, config_entry, aioclient_mock)
    original = config_entry.options[CONF_TELEMETRY]
    inputs = {
        "devices": [DEVICE_GUID], CONF_EDGE_BRIDGE_ENDPOINT: ENDPOINT, CONF_EDGE_BRIDGE_TOKEN: TOKEN, **changes,
    }
    if any(device != DEVICE_GUID for device in inputs["devices"]):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(result["flow_id"], inputs)
        assert config_entry.options[CONF_TELEMETRY] == original
        return
    result = await hass.config_entries.options.async_configure(result["flow_id"], inputs)
    assert result["errors"] == {"base": "invalid_mapping"}
    assert TOKEN not in str(renderable(result))
    assert config_entry.options[CONF_TELEMETRY] == original


async def test_removing_route_requires_a_cloud_key_for_every_retained_mapping(hass, config_entry, aioclient_mock):
    settings, mapping, _ = configured_matter(hass, config_entry)
    saved = asdict(replace(settings, measurement_mappings=(replace(mapping, device_key=""),),
                          edge_bridge_devices=(DEVICE_GUID,), edge_bridge_endpoint=ENDPOINT,
                          edge_bridge_token=TOKEN))
    result = await begin(hass, config_entry, aioclient_mock, saved)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"devices": []})
    assert result["errors"] == {"base": "invalid_mapping"}
    assert config_entry.options[CONF_TELEMETRY] == saved
