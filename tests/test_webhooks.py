"""Signed webhook deliveries and the UteamUP subscription lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from enum import Enum
from types import SimpleNamespace

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_capture_events
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.uteamup.const import (
    CONF_CLOUDHOOK_URL,
    CONF_ENABLE_WEBHOOKS,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_SUBSCRIPTION_GUID,
    DOMAIN,
    EVENT_UTEAMUP,
)
from custom_components.uteamup.webhooks import signature_valid

from .conftest import BASE, mock_login, mock_reads, setup_entry

SUBSCRIPTION = "88888888-8888-4888-8888-888888888888"


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_check() -> None:
    body = b'{"event":"workorder.created"}'
    assert signature_valid("secret", body, _sign("secret", body))
    assert not signature_valid("secret", body, _sign("other", body))
    assert not signature_valid("secret", body + b" ", _sign("secret", body))
    assert not signature_valid("secret", body, _sign("secret", body).removeprefix("sha256="))
    assert not signature_valid("secret", body, None)


@pytest.fixture
async def webhook_entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry):
    await async_setup_component(hass, "webhook", {})
    await async_process_ha_core_config(hass, {"external_url": "https://ha.example.test"})
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_ENABLE_WEBHOOKS: True})
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/webhooks/subscriptions", json=[])
    aioclient_mock.post(f"{BASE}/api/webhooks/subscriptions", status=201, json={"guid": SUBSCRIPTION})
    aioclient_mock.delete(f"{BASE}/api/webhooks/subscriptions/{SUBSCRIPTION}", status=204, text="")
    await setup_entry(hass, config_entry)
    return config_entry


async def test_setup_registers_a_subscription_for_the_external_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, webhook_entry
) -> None:
    create = [call for call in aioclient_mock.mock_calls if call[0] == "POST" and call[1].path == "/api/webhooks/subscriptions"]
    assert len(create) == 1
    body = create[0][2]
    assert body["url"] == f"https://ha.example.test/api/webhook/{webhook_entry.data[CONF_WEBHOOK_ID]}"
    assert body["secret"] == webhook_entry.data[CONF_WEBHOOK_SECRET]
    assert "workorder.created" in body["events"]
    assert webhook_entry.data[CONF_WEBHOOK_SUBSCRIPTION_GUID] == SUBSCRIPTION


async def test_signed_delivery_fires_one_event(hass: HomeAssistant, hass_client_no_auth, webhook_entry) -> None:
    events = async_capture_events(hass, EVENT_UTEAMUP)
    client = await hass_client_no_auth()
    body = json.dumps({"event": "workorder.completed", "data": {"guid": "x"}}).encode()
    headers = {
        "X-UteamUP-Signature": _sign(webhook_entry.data[CONF_WEBHOOK_SECRET], body),
        "X-UteamUP-Delivery": "delivery-1",
        "Content-Type": "application/json",
    }
    url = f"/api/webhook/{webhook_entry.data[CONF_WEBHOOK_ID]}"

    assert (await client.post(url, data=body, headers=headers)).status == 200
    assert (await client.post(url, data=body, headers=headers)).status == 200
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["event"] == "workorder.completed"
    assert events[0].data["data"] == {"guid": "x"}
    assert events[0].data["config_entry_id"] == webhook_entry.entry_id


async def test_unsigned_or_forged_delivery_is_refused(hass: HomeAssistant, hass_client_no_auth, webhook_entry) -> None:
    events = async_capture_events(hass, EVENT_UTEAMUP)
    client = await hass_client_no_auth()
    url = f"/api/webhook/{webhook_entry.data[CONF_WEBHOOK_ID]}"
    body = b'{"event":"workorder.created"}'

    assert (await client.post(url, data=body)).status == 401
    assert (await client.post(url, data=body, headers={"X-UteamUP-Signature": _sign("guess", body)})).status == 401
    await hass.async_block_till_done()
    assert events == []


async def test_removing_the_entry_deletes_the_subscription(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, webhook_entry
) -> None:
    await hass.config_entries.async_remove(webhook_entry.entry_id)
    await hass.async_block_till_done()
    assert any(
        call[0] == "DELETE" and call[1].path == f"/api/webhooks/subscriptions/{SUBSCRIPTION}"
        for call in aioclient_mock.mock_calls
    )


async def test_without_a_public_url_a_repair_issue_explains_why(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    await async_setup_component(hass, "webhook", {})
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_ENABLE_WEBHOOKS: True})
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    await setup_entry(hass, config_entry)

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"webhook_url_unavailable_{config_entry.entry_id}")
    assert not any(call[1].path == "/api/webhooks/subscriptions" for call in aioclient_mock.mock_calls)


async def test_a_plan_without_webhooks_raises_a_repair_issue(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    await async_setup_component(hass, "webhook", {})
    await async_process_ha_core_config(hass, {"external_url": "https://ha.example.test"})
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_ENABLE_WEBHOOKS: True})
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/webhooks/subscriptions", status=403, text="")
    await setup_entry(hass, config_entry)

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"webhooks_not_allowed_{config_entry.entry_id}")


class _CloudState(Enum):
    CLOUD_CONNECTED = "cloud_connected"
    CLOUD_DISCONNECTED = "cloud_disconnected"


@pytest.fixture
def cloud(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A Home Assistant Cloud stand-in: subscribed, but still connecting, as just after a restart."""
    import homeassistant.components as components

    listeners = []

    async def create_cloudhook(_hass, webhook_id):
        fake.created.append(webhook_id)
        return f"https://hooks.nabu.casa/{webhook_id}"

    def listen(_hass, target):
        listeners.append(target)
        return lambda: listeners.remove(target)

    async def connect():
        fake.connected = True
        for target in list(listeners):
            await target(_CloudState.CLOUD_CONNECTED)

    fake = SimpleNamespace(
        connected=False,
        created=[],
        connect=connect,
        CloudConnectionState=_CloudState,
        async_active_subscription=lambda _hass: True,
        async_is_connected=lambda _hass: fake.connected,
        async_create_cloudhook=create_cloudhook,
        async_listen_connection_change=listen,
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.cloud", fake)
    monkeypatch.setattr(components, "cloud", fake, raising=False)
    hass.config.components.add("cloud")
    return fake


def _subscription_creates(aioclient_mock: AiohttpClientMocker) -> list:
    return [call for call in aioclient_mock.mock_calls if call[0] == "POST" and call[1].path == "/api/webhooks/subscriptions"]


async def _setup_without_external_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, **data: str
) -> None:
    await async_setup_component(hass, "webhook", {})
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, **data}, options={CONF_ENABLE_WEBHOOKS: True}
    )
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    aioclient_mock.get(f"{BASE}/api/webhooks/subscriptions", json=[])
    aioclient_mock.post(f"{BASE}/api/webhooks/subscriptions", status=201, json={"guid": SUBSCRIPTION})
    await setup_entry(hass, config_entry)


