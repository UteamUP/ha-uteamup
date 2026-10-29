"""The Home Assistant diagnostics download keeps integration details private."""

from __future__ import annotations

import json

import pytest

from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.setup import async_setup_component

from custom_components.uteamup.const import CONF_API_SECRET, CONF_ASSIGNEE_GUID, CONF_TENANT_GUID

from .conftest import API_KEY, API_SECRET, BASE, TENANT, WORK_ORDERS, mock_login, mock_reads, setup_entry


@pytest.fixture
async def diagnostics_entry(hass, aioclient_mock, config_entry):
    """Load the real integration and diagnostics platform with private fixture data."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            **config_entry.options,
            CONF_ASSIGNEE_GUID: WORK_ORDERS["items"][0]["primaryAssigneeGuid"],
        },
    )
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    await setup_entry(hass, config_entry)
    assert await async_setup_component(hass, "diagnostics", {})
    await hass.async_block_till_done()
    return config_entry


async def test_authenticated_diagnostics_download_is_redacted(hass_client, diagnostics_entry):
    client = await hass_client()

    response = await client.get(f"/api/diagnostics/config_entry/{diagnostics_entry.entry_id}")

    assert response.status == 200
    assert response.content_type == "application/json"
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="config_entry-uteamup-{diagnostics_entry.entry_id}.json"'
    )
    download = await response.json()
    serialized = json.dumps(download)
    for private in (API_KEY, API_SECRET, BASE, TENANT, *WORK_ORDERS["items"][0].values()):
        assert private not in serialized
    diagnostics = download["data"]
    for key in (CONF_API_KEY, CONF_API_SECRET, CONF_URL, CONF_TENANT_GUID):
        assert diagnostics["entry"][key] == "**REDACTED**"
    assert diagnostics["options"][CONF_ASSIGNEE_GUID] == "**REDACTED**"
    assert diagnostics["data"]["open_work_orders_count"] == 1
    assert "open_work_orders" not in diagnostics["data"]


async def test_anonymous_diagnostics_download_is_refused(hass_client_no_auth, diagnostics_entry):
    client = await hass_client_no_auth()

    response = await client.get(f"/api/diagnostics/config_entry/{diagnostics_entry.entry_id}")

    assert response.status == 401
    assert "Content-Disposition" not in response.headers
    body = await response.text()
    for private in (API_KEY, API_SECRET, BASE, TENANT, *WORK_ORDERS["items"][0].values()):
        assert private not in body
