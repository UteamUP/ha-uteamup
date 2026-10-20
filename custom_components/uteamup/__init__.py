"""The UteamUP integration: maintenance work orders, assets and meters in Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import UteamupAuthError, UteamupClient, UteamupError
from .const import CONF_API_SECRET, CONF_ENABLE_TELEMETRY, CONF_ENABLE_WEBHOOKS, CONF_TELEMETRY, CONF_TENANT_GUID, DOMAIN
from .coordinator import UteamupConfigEntry, UteamupCoordinator, UteamupData, UteamupRuntimeData
from .services import async_setup_services
from .telemetry import UteamupTelemetry
from .telemetry_config import TelemetrySettings
from .webhooks import async_remove_webhook, async_setup_webhook, async_unload_webhook

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.SENSOR, Platform.TODO]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


def create_client(hass: HomeAssistant, data: dict) -> UteamupClient:
    return UteamupClient(
        async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, True)),
        data[CONF_URL],
        data[CONF_API_KEY],
        data[CONF_API_SECRET],
        data.get(CONF_TENANT_GUID),
    )


async def async_setup_entry(hass: HomeAssistant, entry: UteamupConfigEntry) -> bool:
    settings = None
    if entry.options.get(CONF_ENABLE_TELEMETRY, False):
        try:
            settings = TelemetrySettings.from_dict(dict(entry.options[CONF_TELEMETRY]), entry.data[CONF_TENANT_GUID])
        except (KeyError, ValueError, TypeError) as err:
            raise ConfigEntryNotReady("Review the saved dedicated telemetry mappings in UteamUP options") from err
    client = create_client(hass, dict(entry.data))
    cmms_error = None
    try:
        await client.async_login()
    except UteamupAuthError as err:
        if settings is None:
            raise ConfigEntryAuthFailed(str(err)) from err
        cmms_error = err
        entry.async_start_reauth(hass)
    except UteamupError as err:
        if settings is None:
            raise ConfigEntryNotReady(str(err)) from err
        cmms_error = err

    coordinator = UteamupCoordinator(hass, entry, client)
    telemetry = UteamupTelemetry(hass, entry, settings) if settings else None
    entry.runtime_data = UteamupRuntimeData(client, coordinator, telemetry)
    if settings:
        # The previously reviewed narrow device keys can deliver queued readings during
        # a CMMS outage. Polling entities stay unavailable and recover independently.
        coordinator.data = UteamupData()
        if cmms_error:
            coordinator.last_update_success = False
            coordinator.last_exception = cmms_error
            _LOGGER.warning("UteamUP CMMS is unavailable; cached dedicated telemetry remains independent")
        else:
            await coordinator.async_refresh()
    else:
        await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if entry.options.get(CONF_ENABLE_WEBHOOKS, True) and cmms_error is None:
        await async_setup_webhook(hass, entry)
    if telemetry:
        await telemetry.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: UteamupConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: UteamupConfigEntry) -> bool:
    async_unload_webhook(hass, entry)
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if entry.runtime_data.telemetry:
        await entry.runtime_data.telemetry.async_stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: UteamupConfigEntry) -> None:
    await async_remove_webhook(hass, entry, create_client(hass, dict(entry.data)))
