"""Fixtures for the UteamUP integration tests."""

from __future__ import annotations

import base64
import json
import time

import pytest

from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.uteamup.const import CONF_API_SECRET, CONF_ENABLE_WEBHOOKS, CONF_TENANT_GUID, DOMAIN

BASE = "https://api.example.test"
TENANT = "3e1cd135-07d1-46ff-8f5f-549da5390e78"
API_KEY = "a" * 32
API_SECRET = "b" * 64
WORK_ORDER_GUID = "11111111-1111-4111-8111-111111111111"
ASSET_GUID = "22222222-2222-4222-8222-222222222222"
ATTRIBUTE_GUID = "33333333-3333-4333-8333-333333333333"


def make_token(tenant: str = TENANT, expires_in: int = 86400) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    claims = {"TenantGuid": tenant, "AuthType": "ApiKey", "exp": int(time.time()) + expires_in}
    return f"{part({'alg': 'HS256'})}.{part(claims)}.signature"


ANALYTICS = {"workorders": {"open": 7, "inProgress": 3, "onHold": 1, "overdue": 2}}
STOCK = {"totalActive": 4, "criticalCount": 1, "lowStockCount": 2, "stockoutCount": 1, "actionRequiredCount": 3}
IOT = {"items": [], "totalItems": 0}
WORK_ORDERS = {
    "items": [
        {
            "guid": WORK_ORDER_GUID,
            "ticketId": "WO-42",
            "name": "Replace pump seal",
            "statusName": "Not started",
            "startDate": "2030-01-01T08:00:00",
            "dueDate": "2030-01-01T12:00:00Z",
            "primaryAssigneeGuid": "44444444-4444-4444-8444-444444444444",
        }
    ],
    "totalItems": 1,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


def mock_login(aioclient_mock: AiohttpClientMocker, tenant: str = TENANT, status: int = 200, body=None) -> None:
    aioclient_mock.post(
        f"{BASE}/api/auth/apikey-login",
        status=status,
        json=body if body is not None else {"accessToken": make_token(tenant), "refreshToken": ""},
    )


def mock_reads(aioclient_mock: AiohttpClientMocker, **overrides) -> None:
    """Register the four polled sections; pass `<section>_status=403` to deny one."""
    for path, body, key in (
        ("/api/analytics/summary", ANALYTICS, "analytics"),
        ("/api/stock/alerts/summary", STOCK, "stock"),
        ("/api/iot/alerts", IOT, "iot"),
        ("/api/workorder", WORK_ORDERS, "workorders"),
    ):
        aioclient_mock.get(f"{BASE}{path}", status=overrides.get(f"{key}_status", 200), json=body)


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="UteamUP 3e1cd135",
        unique_id=TENANT,
        data={
            CONF_URL: BASE,
            CONF_API_KEY: API_KEY,
            CONF_API_SECRET: API_SECRET,
            CONF_VERIFY_SSL: True,
            CONF_TENANT_GUID: TENANT,
        },
        options={CONF_ENABLE_WEBHOOKS: False},
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
