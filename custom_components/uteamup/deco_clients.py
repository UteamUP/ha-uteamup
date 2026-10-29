"""Deco client observations and explicitly reviewed links to existing tenant assets."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .telemetry_config import CLIENT_METRICS, DecoClientMapping, compatible_meter, normalize_mac, public_guid

if TYPE_CHECKING:
    from .api import UteamupClient
    from .telemetry_config import NetworkTelemetryMapping

MAX_SOURCE_AGE = timedelta(minutes=10)
CLIENT_TYPE_NAME = "Deco client telemetry"
CLIENT_FIELDS = {
    "download": ("deco_client_download_kilobytes_per_s", "Client download"),
    "upload": ("deco_client_upload_kilobytes_per_s", "Client upload"),
    "connected": ("deco_client_connected", "Client connected"),
}


def _rate(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 1e15 else None
    except (TypeError, ValueError):
        return None


def deco_clients(hass: HomeAssistant, router_registry_id: str, now: datetime) -> list[dict[str, Any]]:
    """Scope discovery to the router's Deco integration; never copy arbitrary attributes."""
    registry = er.async_get(hass)
    router = registry.async_get(router_registry_id)
    if router is None or not router.config_entry_id:
        return []
    clients = []
    for entity in registry.entities.values():
        if (entity.platform != "tplink_deco" or entity.domain != "device_tracker" or entity.disabled
                or entity.config_entry_id != router.config_entry_id):
            continue
        state = hass.states.get(entity.entity_id)
        if state is None or state.attributes.get("device_type") != "client":
            continue
        try:
            mac = normalize_mac(state.attributes.get("mac"))
        except ValueError:
            continue
        current = (not state.attributes.get("restored")
                   and timedelta(0) <= now - state.last_reported <= MAX_SOURCE_AGE)
        connected = (state.state == "home") if current and state.state in {"home", "not_home"} else None
        try:
            address = str(ip_address(state.attributes.get("ip"))) if connected else None
        except ValueError:
            address = None
        clients.append({
            "trackerRegistryId": entity.id,
            "macAddress": mac,
            "name": str(state.name)[:200],
            "ipAddress": address,
            "connected": connected,
            "downloadKilobytesPerSecond": _rate(state.attributes.get("down_kilobytes_per_s")) if connected else None,
            "uploadKilobytesPerSecond": _rate(state.attributes.get("up_kilobytes_per_s")) if connected else None,
            "connectionType": str(state.attributes.get("connection_type") or "")[:100] or None,
            "decoName": str(state.attributes.get("deco_device") or "")[:200] or None,
            "decoMac": str(state.attributes.get("deco_mac") or "")[:32] or None,
            "sourceObservedAt": state.last_reported.isoformat(),
            "assetGuid": None,
        })
    return sorted(clients, key=lambda item: (item["name"].casefold(), item["macAddress"]))


