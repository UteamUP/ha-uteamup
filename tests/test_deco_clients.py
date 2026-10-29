"""Network isolation, truthful observations and additive asset metering."""

from dataclasses import asdict, replace
from datetime import timedelta
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uteamup.deco_clients import (
    CLIENT_TYPE_NAME, client_meter_definitions, deco_clients, matching_assets, prepare_client_meters,
)
from custom_components.uteamup.telemetry import SourceUnavailable, sample_network_envelope
from custom_components.uteamup.telemetry_config import (
    CLIENT_METRICS, NETWORK_METRICS, DecoClientMapping, NetworkTelemetryMapping, TelemetrySettings,
)
from .conftest import ASSET_GUID, TENANT
from .test_telemetry_config import DEVICE_GUID, DEVICE_KEY, ENDPOINT, ENVIRONMENT_GUID


def network(hass):
    entry = MockConfigEntry(domain="tplink_deco")
    entry.add_to_hass(hass)
    physical = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("tplink_deco", "router")})
    registry = er.async_get(hass)
    router = registry.async_get_or_create("device_tracker", "tplink_deco", "router", device_id=physical.id, config_entry=entry)
    hass.states.async_set(router.entity_id, "home", {"mac": "AA:BB:CC:DD:EE:00", "device_type": "deco"})
    entities = {}
    for name, domain, unit in NETWORK_METRICS:
        entity = registry.async_get_or_create(domain, "tplink_deco", name, device_id=physical.id, config_entry=entry)
        hass.states.async_set(entity.entity_id, "on" if domain == "binary_sensor" else "2", {"unit_of_measurement": unit})
        entities[f"{name}_registry_id"] = entity.id
    # Client trackers need not have a separate device entry (upstream's linking option).
    tracker = registry.async_get_or_create("device_tracker", "tplink_deco", "client", config_entry=entry)
    hass.states.async_set(tracker.entity_id, "home", {
        "mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.68.20", "device_type": "client",
        "friendly_name": "Lamp", "down_kilobytes_per_s": 12.5, "up_kilobytes_per_s": 0,
        "deco_device": "Living room", "secret": "must-not-leave-ha",
    })
    binding = DecoClientMapping(tracker.id, "aabbccddeeff", ASSET_GUID,
                               **{f"{name}_attribute_guid": str(uuid4()) for name, _ in CLIENT_METRICS})
    mapping = NetworkTelemetryMapping(DEVICE_GUID, str(uuid4()), "Deco", physical.id, "aabbccddee00", router.id,
                                     **entities, **{f"{name}_attribute_guid": str(uuid4()) for name, _, _ in NETWORK_METRICS},
                                     device_key=DEVICE_KEY, clients=(binding,))
    settings = TelemetrySettings(ENDPOINT, TENANT, ENVIRONMENT_GUID, (), network_mappings=(mapping,))
    return tracker, mapping, settings


async def test_gateway_exports_inventory_and_client_meters_without_energy_device_duplication(hass):
    tracker, mapping, settings = network(hass)
    body = json.loads(sample_network_envelope(hass, settings, mapping, dt_util.utcnow()).payload)
    row = body["metadata"]["networkClients"][0]
    assert (row["ipAddress"], row["assetGuid"], row["connected"]) == ("192.168.68.20", ASSET_GUID, True)
    assert body["metadata"]["totalConnectedClients"] == 1
    assert body["points"][2]["value"] == 2  # Selected node count is not silently relabelled as total.
    assert [(p["value"], p["unit"]) for p in body["points"] if p["assetGuid"] == ASSET_GUID] == [(12.5, "kB/s"), (0, "kB/s"), (1, "0/1")]
    assert "must-not-leave-ha" not in json.dumps(body)
    assert "remoteIp" not in json.dumps(body)
    assert row["sourceObservedAt"] == hass.states.get(tracker.entity_id).last_reported.isoformat()


@pytest.mark.parametrize("status,expected", [("not_home", [0]), ("unavailable", []), ("unknown", [])])
async def test_missing_and_offline_clients_do_not_reuse_old_rates(hass, status, expected):
    tracker, mapping, settings = network(hass)
    attributes = dict(hass.states.get(tracker.entity_id).attributes)
    hass.states.async_set(tracker.entity_id, status, attributes)
    body = json.loads(sample_network_envelope(hass, settings, mapping, dt_util.utcnow()).payload)
    assert [p["value"] for p in body["points"] if p["assetGuid"] == ASSET_GUID] == expected
    row = body["metadata"]["networkClients"][0]
    assert row["ipAddress"] is None
    assert row["downloadKilobytesPerSecond"] is None


async def test_stale_sources_and_changed_mac_cannot_create_new_client_readings(hass):
    tracker, mapping, settings = network(hass)
    with pytest.raises(SourceUnavailable, match="stale"):
        sample_network_envelope(hass, settings, mapping, dt_util.utcnow() + timedelta(minutes=11))
    attributes = {**hass.states.get(tracker.entity_id).attributes, "mac": "AA:BB:CC:DD:EE:01"}
    hass.states.async_set(tracker.entity_id, "home", attributes)
    body = json.loads(sample_network_envelope(hass, settings, mapping, dt_util.utcnow()).payload)
    assert all(point["assetGuid"] != ASSET_GUID for point in body["points"])


