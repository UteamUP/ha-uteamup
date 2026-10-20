"""Polls UteamUP for the status shown in Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    UteamupAuthError,
    UteamupClient,
    UteamupError,
    UteamupForbiddenError,
    UteamupNotFoundError,
)
from .const import CONF_ASSIGNEE_GUID, DEFAULT_SCAN_INTERVAL, DOMAIN

if TYPE_CHECKING:
    from .telemetry import UteamupTelemetry

_LOGGER = logging.getLogger(__name__)


@dataclass
class UteamupData:
    """One refresh of every section the API key is allowed to read. A denied section is None."""

    analytics: dict[str, Any] | None = None
    stock_alerts: dict[str, Any] | None = None
    iot_alerts: dict[str, Any] | None = None
    open_work_orders: list[dict[str, Any]] | None = None
    denied: set[str] = field(default_factory=set)


@dataclass
class UteamupRuntimeData:
    """Objects one config entry keeps while it is loaded."""

    client: UteamupClient
    coordinator: UteamupCoordinator
    telemetry: UteamupTelemetry | None = None


type UteamupConfigEntry = ConfigEntry[UteamupRuntimeData]


class UteamupCoordinator(DataUpdateCoordinator[UteamupData]):
    """Fetches all sections in one refresh; a 403 disables only that section."""

    config_entry: UteamupConfigEntry

    def __init__(self, hass: HomeAssistant, entry: UteamupConfigEntry, client: UteamupClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        )
        self.client = client
        self._reported_denials: set[str] = set()

    async def _async_update_data(self) -> UteamupData:
        sections = {
            "analytics": self.client.async_get_analytics_summary(),
            "stock_alerts": self.client.async_get_stock_alert_summary(),
            "iot_alerts": self.client.async_get_iot_alerts(),
            "open_work_orders": self.client.async_get_open_work_orders(),
        }
        results = await asyncio.gather(*sections.values(), return_exceptions=True)
        data = UteamupData()
        for name, result in zip(sections, results, strict=True):
            if isinstance(result, UteamupAuthError):
                raise ConfigEntryAuthFailed(str(result)) from result
            if isinstance(result, (UteamupForbiddenError, UteamupNotFoundError)):
                data.denied.add(name)
                if name not in self._reported_denials:
                    self._reported_denials.add(name)
                    _LOGGER.warning(
                        "The UteamUP API key may not read %s (%s); its entities stay unavailable. "
                        "Grant the permission to the key's role and its creator to enable them",
                        name,
                        result,
                    )
                continue
            if isinstance(result, UteamupError):
                raise UpdateFailed(str(result)) from result
            if isinstance(result, BaseException):
                raise result
            if name == "open_work_orders":
                result = self._filter_assignee(result.get("items", []))
            setattr(data, name, result)
        return data

    def _filter_assignee(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assignee = self.config_entry.options.get(CONF_ASSIGNEE_GUID)
        if not assignee:
            return items
        return [item for item in items if str(item.get("primaryAssigneeGuid", "")).lower() == assignee.lower()]