def matching_assets(client: dict[str, Any], assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only exact MACs are suggestions; hostnames and IPs are never identity evidence."""
    matches = []
    for asset in assets:
        candidates = [asset.get("serialNumber"), asset.get("macAddress")]
        candidates.extend(re.findall(r"(?<![\w])(?:[\da-fA-F]{2}[:-]){5}[\da-fA-F]{2}(?![\w])",
                                     str(asset.get("referenceNumber") or "")))
        for candidate in candidates:
            try:
                if normalize_mac(candidate) == client["macAddress"]:
                    matches.append(asset)
                    break
            except ValueError:
                continue
    return matches


def client_meter_definitions() -> list[dict[str, Any]]:
    return [{
        "name": CLIENT_FIELDS[name][1], "fieldKey": CLIENT_FIELDS[name][0], "dataType": 3,
        "unit": unit, "isMeter": True, "allowIoTUpdates": True, "minReadingIntervalSeconds": 60,
        "displayOrder": index, "groupName": "Network", "isRequired": False,
    } for index, (name, unit) in enumerate(CLIENT_METRICS)]


async def prepare_client_meters(client: UteamupClient, asset_guid: str) -> dict[str, str]:
    """Add only missing definitions to a tenant custom type and preserve all existing types."""
    asset = await client.async_get_asset(asset_guid)
    types = await client.async_get_asset_types()
    candidates = [item for item in types if item.get("name") == CLIENT_TYPE_NAME
                  and item.get("isSystemType") is False and item.get("isActive") is True]
    if len(candidates) > 1:
        raise ValueError("ambiguous_client_type")
    if candidates:
        asset_type = await client.async_get_asset_type(candidates[0]["externalGuid"])
    else:
        asset_type = await client.async_create_asset_type({
            "name": CLIENT_TYPE_NAME, "categoryGroup": 10, "isMetered": True,
            "description": "Reported Deco client rates and connectivity. DNS queries and remote flows are not supplied by Deco.",
            "attributeDefinitions": client_meter_definitions(),
        })
    type_guid = public_guid(asset_type["externalGuid"])
    definitions = {item["fieldKey"]: item for item in asset_type.get("attributeDefinitions", [])}
    meters = {}
    for requested in client_meter_definitions():
        definition = definitions.get(requested["fieldKey"])
        if definition is None:
            definition = await client.async_add_asset_type_attribute(type_guid, requested)
        if not compatible_meter(definition, requested["unit"]):
            raise ValueError("attribute_mismatch")
        name = next(name for name, (key, _) in CLIENT_FIELDS.items() if key == requested["fieldKey"])
        meters[f"{name}_attribute_guid"] = public_guid(definition["externalGuid"])
    assigned = list(asset.get("assetTypeGuids") or [])
    primary = asset.get("primaryAssetTypeGuid") or asset.get("assetTypeGuid")
    if primary and primary not in assigned:
        assigned.append(primary)
    if type_guid not in assigned:
        await client.async_patch_asset(asset_guid, {
            "assetTypeGuids": [*assigned, type_guid],
            **({"primaryAssetTypeGuid": primary} if primary else {}),
            "expectedUpdatedAtUtc": asset.get("updatedAt"),
        })
    return meters


def network_client_observations(
    hass: HomeAssistant, mapping: NetworkTelemetryMapping, now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    clients = deco_clients(hass, mapping.tracker_registry_id, now)
    bindings = {item.tracker_registry_id: item for item in mapping.clients}
    points = []
    for client in clients:
        binding = bindings.get(client["trackerRegistryId"])
        if binding is None or client["macAddress"] != binding.mac_address:
            continue
        client["assetGuid"] = binding.asset_guid
        if client["connected"] is None:
            continue
        values = {"connected": float(client["connected"]),
                  "download": client["downloadKilobytesPerSecond"], "upload": client["uploadKilobytesPerSecond"]}
        for name, unit in CLIENT_METRICS:
            if values[name] is None:
                continue
            points.append({
                "pointId": str(uuid4()), "assetGuid": binding.asset_guid,
                "attributeDefinitionGuid": getattr(binding, f"{name}_attribute_guid"),
                "value": values[name], "observedAt": client["sourceObservedAt"], "unit": unit, "quality": "measured",
                "metadata": {"networkClientMac": client["macAddress"]},
            })
    return clients, points


def resolved_client_mappings(
    hass: HomeAssistant, mapping: NetworkTelemetryMapping, now: datetime, links: list[dict[str, Any]],
) -> tuple[DecoClientMapping, ...]:
    """Only complete current server links authorize observations; unlinks override saved HA choices."""
    if not isinstance(links, list) or len(links) > 500:
        raise ValueError("invalid_client_links")
    observed = {client["macAddress"]: client for client in deco_clients(hass, mapping.tracker_registry_id, now)}
    result = []
    seen = set()
    for link in links:
        if not isinstance(link, dict) or public_guid(link.get("gatewayDeviceGuid")) != mapping.device_guid:
            raise ValueError("invalid_client_links")
        mac = normalize_mac(link.get("macAddress"))
        if mac in seen:
            raise ValueError("invalid_client_links")
        seen.add(mac)
        if link.get("state") != "linked" or mac not in observed:
            continue
        result.append(DecoClientMapping(
            tracker_registry_id=observed[mac]["trackerRegistryId"], mac_address=mac,
            asset_guid=public_guid(link.get("assetGuid")),
            download_attribute_guid=public_guid(link.get("downloadAttributeGuid")),
            upload_attribute_guid=public_guid(link.get("uploadAttributeGuid")),
            connected_attribute_guid=public_guid(link.get("connectedAttributeGuid")),
        ))
    return tuple(result)
