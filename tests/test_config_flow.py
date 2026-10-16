"""Config, reauth, reconfigure and options flows."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.uteamup.const import (
    CONF_API_SECRET,
    CONF_ASSIGNEE_GUID,
    CONF_ENABLE_WEBHOOKS,
    CONF_TENANT_GUID,
    DOMAIN,
)

from .conftest import API_KEY, API_SECRET, BASE, TENANT, make_token, mock_login

USER_INPUT = {CONF_URL: BASE, CONF_API_KEY: API_KEY, CONF_API_SECRET: API_SECRET, CONF_VERIFY_SSL: True}


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.uteamup.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_user_flow_creates_entry_for_the_keys_tenant(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_login(aioclient_mock)
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == TENANT
    assert result["title"] == "UteamUP 0a1b2c3d"
    assert result["data"] == {**USER_INPUT, CONF_TENANT_GUID: TENANT}
    login_body = aioclient_mock.mock_calls[0][2]
    assert login_body == {"apiKey": API_KEY, "secret": API_SECRET}


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"message": "Invalid API key or secret."}, "invalid_auth"),
        (400, {"message": "MCP keys must authenticate via the OAuth 2.0 flow."}, "mcp_key_not_allowed"),
        (429, {"message": "Rate limit exceeded."}, "rate_limited"),
        (503, {}, "cannot_connect"),
    ],
)
async def test_user_flow_shows_login_errors_and_recovers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, body: dict, error: str
) -> None:
    mock_login(aioclient_mock, status=status, body=body)
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    aioclient_mock.clear_requests()
    mock_login(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_same_tenant_cannot_be_added_twice(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    mock_login(aioclient_mock)
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_the_credentials(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    mock_login(aioclient_mock)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    new_key, new_secret = "c" * 32, "d" * 64
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: new_key, CONF_API_SECRET: new_secret}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_KEY] == new_key
    assert config_entry.data[CONF_API_SECRET] == new_secret


async def test_reauth_with_another_companys_key_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    mock_login(aioclient_mock, tenant="99999999-9999-4999-8999-999999999999")
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "c" * 32, CONF_API_SECRET: "d" * 64}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_tenant"
    assert config_entry.data[CONF_API_KEY] == API_KEY


async def test_reconfigure_changes_the_address(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    new_base = "https://devback.example.test"
    aioclient_mock.post(f"{new_base}/api/auth/apikey-login", json={"accessToken": make_token()})
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, CONF_URL: new_base})
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_URL] == new_base


async def test_options_validate_the_assignee_guid(hass: HomeAssistant, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_ENABLE_WEBHOOKS: False, CONF_ASSIGNEE_GUID: "not-a-guid"}
    )
    assert result["errors"] == {CONF_ASSIGNEE_GUID: "invalid_guid"}

    assignee = "44444444-4444-4444-8444-444444444444"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_ENABLE_WEBHOOKS: False, CONF_ASSIGNEE_GUID: assignee}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_SCAN_INTERVAL: 60, CONF_ENABLE_WEBHOOKS: False, CONF_ASSIGNEE_GUID: assignee}