async def test_discovery_excludes_other_deco_integration_and_restored_rates(hass):
    tracker, mapping, _ = network(hass)
    other = MockConfigEntry(domain="tplink_deco")
    other.add_to_hass(hass)
    foreign = er.async_get(hass).async_get_or_create("device_tracker", "tplink_deco", "foreign", config_entry=other)
    hass.states.async_set(foreign.entity_id, "home", {"device_type": "client", "mac": "001122334455"})
    hass.states.async_set(tracker.entity_id, "home", {**hass.states.get(tracker.entity_id).attributes, "restored": True})
    rows = deco_clients(hass, mapping.tracker_registry_id, dt_util.utcnow())
    assert len(rows) == 1 and rows[0]["connected"] is None


def test_asset_suggestions_require_exact_mac_and_surface_ambiguity():
    row = {"macAddress": "aabbccddeeff", "name": "Lamp", "ipAddress": "192.168.68.20"}
    assets = [{"serialNumber": "AA-BB-CC-DD-EE-FF"}, {"referenceNumber": "MAC AA:BB:CC:DD:EE:FF"},
              {"name": "Lamp", "serialNumber": "192.168.68.20"}]
    assert matching_assets(row, assets) == assets[:2]


async def test_saved_client_mapping_roundtrip_and_duplicate_asset_rejection(hass):
    _, mapping, settings = network(hass)
    saved = asdict(settings)
    assert TelemetrySettings.from_dict(saved, TENANT) == settings
    mapping = replace(mapping, clients=(mapping.clients[0], replace(mapping.clients[0], tracker_registry_id=str(uuid4()))))
    with pytest.raises(ValueError, match="device_mismatch"):
        TelemetrySettings.from_dict(asdict(replace(settings, network_mappings=(mapping,))), TENANT)


async def test_missing_meters_are_added_and_existing_asset_types_preserved():
    client = AsyncMock()
    type_guid, primary, extra = str(uuid4()), str(uuid4()), str(uuid4())
    definitions = [{**item, "externalGuid": str(uuid4()), "isActive": True} for item in client_meter_definitions()]
    client.async_get_asset.return_value = {"assetTypeGuids": [primary, extra], "primaryAssetTypeGuid": primary, "updatedAt": "2026-01-01T00:00:00Z"}
    client.async_get_asset_types.return_value = [{"name": CLIENT_TYPE_NAME, "externalGuid": type_guid, "isActive": True, "isSystemType": False}]
    client.async_get_asset_type.return_value = {"externalGuid": type_guid, "attributeDefinitions": definitions[:2]}
    client.async_add_asset_type_attribute.return_value = definitions[2]
    meters = await prepare_client_meters(client, ASSET_GUID)
    assert meters["connected_attribute_guid"] == definitions[2]["externalGuid"]
    client.async_create_asset_type.assert_not_awaited()
    assert client.async_patch_asset.await_args.args[1] == {"assetTypeGuids": [primary, extra, type_guid], "primaryAssetTypeGuid": primary, "expectedUpdatedAtUtc": "2026-01-01T00:00:00Z"}


async def test_disabled_existing_meter_is_not_silently_reenabled():
    client = AsyncMock()
    type_guid = str(uuid4())
    client.async_get_asset.return_value = {}
    client.async_get_asset_types.return_value = [{"name": CLIENT_TYPE_NAME, "externalGuid": type_guid, "isActive": True, "isSystemType": False}]
    client.async_get_asset_type.return_value = {"externalGuid": type_guid, "attributeDefinitions": [
        {**item, "externalGuid": str(uuid4()), "isActive": False} for item in client_meter_definitions()]}
    with pytest.raises(ValueError, match="attribute_mismatch"):
        await prepare_client_meters(client, ASSET_GUID)
    client.async_patch_asset.assert_not_awaited()


async def test_existing_complete_type_and_link_are_idempotent():
    client = AsyncMock()
    type_guid = str(uuid4())
    definitions = [{**item, "externalGuid": str(uuid4()), "isActive": True} for item in client_meter_definitions()]
    client.async_get_asset.return_value = {"assetTypeGuids": [type_guid]}
    client.async_get_asset_types.return_value = [{"name": CLIENT_TYPE_NAME, "externalGuid": type_guid, "isActive": True, "isSystemType": False}]
    client.async_get_asset_type.return_value = {"externalGuid": type_guid, "attributeDefinitions": definitions}
    assert len(await prepare_client_meters(client, ASSET_GUID)) == 3
    client.async_patch_asset.assert_not_awaited()
    client.async_add_asset_type_attribute.assert_not_awaited()
    client.async_create_asset_type.assert_not_awaited()


async def test_new_custom_type_contains_all_three_meters_before_linking():
    client = AsyncMock()
    type_guid, primary = str(uuid4()), str(uuid4())
    client.async_get_asset.return_value = {"primaryAssetTypeGuid": primary, "updatedAt": "2026-10-29T12:00:00Z"}
    client.async_get_asset_types.return_value = []
    client.async_create_asset_type.return_value = {"externalGuid": type_guid, "attributeDefinitions": [
        {**item, "externalGuid": str(uuid4()), "isActive": True} for item in client_meter_definitions()]}
    await prepare_client_meters(client, ASSET_GUID)
    request = client.async_create_asset_type.await_args.args[0]
    assert len(request["attributeDefinitions"]) == 3
    assert all(item["isMeter"] and item["allowIoTUpdates"] for item in request["attributeDefinitions"])
    assert client.async_patch_asset.await_args.args[1]["assetTypeGuids"] == [primary, type_guid]
