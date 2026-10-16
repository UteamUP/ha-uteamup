"""Open work orders as a Home Assistant to-do list."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import UteamupError
from .coordinator import UteamupConfigEntry
from .entity import UteamupEntity, parse_api_datetime


async def async_setup_entry(
    hass: HomeAssistant, entry: UteamupConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([UteamupWorkOrderTodoList(entry.runtime_data.coordinator, "open_work_orders_list")])


class UteamupWorkOrderTodoList(UteamupEntity, TodoListEntity):
    """Checking an item completes the work order; adding one creates a work order."""

    _attr_supported_features = TodoListEntityFeature.CREATE_TODO_ITEM | TodoListEntityFeature.UPDATE_TODO_ITEM

    @property
    def available(self) -> bool:
        return super().available and "open_work_orders" not in self.coordinator.data.denied

    @property
    def todo_items(self) -> list[TodoItem]:
        return [
            TodoItem(
                uid=item["guid"],
                summary=" ".join(part for part in (item.get("ticketId"), item.get("name")) if part),
                status=TodoItemStatus.NEEDS_ACTION,
                due=parse_api_datetime(item.get("dueDate")),
                description=item.get("description"),
            )
            for item in self.coordinator.data.open_work_orders or []
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        start = dt_util.utcnow()
        due = item.due
        if isinstance(due, date) and not isinstance(due, datetime):
            due = dt_util.start_of_local_day(due)
        if due is None or due <= start:
            due = start + timedelta(days=1)
        body = {
            "name": item.summary,
            "description": item.description,
            "priority": 2,
            "startDate": start.isoformat(),
            "dueDate": due.isoformat(),
        }
        await self._call(self.coordinator.client.async_create_work_order({k: v for k, v in body.items() if v is not None}))

    async def async_update_todo_item(self, item: TodoItem) -> None:
        if item.status != TodoItemStatus.COMPLETED:
            raise HomeAssistantError("UteamUP work orders can only be completed from Home Assistant")
        await self._call(self.coordinator.client.async_complete_work_order(item.uid))

    async def _call(self, request) -> None:
        try:
            await request
        except UteamupError as err:
            raise HomeAssistantError(f"UteamUP rejected the change: {err}") from err
        await self.coordinator.async_request_refresh()
