"""The uteamup.* actions."""

from __future__ import annotations

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.uteamup.const import DOMAIN

from .conftest import ASSET_GUID, ATTRIBUTE_GUID, BASE, WORK_ORDER_GUID, mock_login, mock_reads, setup_entry


@pytest.fixture
async def loaded(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry) -> MockConfigEntry:
    mock_login(aioclient_mock)
    mock_reads(aioclient_mock)
    await setup_entry(hass, config_entry)
    return config_entry


def _last(aioclient_mock: AiohttpClientMocker, method: str, path: str):
    return [call for call in aioclient_mock.mock_calls if call[0] == method and call[1].path == path][-1]


async def test_create_work_order_returns_its_guid(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded) -> None:
    aioclient_mock.post(f"{BASE}/api/workorder", status=201, json={"guid": WORK_ORDER_GUID, "ticketId": "WO-43"})

    response = await hass.services.async_call(
        DOMAIN,
        "create_work_order",
        {"name": "Leak in boiler room", "priority": 4, "asset_guid": ASSET_GUID.upper()},
        blocking=True,
        return_response=True,
    )

    assert response == {"work_order_guid": WORK_ORDER_GUID, "ticket_id": "WO-43"}
    body = _last(aioclient_mock, "POST", "/api/workorder")[2]
    assert body["name"] == "Leak in boiler room"
    assert body["priority"] == 4
    assert body["assetGuids"] == [ASSET_GUID]
    assert body["dueDate"] > body["startDate"]


async def test_create_quick_report(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded) -> None:
    aioclient_mock.post(f"{BASE}/api/quickreport", status=201, json={"workorderGuid": WORK_ORDER_GUID, "ticketId": "QR-7"})
    response = await hass.services.async_call(
        DOMAIN, "create_quick_report", {"description": "Water on floor", "severity": "critical"}, blocking=True, return_response=True
    )
    assert response == {"work_order_guid": WORK_ORDER_GUID, "ticket_id": "QR-7"}


async def test_set_work_order_status_uses_the_status_id_and_guard(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded
) -> None:
    path = f"/api/workorder/by-guid/{WORK_ORDER_GUID}/status/2"
    aioclient_mock.put(f"{BASE}{path}", json={"guid": WORK_ORDER_GUID})
    await hass.services.async_call(
        DOMAIN,
        "set_work_order_status",
        {"work_order_guid": WORK_ORDER_GUID, "status": "in_progress", "expected_status": "not_started"},
        blocking=True,
    )
    assert _last(aioclient_mock, "PUT", path)[1].query["expectedStatus"] == "0"


async def test_complete_work_order_sends_the_notes(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded) -> None:
    path = f"/api/workorder/by-guid/{WORK_ORDER_GUID}/complete"
    aioclient_mock.post(f"{BASE}{path}", json={"workorderGuid": WORK_ORDER_GUID})
    await hass.services.async_call(
        DOMAIN, "complete_work_order", {"work_order_guid": WORK_ORDER_GUID, "notes": "Done"}, blocking=True
    )
    assert _last(aioclient_mock, "POST", path)[2] == {"report": {"description": "Done"}}


async def test_set_asset_state_uses_the_api_enum(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded) -> None:
    path = f"/api/assetstate/asset/by-guid/{ASSET_GUID}/operational"
    aioclient_mock.put(f"{BASE}{path}", json={"assetGuid": ASSET_GUID})
    await hass.services.async_call(
        DOMAIN, "set_asset_state", {"asset_guid": ASSET_GUID, "state": "under_maintenance", "reason": "Planned"}, blocking=True
    )
    assert _last(aioclient_mock, "PUT", path)[2] == {"toState": "underMaintenance", "reason": "Planned"}


async def test_run_pm_schedule(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded) -> None:
    schedule = "66666666-6666-4666-8666-666666666666"
    aioclient_mock.post(f"{BASE}/api/workordertemplate/schedules/{schedule}/runnow", status=201, json={"guid": WORK_ORDER_GUID})
    response = await hass.services.async_call(
        DOMAIN, "run_pm_schedule", {"schedule_guid": schedule}, blocking=True, return_response=True
    )
    assert response["work_order_guid"] == WORK_ORDER_GUID


