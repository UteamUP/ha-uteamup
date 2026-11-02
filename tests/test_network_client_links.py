"""Persisted client associations override stale local mappings without inventing devices."""

from dataclasses import asdict, replace
import hashlib
import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from homeassistant.util import dt as dt_util
from homeassistant.helpers import entity_registry as er

from custom_components.uteamup.api import UteamupConnectionError
from custom_components.uteamup.deco_clients import resolved_client_mappings
from custom_components.uteamup.telemetry import UteamupTelemetry, sample_network_envelope
from custom_components.uteamup.telemetry_config import CLIENT_METRICS, TelemetrySettings
from .test_deco_clients import network


def server_link(mapping, state="linked"):
    client = mapping.clients[0]
    return {
        "linkGuid": str(uuid4()), "revision": str(uuid4()), "gatewayDeviceGuid": mapping.device_guid,
        "macAddress": client.mac_address, "assetGuid": client.asset_guid, "state": state,
        "downloadAttributeGuid": client.download_attribute_guid,
        "uploadAttributeGuid": client.upload_attribute_guid,
        "connectedAttributeGuid": client.connected_attribute_guid,
    }


@pytest.mark.parametrize("kind", ["part", "tool"])
async def test_part_and_tool_links_export_own_meter_owner_and_capture_association_revision(hass, kind):
    _, mapping, settings = network(hass)
    link = {**server_link(mapping), "assetGuid": None, f"{kind}Guid": str(uuid4())}
    resolved = replace(mapping, clients=resolved_client_mappings(hass, mapping, dt_util.utcnow(), [link]))

    envelope = sample_network_envelope(hass, settings, mapping, dt_util.utcnow(), resolved)
    body = json.loads(envelope.payload)

    points = [point for point in body["points"] if f"{kind}Guid" in point]
    assert len(points) == 3
    assert all(point[f"{kind}Guid"] == link[f"{kind}Guid"] and "assetGuid" not in point for point in points)
    assert {point["attributeDefinitionGuid"] for point in points} == {
        link["downloadAttributeGuid"], link["uploadAttributeGuid"], link["connectedAttributeGuid"],
    }
    assert all(point["metadata"]["networkClientLinkGuid"] == link["linkGuid"] for point in points)
    assert all(point["metadata"]["networkClientLinkRevision"] == link["revision"] for point in points)
    row = body["metadata"]["networkClients"][0]
    assert row["assetGuid"] is None and row[f"{kind}Guid"] == link[f"{kind}Guid"]
    assert envelope.mapping_fingerprint == mapping.fingerprint
    link[f"{kind}Guid"] = str(uuid4())
    assert json.loads(envelope.payload)["points"][-1][f"{kind}Guid"] != link[f"{kind}Guid"]


@pytest.mark.parametrize("defect", ["missing", "multiple", "empty", "wrong_kind", "missing_revision"])
async def test_server_resource_links_require_exactly_one_valid_owner_and_revision(hass, defect):
    _, mapping, _ = network(hass)
    link = server_link(mapping)
    if defect == "missing":
        link["assetGuid"] = None
    elif defect == "multiple":
        link["partGuid"] = str(uuid4())
    elif defect == "empty":
        link["assetGuid"] = None
        link["toolGuid"] = str(UUID(int=0))
    elif defect == "wrong_kind":
        link["assetGuid"] = None
        link["partGuid"] = "invalid"
    else:
        link["assetGuid"] = None
        link["toolGuid"] = str(uuid4())
        link.pop("revision")
    with pytest.raises(ValueError):
        resolved_client_mappings(hass, mapping, dt_util.utcnow(), [link])


async def test_two_clients_can_supply_independent_meters_to_one_existing_asset(hass):
    tracker, mapping, settings = network(hass)
    registry = er.async_get(hass)
    entry = hass.config_entries.async_get_entry(registry.async_get(tracker.entity_id).config_entry_id)
    second = registry.async_get_or_create("device_tracker", "tplink_deco", "second-client", config_entry=entry)
    hass.states.async_set(second.entity_id, "home", {
        **hass.states.get(tracker.entity_id).attributes, "mac": "AA:BB:CC:DD:EE:01", "down_kilobytes_per_s": 25,
    })
    first_link = server_link(mapping)
    second_link = {**server_link(mapping), "macAddress": "aabbccddee01",
                   **{f"{name}AttributeGuid": str(uuid4()) for name, _ in CLIENT_METRICS}}
    resolved = replace(mapping, clients=resolved_client_mappings(hass, mapping, dt_util.utcnow(), [first_link, second_link]))

    body = json.loads(sample_network_envelope(hass, settings, mapping, dt_util.utcnow(), resolved).payload)

    points = [point for point in body["points"] if point.get("assetGuid") == first_link["assetGuid"]]
    assert len(points) == 6
    assert len({point["attributeDefinitionGuid"] for point in points}) == 6
    assert {point["value"] for point in points if point["unit"] == "kB/s"} == {0, 12.5, 25}
    with pytest.raises(ValueError, match="invalid_client_links"):
        resolved_client_mappings(hass, mapping, dt_util.utcnow(), [first_link, {
            **second_link, "downloadAttributeGuid": first_link["downloadAttributeGuid"],
        }])


async def test_legacy_asset_queue_fingerprint_and_independent_resource_mapping_roundtrip(hass):
    _, mapping, settings = network(hass)
    legacy = asdict(mapping)
    legacy.pop("name")
    legacy.pop("device_key")
    for client in legacy["clients"]:
        for key in ("part_guid", "tool_guid", "link_guid", "link_revision"):
            client.pop(key)
    assert mapping.fingerprint == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    second = replace(mapping.clients[0], tracker_registry_id=str(uuid4()), mac_address="aabbccddee01",
                     **{f"{name}_attribute_guid": str(uuid4()) for name, _ in CLIENT_METRICS})
    configured = replace(settings, network_mappings=(replace(mapping, clients=(*mapping.clients, second)),))
    assert TelemetrySettings.from_dict(asdict(configured), configured.tenant_guid) == configured


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


async def test_ip_only_links_cannot_authorize_mac_readings_or_break_existing_links(hass):
    _, mapping, _ = network(hass)
    ip_link = {**server_link(mapping), "macAddress": "", "ipAddress": "192.168.68.20"}
    assert not resolved_client_mappings(hass, mapping, dt_util.utcnow(), [ip_link])
    assert len(resolved_client_mappings(hass, mapping, dt_util.utcnow(), [ip_link, server_link(mapping)])) == 1
    with pytest.raises(ValueError, match="invalid_client_links"):
        resolved_client_mappings(hass, mapping, dt_util.utcnow(), [ip_link, ip_link])


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
    client.async_get_network_client_links.return_value = [link, {**link, "macAddress": "", "ipAddress": "192.168.68.20"}]
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
