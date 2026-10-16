"""Work order, stock and IoT counts from UteamUP."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import UteamupConfigEntry, UteamupData
from .entity import UteamupEntity


@dataclass(frozen=True, kw_only=True)
class UteamupSensorDescription(SensorEntityDescription):
    """A count read from one section of the refresh."""

    section: str
    value_fn: Callable[[UteamupData], int | None]
    attributes_fn: Callable[[UteamupData], dict[str, Any]] | None = None


def _work_orders(data: UteamupData, field: str) -> int | None:
    section = (data.analytics or {}).get("workorders")
    return None if section is None else section.get(field)


SENSORS: tuple[UteamupSensorDescription, ...] = (
    UteamupSensorDescription(
        key="open_work_orders",
        section="analytics",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _work_orders(data, "open"),
    ),
    UteamupSensorDescription(
        key="in_progress_work_orders",
        section="analytics",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _work_orders(data, "inProgress"),
    ),
    UteamupSensorDescription(
        key="on_hold_work_orders",
        section="analytics",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _work_orders(data, "onHold"),
    ),
    UteamupSensorDescription(
        key="overdue_work_orders",
        section="analytics",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _work_orders(data, "overdue"),
    ),
    UteamupSensorDescription(
        key="stock_alerts",
        section="stock_alerts",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (data.stock_alerts or {}).get("totalActive"),
        attributes_fn=lambda data: {
            "critical": (data.stock_alerts or {}).get("criticalCount"),
            "low_stock": (data.stock_alerts or {}).get("lowStockCount"),
            "stockout": (data.stock_alerts or {}).get("stockoutCount"),
            "action_required": (data.stock_alerts or {}).get("actionRequiredCount"),
        },
    ),
    UteamupSensorDescription(
        key="iot_alerts",
        section="iot_alerts",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (data.iot_alerts or {}).get("totalItems"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: UteamupConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(UteamupSensor(coordinator, description) for description in SENSORS)


class UteamupSensor(UteamupEntity, SensorEntity):
    """A count that is unavailable while the key may not read its section."""

    entity_description: UteamupSensorDescription

    def __init__(self, coordinator, description: UteamupSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.entity_description.section not in self.coordinator.data.denied
            and self.native_value is not None
        )

    @property
    def native_value(self) -> int | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)
