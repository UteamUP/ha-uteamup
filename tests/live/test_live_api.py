"""Live checks against a real UteamUP API. Skipped unless UTEAMUP_LIVE_KEY_FILE is set.

The key file is JSON: {"base": "https://127.0.0.1:5002", "tenant": "<tenant guid>",
"apiKey": "...", "secret": "...", "assetGuid": "...", "attributeDefinitionGuid": "..."}.
Use a local development API; these tests create a work order, a meter reading and a
webhook subscription (removed again) in that tenant.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import timedelta

import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.uteamup.const import (
    CONF_API_SECRET,
    CONF_ENABLE_WEBHOOKS,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_SUBSCRIPTION_GUID,
    DOMAIN,
    EVENT_UTEAMUP,
)

KEY_FILE = os.environ.get("UTEAMUP_LIVE_KEY_FILE")
pytestmark = pytest.mark.skipif(not KEY_FILE, reason="set UTEAMUP_LIVE_KEY_FILE to run against a live API")


class LiveConfiguration(dict):
    """Keep credentials out of pytest's fixture-argument failure reports."""

    def __repr__(self) -> str:
        return repr({**self, "apiKey": "[REDACTED]", "secret": "[REDACTED]"})


@pytest.fixture
def live() -> dict:
    with open(KEY_FILE, encoding="utf-8") as handle:
        return LiveConfiguration(json.load(handle))


async def _add_entry(hass: HomeAssistant, live: dict):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: live["base"], CONF_API_KEY: live["apiKey"], CONF_API_SECRET: live["secret"], CONF_VERIFY_SSL: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    return result["result"]


def _state(hass: HomeAssistant, entry, platform: str, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.unique_id}_{key}")
    assert entity_id, f"{platform}.{key} missing"
    return hass.states.get(entity_id)


async def test_live_entities_and_actions(hass: HomeAssistant, socket_enabled, live: dict) -> None:
    entry = await _add_entry(hass, live)
    assert entry.unique_id == live["tenant"]
    hass.config_entries.async_update_entry(entry, options={CONF_ENABLE_WEBHOOKS: False})
    await hass.async_block_till_done()
    client = entry.runtime_data.client
    coordinator = entry.runtime_data.coordinator

    open_wo = _state(hass, entry, "sensor", "open_work_orders")
    assert open_wo.state.isdigit(), open_wo
    assert _state(hass, entry, "sensor", "stock_alerts").state.isdigit()
    todo = _state(hass, entry, "todo", "open_work_orders_list")
    assert todo.state.isdigit()
    print("\nlive counts:", {key: _state(hass, entry, "sensor", key).state for key in
                             ("open_work_orders", "in_progress_work_orders", "overdue_work_orders", "stock_alerts", "iot_alerts")},
          "denied:", sorted(coordinator.data.denied))

    report = await hass.services.async_call(
        DOMAIN,
        "create_quick_report",
        {"description": "Home Assistant live test: please ignore", "severity": "low", "asset_guid": live["assetGuid"]},
        blocking=True,
        return_response=True,
    )
    work_order_guid = report["work_order_guid"]
    assert work_order_guid, report
    created = await client._request("GET", f"/api/workorder/by-guid/{work_order_guid}")
    assert str(created["guid"]).lower() == work_order_guid.lower()
    print("quick report:", report["ticket_id"], "created by", created.get("createdByName") or created.get("createdBy"))

    reading = await hass.services.async_call(
        DOMAIN,
        "record_meter_reading",
        {
            "asset_guid": live["assetGuid"],
            "attribute_definition_guid": live["attributeDefinitionGuid"],
            "value": 26.4,
            "notes": "Home Assistant live test",
        },
        blocking=True,
        return_response=True,
    )
    assert reading["reading_guid"], reading
    print("meter reading:", reading["reading_guid"])

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert any(item["guid"].lower() == work_order_guid.lower() for item in coordinator.data.open_work_orders)
    await hass.services.async_call(
        "todo", "update_item", {"item": work_order_guid, "status": "completed"},
        target={"entity_id": todo.entity_id}, blocking=True,
    )
    completed = await client._request("GET", f"/api/workorder/by-guid/{work_order_guid}")
    assert completed["status"] == 4, completed.get("statusName")
    print("completed via to-do:", completed.get("statusName"))

    calendar = _state(hass, entry, "calendar", "work_orders")
    start = dt_util.utcnow() - timedelta(days=7)
    events = await hass.services.async_call(
        "calendar", "get_events", {"start_date_time": start, "end_date_time": start + timedelta(days=37)},
        target={"entity_id": calendar.entity_id}, blocking=True, return_response=True,
    )
    print("calendar events (44 days):", len(events[calendar.entity_id]["events"]))

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_live_webhook_subscription_lifecycle(hass: HomeAssistant, socket_enabled, hass_client_no_auth, live: dict) -> None:
    await async_setup_component(hass, "webhook", {})
    await async_process_ha_core_config(hass, {"external_url": "https://ha-live-test.example.invalid"})
    entry = await _add_entry(hass, live)
    hass.config_entries.async_update_entry(entry, options={CONF_ENABLE_WEBHOOKS: True})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    client = entry.runtime_data.client

    subscription = entry.data.get(CONF_WEBHOOK_SUBSCRIPTION_GUID)
    assert subscription, "UteamUP returned no subscription GUID"
    listed = await client.async_list_webhook_subscriptions()
    mine = [item for item in listed if str(item.get("guid")).lower() == subscription.lower()]
    assert mine and mine[0]["url"].endswith(f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}"), mine
    print("\nsubscription:", subscription, "events:", mine[0]["events"])

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data[CONF_WEBHOOK_SUBSCRIPTION_GUID] == subscription, "a reload must reuse the subscription"

    events = async_capture_events(hass, EVENT_UTEAMUP)
    body = json.dumps({"event": "workorder.created", "data": {"guid": "live"}}).encode()
    signature = "sha256=" + hmac.new(entry.data[CONF_WEBHOOK_SECRET].encode(), body, hashlib.sha256).hexdigest()
    http = await hass_client_no_auth()
    response = await http.post(f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", data=body,
                               headers={"X-UteamUP-Signature": signature, "X-UteamUP-Delivery": "live-1"})
    assert response.status == 200
    await hass.async_block_till_done()
    assert [event.data["event"] for event in events] == ["workorder.created"]

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    remaining = await client.async_list_webhook_subscriptions()
    assert not any(str(item.get("guid")).lower() == subscription.lower() for item in remaining)
    print("subscription removed with the entry")