async def test_record_meter_reading(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded) -> None:
    path = f"/api/assets/{ASSET_GUID}/meter-readings"
    reading = "77777777-7777-4777-8777-777777777777"
    aioclient_mock.post(f"{BASE}{path}", status=201, json={"externalGuid": reading})
    response = await hass.services.async_call(
        DOMAIN,
        "record_meter_reading",
        {"asset_guid": ASSET_GUID, "attribute_definition_guid": ATTRIBUTE_GUID, "value": "1234.5", "notes": "HA"},
        blocking=True,
        return_response=True,
    )
    assert response == {"reading_guid": reading}
    assert _last(aioclient_mock, "POST", path)[2] == {
        "attributeDefinitionGuid": ATTRIBUTE_GUID,
        "readingValue": 1234.5,
        "notes": "HA",
    }


async def test_append_journal_entry_preserves_metadata_and_escapes_html(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded
) -> None:
    journal = "88888888-8888-4888-8888-888888888888"
    path = f"/api/journal/by-guid/{journal}"
    aioclient_mock.get(
        f"{BASE}{path}",
        json={
            "guid": journal,
            "title": "Activity Log",
            "content": "<p>Header</p>",
            "journalDate": "2026-10-18T17:00:00Z",
            "priority": 2,
            "summary": "Room activity",
            "categoryGuid": "2c1da394-f841-4f98-9933-31c13c3d2e2f",
            "locationGuid": "71cfb161-1978-4c3e-aa7d-1f00b9be0790",
            "isTemplate": False,
            "isArchived": False,
        },
    )
    aioclient_mock.put(f"{BASE}{path}", json={"guid": journal})

    response = await hass.services.async_call(
        DOMAIN,
        "append_journal_entry",
        {"journal_guid": journal, "entry": "2026-10-18 17:55 — Einhver fór inn í kompu <1>"},
        blocking=True,
        return_response=True,
    )

    assert response == {"journal_guid": journal}
    form = _last(aioclient_mock, "PUT", path)[2]
    assert form.is_multipart
    fields = {field[0]["name"]: field[2] for field in form._fields}
    assert fields["Title"] == "Activity Log"
    assert fields["Content"] == "<p>Header</p>\n<p>2026-10-18 17:55 — Einhver fór inn í kompu &lt;1&gt;</p>"
    assert fields["Priority"] == "2"
    assert fields["Summary"] == "Room activity"
    assert fields["CategoryGuid"] == "2c1da394-f841-4f98-9933-31c13c3d2e2f"
    assert fields["LocationGuid"] == "71cfb161-1978-4c3e-aa7d-1f00b9be0790"


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("complete_work_order", {"work_order_guid": "WO-42"}),
        ("set_asset_state", {"asset_guid": "1; DROP TABLE", "state": "online"}),
        ("set_asset_state", {"asset_guid": ASSET_GUID, "state": "exploded"}),
        ("record_meter_reading", {"asset_guid": ASSET_GUID, "attribute_definition_guid": ATTRIBUTE_GUID, "value": "lots"}),
        ("create_quick_report", {"description": ""}),
    ],
)
async def test_invalid_input_is_rejected_before_any_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded, service: str, data: dict
) -> None:
    calls = aioclient_mock.call_count
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    assert aioclient_mock.call_count == calls


async def test_forbidden_is_explained(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded) -> None:
    aioclient_mock.post(f"{BASE}/api/workorder/by-guid/{WORK_ORDER_GUID}/complete", status=403, text="")
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(DOMAIN, "complete_work_order", {"work_order_guid": WORK_ORDER_GUID}, blocking=True)
    assert raised.value.translation_key == "forbidden"


async def test_missing_record_is_a_validation_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded) -> None:
    aioclient_mock.post(f"{BASE}/api/workorder/by-guid/{WORK_ORDER_GUID}/complete", status=404, json={"error": "gone"})
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(DOMAIN, "complete_work_order", {"work_order_guid": WORK_ORDER_GUID}, blocking=True)
    assert raised.value.translation_key == "not_found"


async def test_two_companies_need_an_explicit_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, loaded
) -> None:
    second = MockConfigEntry(domain=DOMAIN, unique_id="other", data=dict(loaded.data), options=dict(loaded.options))
    await setup_entry(hass, second)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(DOMAIN, "complete_work_order", {"work_order_guid": WORK_ORDER_GUID}, blocking=True)
    assert raised.value.translation_key == "entry_ambiguous"

    aioclient_mock.post(f"{BASE}/api/workorder/by-guid/{WORK_ORDER_GUID}/complete", json={})
    await hass.services.async_call(
        DOMAIN,
        "complete_work_order",
        {"work_order_guid": WORK_ORDER_GUID, "config_entry_id": second.entry_id},
        blocking=True,
    )