async def test_a_stored_cloudhook_is_used_while_cloud_is_still_connecting(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, cloud
) -> None:
    await _setup_without_external_url(
        hass,
        aioclient_mock,
        config_entry,
        **{CONF_WEBHOOK_ID: "hook", CONF_CLOUDHOOK_URL: "https://hooks.nabu.casa/hook"},
    )

    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"webhook_url_unavailable_{config_entry.entry_id}")
    assert [call[2]["url"] for call in _subscription_creates(aioclient_mock)] == ["https://hooks.nabu.casa/hook"]
    assert cloud.created == []


async def test_a_new_cloudhook_waits_for_cloud_to_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, cloud
) -> None:
    await _setup_without_external_url(hass, aioclient_mock, config_entry)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"webhook_url_unavailable_{config_entry.entry_id}")
    assert _subscription_creates(aioclient_mock) == []

    await cloud.connect()

    webhook_id = config_entry.data[CONF_WEBHOOK_ID]
    assert cloud.created == [webhook_id]
    assert config_entry.data[CONF_CLOUDHOOK_URL] == f"https://hooks.nabu.casa/{webhook_id}"
    assert [call[2]["url"] for call in _subscription_creates(aioclient_mock)] == [f"https://hooks.nabu.casa/{webhook_id}"]
    assert config_entry.data[CONF_WEBHOOK_SUBSCRIPTION_GUID] == SUBSCRIPTION
