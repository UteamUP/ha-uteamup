"""The UteamUP integration: maintenance work orders, assets and meters in Home Assistant."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import UteamupAuthError, UteamupClient, UteamupError
from .const import CONF_API_SECRET, CONF_ENABLE_WEBHOOKS, CONF_TENANT_GUID, DOMAIN
from .coordinator import UteamupConfigEntry, UteamupCoordinator, UteamupRuntimeData
from .services import async_setup_services
from .webhooks import async_remove_webhook, async_setup_webhook, async_unload_webhook

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.SENSOR, Platform.TODO]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


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
    client = create_client(hass, dict(entry.data))
    try:
        await client.async_login()
    except UteamupAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except UteamupError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = UteamupCoordinator(hass, entry, client)
    entry.runtime_data = UteamupRuntimeData(client, coordinator)
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if entry.options.get(CONF_ENABLE_WEBHOOKS, True):
        await async_setup_webhook(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: UteamupConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: UteamupConfigEntry) -> bool:
    async_unload_webhook(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: UteamupConfigEntry) -> None:
    await async_remove_webhook(hass, entry, create_client(hass, dict(entry.data)))
