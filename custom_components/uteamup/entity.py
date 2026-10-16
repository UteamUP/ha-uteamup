"""Base entity for UteamUP."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import UteamupCoordinator


def parse_api_datetime(value: str | None) -> datetime | None:
    """UteamUP stores times in UTC and may omit the offset."""
    parsed = dt_util.parse_datetime(value) if value else None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class UteamupEntity(CoordinatorEntity[UteamupCoordinator]):
    """An entity on the tenant's service device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UteamupCoordinator, key: str) -> None:
        super().__init__(coordinator)
        tenant_guid = coordinator.config_entry.unique_id
        self._attr_unique_id = f"{tenant_guid}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(tenant_guid))},
            name=coordinator.config_entry.title,
            manufacturer="UteamUP",
            entry_type=DeviceEntryType.SERVICE,
        )
