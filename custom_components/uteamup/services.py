"""Actions that change UteamUP from Home Assistant automations."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .api import UteamupClient, UteamupError, UteamupForbiddenError, UteamupNotFoundError
from .const import ASSET_STATES, DOMAIN, QUICK_REPORT_SEVERITIES, WORK_ORDER_STATUSES
from .coordinator import UteamupConfigEntry

ATTR_CONFIG_ENTRY_ID = "config_entry_id"


def valid_guid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as err:
        raise vol.Invalid(f"{value!r} is not a GUID") from err


_BASE = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}

SCHEMAS: dict[str, vol.Schema] = {
    "create_work_order": vol.Schema(
        {
            **_BASE,
            vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=200)),
            vol.Optional("description"): cv.string,
            vol.Optional("priority", default=2): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
            vol.Optional("due"): cv.datetime,
            vol.Optional("asset_guid"): valid_guid,
            vol.Optional("assignee_guid"): valid_guid,
        }
    ),
    "create_quick_report": vol.Schema(
        {
            **_BASE,
            vol.Required("description"): vol.All(cv.string, vol.Length(min=1, max=2000)),
            vol.Optional("severity"): vol.In(QUICK_REPORT_SEVERITIES),
            vol.Optional("asset_guid"): valid_guid,
            vol.Optional("tags"): vol.All(cv.ensure_list, [cv.string]),
        }
    ),
    "set_work_order_status": vol.Schema(
        {
            **_BASE,
            vol.Required("work_order_guid"): valid_guid,
            vol.Required("status"): vol.In(list(WORK_ORDER_STATUSES)),
            vol.Optional("expected_status"): vol.In(list(WORK_ORDER_STATUSES)),
        }
    ),
    "complete_work_order": vol.Schema(
        {**_BASE, vol.Required("work_order_guid"): valid_guid, vol.Optional("notes"): vol.All(cv.string, vol.Length(max=2000))}
    ),
    "set_asset_state": vol.Schema(
        {
            **_BASE,
            vol.Required("asset_guid"): valid_guid,
            vol.Required("state"): vol.In(list(ASSET_STATES)),
            vol.Optional("reason"): cv.string,
        }
    ),
    "run_pm_schedule": vol.Schema({**_BASE, vol.Required("schedule_guid"): valid_guid}),
    "record_meter_reading": vol.Schema(
        {
            **_BASE,
            vol.Required("asset_guid"): valid_guid,
            vol.Required("attribute_definition_guid"): valid_guid,
            vol.Required("value"): vol.Coerce(float),
            vol.Optional("timestamp"): cv.datetime,
            vol.Optional("notes"): cv.string,
        }
    ),
    "append_journal_entry": vol.Schema(
        {
            **_BASE,
            vol.Required("journal_guid"): valid_guid,
            vol.Required("entry"): vol.All(cv.string, vol.Length(min=1, max=2000)),
        }
    ),
}


def _entry(hass: HomeAssistant, call: ServiceCall) -> UteamupConfigEntry:
    entries = [
        entry for entry in hass.config_entries.async_entries(DOMAIN) if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
    if len(entries) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_required" if not entries or entry_id else "entry_ambiguous",
        )
    return entries[0]


async def _create_work_order(client: UteamupClient, data: dict[str, Any]) -> dict[str, Any]:
    start = dt_util.utcnow()
    due = dt_util.as_utc(data["due"]) if "due" in data else start + timedelta(days=1)
    body: dict[str, Any] = {
        "name": data["name"],
        "priority": data["priority"],
        "startDate": start.isoformat(),
        "dueDate": max(due, start).isoformat(),
    }
    if "description" in data:
        body["description"] = data["description"]
    if "asset_guid" in data:
        body["assetGuids"] = [data["asset_guid"]]
    if "assignee_guid" in data:
        body["primaryAssigneeGuid"] = data["assignee_guid"]
    created = await client.async_create_work_order(body)
    return {"work_order_guid": created.get("guid"), "ticket_id": created.get("ticketId")}


async def _create_quick_report(client: UteamupClient, data: dict[str, Any]) -> dict[str, Any]:
    created = await client.async_create_quick_report(
        data["description"], data.get("severity"), data.get("asset_guid"), data.get("tags")
    )
    return {"work_order_guid": created.get("workorderGuid"), "ticket_id": created.get("ticketId")}


async def _set_work_order_status(client: UteamupClient, data: dict[str, Any]) -> None:
    expected = data.get("expected_status")
    await client.async_set_work_order_status(
        data["work_order_guid"],
        WORK_ORDER_STATUSES[data["status"]],
        WORK_ORDER_STATUSES[expected] if expected else None,
    )


async def _complete_work_order(client: UteamupClient, data: dict[str, Any]) -> None:
    await client.async_complete_work_order(data["work_order_guid"], data.get("notes"))


async def _set_asset_state(client: UteamupClient, data: dict[str, Any]) -> None:
    await client.async_set_asset_state(data["asset_guid"], ASSET_STATES[data["state"]], data.get("reason"))


async def _run_pm_schedule(client: UteamupClient, data: dict[str, Any]) -> dict[str, Any]:
    created = await client.async_run_pm_schedule(data["schedule_guid"])
    return {"work_order_guid": created.get("guid"), "ticket_id": created.get("ticketId")}


async def _record_meter_reading(client: UteamupClient, data: dict[str, Any]) -> dict[str, Any]:
    timestamp = data.get("timestamp")
    created = await client.async_record_meter_reading(
        data["asset_guid"],
        data["attribute_definition_guid"],
        data["value"],
        dt_util.as_utc(timestamp) if timestamp else None,
        data.get("notes"),
    )
    return {"reading_guid": created.get("externalGuid")}


async def _append_journal_entry(client: UteamupClient, data: dict[str, Any]) -> dict[str, Any]:
    updated = await client.async_append_journal_entry(data["journal_guid"], data["entry"])
    return {"journal_guid": updated.get("guid")}


HANDLERS: dict[str, Callable[[UteamupClient, dict[str, Any]], Awaitable[dict[str, Any] | None]]] = {
    "create_work_order": _create_work_order,
    "create_quick_report": _create_quick_report,
    "set_work_order_status": _set_work_order_status,
    "complete_work_order": _complete_work_order,
    "set_asset_state": _set_asset_state,
    "run_pm_schedule": _run_pm_schedule,
    "record_meter_reading": _record_meter_reading,
    "append_journal_entry": _append_journal_entry,
}

RETURNS_DATA = {
    "create_work_order",
    "create_quick_report",
    "run_pm_schedule",
    "record_meter_reading",
    "append_journal_entry",
}


def async_setup_services(hass: HomeAssistant) -> None:
    """Register every UteamUP action once for all config entries."""

    def make_handler(name: str):
        async def handle(call: ServiceCall) -> ServiceResponse:
            entry = _entry(hass, call)
            try:
                result = await HANDLERS[name](entry.runtime_data.client, dict(call.data))
            except UteamupForbiddenError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="forbidden", translation_placeholders={"error": str(err)}
                ) from err
            except UteamupNotFoundError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="not_found", translation_placeholders={"error": str(err)}
                ) from err
            except UteamupError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="api_error", translation_placeholders={"error": str(err)}
                ) from err
            await entry.runtime_data.coordinator.async_request_refresh()
            return result if call.return_response else None

        return handle

    for name, schema in SCHEMAS.items():
        hass.services.async_register(
            DOMAIN,
            name,
            make_handler(name),
            schema=schema,
            supports_response=SupportsResponse.OPTIONAL if name in RETURNS_DATA else SupportsResponse.NONE,
        )
