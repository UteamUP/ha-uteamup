"""Work orders on a Home Assistant calendar."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import UteamupError
from .coordinator import UteamupConfigEntry
from .entity import UteamupEntity, parse_api_datetime

DEFAULT_DURATION = timedelta(hours=1)


async def async_setup_entry(
    hass: HomeAssistant, entry: UteamupConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([UteamupWorkOrderCalendar(entry.runtime_data.coordinator, "work_orders")])


def _event(item: dict[str, Any]) -> CalendarEvent | None:
    start = parse_api_datetime(item.get("startDate")) or parse_api_datetime(item.get("dueDate"))
    if start is None:
        return None
    end = parse_api_datetime(item.get("dueDate"))
    if end is None or end <= start:
        end = start + DEFAULT_DURATION
    return CalendarEvent(
        start=start,
        end=end,
        summary=item.get("name") or item.get("ticketId") or "Work order",
        description=item.get("statusName"),
        location=item.get("locationName"),
        uid=item.get("guid"),
    )


class UteamupWorkOrderCalendar(UteamupEntity, CalendarEntity):
    """Scheduled work orders; the current event is the next open one."""

    @property
    def available(self) -> bool:
        return super().available and "open_work_orders" not in self.coordinator.data.denied

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.utcnow()
        events = [event for item in self.coordinator.data.open_work_orders or [] if (event := _event(item))]
        upcoming = [event for event in events if event.end > now]
        return min(upcoming, key=lambda event: event.start, default=None)

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        try:
            items = await self.coordinator.client.async_get_work_order_calendar(start_date, end_date)
        except UteamupError as err:
            raise HomeAssistantError(f"Could not read UteamUP work orders: {err}") from err
        return [event for item in items if (event := _event(item))]
