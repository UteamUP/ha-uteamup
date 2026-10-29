"""Persisted client associations override stale local mappings without inventing devices."""

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from homeassistant.util import dt as dt_util

from custom_components.uteamup.api import UteamupConnectionError
from custom_components.uteamup.deco_clients import resolved_client_mappings
from custom_components.uteamup.telemetry import UteamupTelemetry, sample_network_envelope
from .test_deco_clients import network


def server_link(mapping, state="linked"):
    client = mapping.clients[0]
    return {
        "linkGuid": str(uuid4()), "gatewayDeviceGuid": mapping.device_guid,
        "macAddress": client.mac_address, "assetGuid": client.asset_guid, "state": state,
        "downloadAttributeGuid": client.download_attribute_guid,
        "uploadAttributeGuid": client.upload_attribute_guid,
        "connectedAttributeGuid": client.connected_attribute_guid,
    }


@pytest.mark.parametrize("state", ["blocked", "unavailable"])
async def test_server_unlink_suppresses_saved_ha_mapping_but_keeps_inventory(hass, state):
    _, mapping, settings = network(hass)
    now = dt_util.utcnow()
    resolved = replace(mapping, clients=resolved_client_mappings(hass, mapping, now, [server_link(mapping, state)]))
    import json
    body = json.loads(sample_network_envelope(hass, settings, mapping, now, resolved).payload)
    assert body["metadata"]["networkClients"][0]["assetGuid"] is None
    assert len(body["points"]) == 5
    assert {point["assetGuid"] for point in body["points"]} == {mapping.asset_guid}


async def test_server_link_can_target_existing_asset_with_new_meters_without_new_device(hass):
    _, mapping, _ = network(hass)
    link = server_link(mapping)
    link["assetGuid"] = str(uuid4())
    result = resolved_client_mappings(hass, mapping, dt_util.utcnow(), [link])
    assert len(result) == 1 and result[0].asset_guid == link["assetGuid"]
    assert result[0].tracker_registry_id == mapping.clients[0].tracker_registry_id


@pytest.mark.parametrize("invalid", ["foreign", "duplicate", "missing_meter"])
async def test_foreign_duplicate_and_incomplete_responses_fail_closed(hass, invalid):
    _, mapping, _ = network(hass)
    link = server_link(mapping)
    if invalid == "foreign":
        link["gatewayDeviceGuid"] = str(uuid4())
    if invalid == "missing_meter":
        link.pop("downloadAttributeGuid")
    with pytest.raises(ValueError):
        resolved_client_mappings(hass, mapping, dt_util.utcnow(), [link, link] if invalid == "duplicate" else [link])


async def test_runtime_auto_matches_only_unique_existing_device_and_persists_once(hass, config_entry):
    _, mapping, settings = network(hass)
    link = server_link(mapping)
    client = AsyncMock()
    client.async_get_network_client_links.return_value = []
    client.async_get_iot_devices.return_value = [{
        "externalGuid": str(uuid4()), "macAddress": "AA:BB:CC:DD:EE:FF", "isActive": True,
        "assetGuid": link["assetGuid"],
    }]
    client.async_link_network_client.return_value = link
    unmapped = replace(mapping, clients=())
    runtime = UteamupTelemetry(hass, config_entry, replace(settings, network_mappings=(unmapped,)), client)
    resolved = await runtime._async_resolve_clients(unmapped, dt_util.utcnow())
    assert len(resolved.clients) == 1
    client.async_link_network_client.assert_awaited_once_with(mapping.device_guid, {
        "macAddress": "aabbccddeeff", "autoMatch": True,
    })
    client.async_get_network_client_links.return_value = [link]
    await runtime._async_resolve_clients(unmapped, dt_util.utcnow())
    assert client.async_link_network_client.await_count == 1


async def test_runtime_ambiguous_match_and_unavailable_api_do_not_emit_client_readings(hass, config_entry):
    _, mapping, settings = network(hass)
    client = AsyncMock()
    client.async_get_network_client_links.return_value = []
    client.async_get_iot_devices.return_value = [
        {"externalGuid": str(uuid4()), "macAddress": "aabbccddeeff", "isActive": True, "assetGuid": str(uuid4())}
        for _ in range(2)
    ]
    unmapped = replace(mapping, clients=())
    runtime = UteamupTelemetry(hass, config_entry, settings, client)
    assert not (await runtime._async_resolve_clients(unmapped, dt_util.utcnow())).clients
    client.async_link_network_client.assert_not_awaited()
    client.async_get_network_client_links.side_effect = UteamupConnectionError("offline")
    assert not (await runtime._async_resolve_clients(mapping, dt_util.utcnow())).clients
    assert runtime.health.client_links[mapping.device_guid] == "unavailable"
