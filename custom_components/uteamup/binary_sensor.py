"""Problem flags derived from UteamUP alerts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import UteamupConfigEntry, UteamupData
from .entity import UteamupEntity


@dataclass(frozen=True, kw_only=True)
class UteamupBinarySensorDescription(BinarySensorEntityDescription):
    """A flag read from one section of the refresh."""

    section: str
    is_on_fn: Callable[[UteamupData], bool | None]


BINARY_SENSORS: tuple[UteamupBinarySensorDescription, ...] = (
    UteamupBinarySensorDescription(
        key="stock_critical",
        section="stock_alerts",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: None if data.stock_alerts is None else data.stock_alerts.get("criticalCount", 0) > 0,
    ),
    UteamupBinarySensorDescription(
        key="iot_alert_active",
        section="iot_alerts",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: None if data.iot_alerts is None else data.iot_alerts.get("totalItems", 0) > 0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: UteamupConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(UteamupBinarySensor(coordinator, description) for description in BINARY_SENSORS)


class UteamupBinarySensor(UteamupEntity, BinarySensorEntity):
    """A flag that is unavailable while the key may not read its section."""

    entity_description: UteamupBinarySensorDescription

    def __init__(self, coordinator, description: UteamupBinarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.section not in self.coordinator.data.denied

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.coordinator.data)
