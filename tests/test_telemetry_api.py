"""Discovery extends the existing tenant API transport and public GUID contracts."""

from unittest.mock import AsyncMock, MagicMock, call

from custom_components.uteamup.api import UteamupClient

from .conftest import API_KEY, API_SECRET, ASSET_GUID, BASE
from .test_telemetry_config import ASSET_TYPE, ASSET_TYPE_GUID, ATTRIBUTES, DEVICE


async def test_iot_discovery_follows_device_pages_and_reads_effective_attributes():
    client = UteamupClient(MagicMock(), BASE, API_KEY, API_SECRET)
    client._request = AsyncMock(side_effect=[
        {"items": [DEVICE], "totalItems": 2}, {"items": [DEVICE], "totalItems": 2},
        {"environment": {}}, ATTRIBUTES, ASSET_TYPE,
    ])
    assert len(await client.async_get_iot_devices()) == 2
    await client.async_get_iot_environment()
    await client.async_get_effective_attributes(ASSET_GUID)
    assert client._request.await_args_list == [
        call("GET", "/api/iot/devices", params={"page": 1, "pageSize": 100}),
        call("GET", "/api/iot/devices", params={"page": 2, "pageSize": 100}),
        call("GET", "/api/iot/environment"),
        call("GET", f"/api/asset/by-guid/{ASSET_GUID}/effective-attribute-definitions"),
        call("GET", f"/api/asset-type/{ASSET_TYPE_GUID}/with-inherited-attributes"),
    ]


async def test_compact_effective_definition_cannot_override_full_iot_flags():
    client = UteamupClient(MagicMock(), BASE, API_KEY, API_SECRET)
    disabled = {**ASSET_TYPE, "attributeDefinitions": [
        {**item, "allowIoTUpdates": False} for item in ASSET_TYPE["attributeDefinitions"]
    ]}
    client._request = AsyncMock(side_effect=[ATTRIBUTES, disabled, ATTRIBUTES])
    first = await client.async_get_effective_attributes(ASSET_GUID)
    assert all(item["allowIoTUpdates"] is False for item in first[0]["definitions"])
    await client.async_get_effective_attributes(ASSET_GUID)
    assert client._request.await_count == 3  # The reviewed type is reused within this discovery client.
