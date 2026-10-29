"""The REST client: token handling, headers and error mapping."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.uteamup.api import (
    UteamupApiError,
    UteamupAuthError,
    UteamupClient,
    UteamupForbiddenError,
    UteamupNotFoundError,
    UteamupPlanError,
    UteamupRateLimitError,
)

from .conftest import API_KEY, API_SECRET, BASE, TENANT, WORK_ORDER_GUID, make_token, mock_login


def _client(hass: HomeAssistant) -> UteamupClient:
    return UteamupClient(async_get_clientsession(hass), BASE, API_KEY, API_SECRET)


async def test_login_reads_the_tenant_from_the_token(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_login(aioclient_mock)
    client = _client(hass)
    assert await client.async_login() == TENANT
    assert client.tenant_guid == TENANT


async def test_token_is_reused_and_sent_with_the_tenant(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/stock/alerts/summary", json={"totalActive": 1})
    client = _client(hass)
    await client.async_get_stock_alert_summary()
    await client.async_get_stock_alert_summary()

    logins = [call for call in aioclient_mock.mock_calls if call[1].path == "/api/auth/apikey-login"]
    assert len(logins) == 1
    headers = aioclient_mock.mock_calls[-1][3]
    assert headers["X-Tenant-Guid"] == TENANT
    assert headers["Authorization"].startswith("Bearer ")
    assert "X-Requested-With" not in headers


async def test_expiring_token_is_renewed_before_the_call(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{BASE}/api/auth/apikey-login", json={"accessToken": make_token(expires_in=60)})
    aioclient_mock.get(f"{BASE}/api/stock/alerts/summary", json={})
    client = _client(hass)
    await client.async_login()
    await client.async_get_stock_alert_summary()
    logins = [call for call in aioclient_mock.mock_calls if call[1].path == "/api/auth/apikey-login"]
    assert len(logins) == 2


async def test_writes_carry_the_csrf_header(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_login(aioclient_mock)
    aioclient_mock.post(f"{BASE}/api/workorder/by-guid/{WORK_ORDER_GUID}/complete", json={"workorderGuid": WORK_ORDER_GUID})
    await _client(hass).async_complete_work_order(WORK_ORDER_GUID, "Seal replaced")
    _, url, body, headers = aioclient_mock.mock_calls[-1]
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert body == {"report": {"description": "Seal replaced"}}


async def test_asset_link_patch_uses_guid_tenant_and_exact_additive_payload(hass, aioclient_mock):
    from .conftest import ASSET_GUID
    mock_login(aioclient_mock)
    aioclient_mock.patch(f"{BASE}/api/asset/by-guid/{ASSET_GUID}", json={"externalGuid": ASSET_GUID})
    payload = {"assetTypeGuids": [WORK_ORDER_GUID], "expectedUpdatedAtUtc": "2026-10-29T12:00:00Z"}
    await _client(hass).async_patch_asset(ASSET_GUID, payload)
    _, url, body, headers = aioclient_mock.mock_calls[-1]
    assert url.path == f"/api/asset/by-guid/{ASSET_GUID}"
    assert body == payload
    assert headers["X-Tenant-Guid"] == TENANT
    assert headers["X-Requested-With"] == "XMLHttpRequest"


async def test_a_401_logs_in_again_once(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/stock/alerts/summary", status=401, json={"message": "expired"})
    client = _client(hass)
    with pytest.raises(UteamupAuthError):
        await client.async_get_stock_alert_summary()
    logins = [call for call in aioclient_mock.mock_calls if call[1].path == "/api/auth/apikey-login"]
    assert len(logins) == 2


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (402, UteamupPlanError),
        (403, UteamupForbiddenError),
        (404, UteamupNotFoundError),
        (409, UteamupApiError),
        (422, UteamupApiError),
    ],
)
async def test_error_statuses_map_to_typed_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, error: type[Exception]
) -> None:
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/stock/alerts/summary", status=status, json={"error": "no"})
    with pytest.raises(error):
        await _client(hass).async_get_stock_alert_summary()


async def test_rate_limit_reports_retry_after(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/stock/alerts/summary", status=429, headers={"Retry-After": "30"}, text="")
    with pytest.raises(UteamupRateLimitError) as raised:
        await _client(hass).async_get_stock_alert_summary()
    assert raised.value.retry_after == 30


async def test_quick_report_is_sent_as_a_form(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_login(aioclient_mock)
    aioclient_mock.post(f"{BASE}/api/quickreport", status=201, json={"workorderGuid": WORK_ORDER_GUID, "ticketId": "QR-1"})
    result = await _client(hass).async_create_quick_report("Leak under sink", "high", None, ["plumbing"])
    assert result["ticketId"] == "QR-1"
    _, _, body, headers = aioclient_mock.mock_calls[-1]
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert body.is_multipart, "the endpoint answers 415 to a URL-encoded form"
    fields = {field[0]["name"]: field[2] for field in body._fields}
    assert fields == {"Description": "Leak under sink", "Severity": "HIGH", "Tags": "plumbing"}
