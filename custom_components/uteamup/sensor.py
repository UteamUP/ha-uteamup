"""Work order, stock and IoT counts from UteamUP."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import UteamupConfigEntry, UteamupData
from .entity import UteamupEntity
from .const import DOMAIN


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
    if entry.runtime_data.telemetry:
        async_add_entities(UteamupTelemetrySensor(entry, key) for key in (
            "telemetry_sender", "telemetry_queue", "telemetry_quarantined", "telemetry_sources_available", "telemetry_last_success",
        ))


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


class UteamupTelemetrySensor(SensorEntity):
    """Diagnostics remain available when CMMS is down or a physical bulb is offline."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: UteamupConfigEntry, key: str) -> None:
        self._telemetry = entry.runtime_data.telemetry
        self._key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))}, name=entry.title,
            manufacturer="UteamUP", entry_type=DeviceEntryType.SERVICE,
        )
        if key == "telemetry_sender":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["starting", "running", "retrying", "blocked", "queue_full", "storage_error", "disabled"]
        elif key == "telemetry_last_success":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self._telemetry.signal, self.async_write_ha_state))

    @property
    def native_value(self):
        health = self._telemetry.health
        return {
            "telemetry_sender": health.sender,
            "telemetry_queue": health.queue.pending,
            "telemetry_quarantined": health.queue.quarantined,
            "telemetry_sources_available": sum(value == "available" for value in health.sources.values()),
            "telemetry_last_success": health.last_success,
        }[self._key]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._key == "telemetry_sources_available":
            return {"sources": {
                item.device_guid: {"name": item.name, "status": self._telemetry.health.sources.get(item.device_guid, "starting")}
                for item in self._telemetry.settings.mappings
            }}
        if self._key == "telemetry_queue":
            queue = self._telemetry.health.queue
            return {"bytes": queue.bytes, "blocked_devices": queue.blocked_devices,
                    "missed_samples": queue.missed_samples, "retrying_devices": queue.retrying_devices}
        return None
