"""Setup, entities and their per-section availability."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.uteamup.const import CONF_ASSIGNEE_GUID, CONF_ENABLE_WEBHOOKS, DOMAIN
from custom_components.uteamup.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    ANALYTICS,
    API_KEY,
    API_SECRET,
    BASE,
    IOT,
    STOCK,
    TENANT,
    WORK_ORDER_GUID,
    mock_login,
    mock_reads,
    setup_entry,
)


def _state(hass: HomeAssistant, platform: str, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{TENANT}_{key}")
    assert entity_id, f"{platform} {key} was not created"
    return hass.states.get(entity_id)


async def test_entities_show_the_polled_counts(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry) -> None:
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor", "open_work_orders").state == "7"
    assert _state(hass, "sensor", "in_progress_work_orders").state == "3"
    assert _state(hass, "sensor", "overdue_work_orders").state == "2"
    stock = _state(hass, "sensor", "stock_alerts")
    assert stock.state == "4"
    assert stock.attributes["critical"] == 1
    assert _state(hass, "sensor", "iot_alerts").state == "0"
    assert _state(hass, "binary_sensor", "stock_critical").state == STATE_ON
    assert _state(hass, "binary_sensor", "iot_alert_active").state == STATE_OFF
    assert _state(hass, "todo", "open_work_orders_list").state == "1"


async def test_a_denied_section_only_disables_its_own_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock, analytics_status=403, iot_status=403)
    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor", "open_work_orders").state == STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", "iot_alert_active").state == STATE_UNAVAILABLE
    assert _state(hass, "sensor", "stock_alerts").state == "4"


async def test_rejected_key_starts_reauth(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry) -> None:
    mock_login(aioclient_mock, status=401, body={"message": "Invalid API key or secret."})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_unreachable_api_retries_setup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry) -> None:
    mock_login(aioclient_mock, status=503, body={})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_assignee_option_filters_the_todo_list(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_ENABLE_WEBHOOKS: False, CONF_ASSIGNEE_GUID: "55555555-5555-4555-8555-555555555555"}
    )
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    await setup_entry(hass, config_entry)
    assert _state(hass, "todo", "open_work_orders_list").state == "0"


async def test_checking_a_todo_item_completes_the_work_order(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    aioclient_mock.post(f"{BASE}/api/workorder/by-guid/{WORK_ORDER_GUID}/complete", json={"workorderGuid": WORK_ORDER_GUID})
    await setup_entry(hass, config_entry)
    todo = _state(hass, "todo", "open_work_orders_list")

    await hass.services.async_call(
        "todo", "update_item", {"item": WORK_ORDER_GUID, "status": "completed"}, target={"entity_id": todo.entity_id}, blocking=True
    )

    assert any(call[1].path == f"/api/workorder/by-guid/{WORK_ORDER_GUID}/complete" for call in aioclient_mock.mock_calls)


async def test_adding_a_todo_item_creates_a_work_order(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    aioclient_mock.post(f"{BASE}/api/workorder", status=201, json={"guid": WORK_ORDER_GUID})
    await setup_entry(hass, config_entry)
    todo = _state(hass, "todo", "open_work_orders_list")

    await hass.services.async_call("todo", "add_item", {"item": "Check boiler"}, target={"entity_id": todo.entity_id}, blocking=True)

    create = next(call for call in aioclient_mock.mock_calls if call[0] == "POST" and call[1].path == "/api/workorder")
    assert create[2]["name"] == "Check boiler"
    assert create[2]["dueDate"] > create[2]["startDate"]


async def test_calendar_lists_work_orders_in_the_window(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    aioclient_mock.get(
        f"{BASE}/api/workorder/calendar",
        json=[{"guid": WORK_ORDER_GUID, "name": "Replace pump seal", "startDate": "2030-01-01T08:00:00Z", "dueDate": "2030-01-01T08:00:00Z"}],
    )
    await setup_entry(hass, config_entry)
    calendar = _state(hass, "calendar", "work_orders")

    start = dt_util.parse_datetime("2030-01-01T00:00:00+00:00")
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {"start_date_time": start, "end_date_time": start + timedelta(days=1)},
        target={"entity_id": calendar.entity_id},
        blocking=True,
        return_response=True,
    )

    events = response[calendar.entity_id]["events"]
    assert [event["summary"] for event in events] == ["Replace pump seal"]
    assert events[0]["end"] == "2030-01-01T09:00:00+00:00"


async def test_diagnostics_redact_credentials(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry) -> None:
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock, iot_status=403)
    await setup_entry(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    text = str(diagnostics)
    assert API_KEY not in text
    assert API_SECRET not in text
    assert diagnostics["data"]["denied"] == ["iot_alerts"]


async def test_unload(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry) -> None:
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    await setup_entry(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_unset_dotnet_dates_do_not_break_calendar_or_todo(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    """A work order without dates arrives as 0001-01-01, which overflows local-time conversion."""
    mock_login(aioclient_mock)
    unset = "0001-01-01T00:00:00"
    for path, body in (("/api/analytics/summary", ANALYTICS), ("/api/stock/alerts/summary", STOCK), ("/api/iot/alerts", IOT)):
        aioclient_mock.get(f"{BASE}{path}", json=body)
    aioclient_mock.get(
        f"{BASE}/api/workorder",
        json={"items": [{"guid": WORK_ORDER_GUID, "ticketId": "WO-1", "name": "Undated", "startDate": unset, "dueDate": unset}]},
    )
    await setup_entry(hass, config_entry)

    assert _state(hass, "calendar", "work_orders").state == "off"
    todo = _state(hass, "todo", "open_work_orders_list")
    assert todo.state == "1"
    items = await hass.services.async_call(
        "todo", "get_items", target={"entity_id": todo.entity_id}, blocking=True, return_response=True
    )
    assert "due" not in items[todo.entity_id]["items"][0]
