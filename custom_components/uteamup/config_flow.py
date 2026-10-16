"""Config flow: connect one UteamUP tenant with a tenant API key."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from . import create_client
from .api import (
    UteamupAuthError,
    UteamupConnectionError,
    UteamupMcpKeyError,
    UteamupPlanError,
    UteamupRateLimitError,
)
from .const import (
    CONF_API_SECRET,
    CONF_ASSIGNEE_GUID,
    CONF_ENABLE_WEBHOOKS,
    CONF_TENANT_GUID,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import UteamupConfigEntry
from .services import valid_guid

_LOGGER = logging.getLogger(__name__)

CREDENTIALS_SCHEMA = {
    vol.Required(CONF_API_KEY): vol.All(cv.string, vol.Length(min=32, max=32)),
    vol.Required(CONF_API_SECRET): vol.All(cv.string, vol.Length(min=32, max=64)),
}


def _connection_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, DEFAULT_BASE_URL)): cv.url,
            **CREDENTIALS_SCHEMA,
            vol.Optional(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): cv.boolean,
        }
    )


class UteamupConfigFlow(ConfigFlow, domain=DOMAIN):
    """Validates the key by signing in; the key's tenant becomes the entry's unique id."""

    VERSION = 1

    async def _validate(self, data: dict[str, Any], errors: dict[str, str]) -> str | None:
        try:
            return await create_client(self.hass, data).async_login()
        except UteamupMcpKeyError:
            errors["base"] = "mcp_key_not_allowed"
        except UteamupAuthError:
            errors["base"] = "invalid_auth"
        except UteamupPlanError:
            errors["base"] = "plan_required"
        except UteamupRateLimitError:
            errors["base"] = "rate_limited"
        except UteamupConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error signing in to UteamUP")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if tenant_guid := await self._validate(user_input, errors):
                await self.async_set_unique_id(tenant_guid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"UteamUP {tenant_guid[:8]}", data={**user_input, CONF_TENANT_GUID: tenant_guid}
                )
        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(user_input or {}), errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            if tenant_guid := await self._validate(data, errors):
                await self.async_set_unique_id(tenant_guid)
                self._abort_if_unique_id_mismatch(reason="wrong_tenant")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema(CREDENTIALS_SCHEMA), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            if tenant_guid := await self._validate(data, errors):
                await self.async_set_unique_id(tenant_guid)
                self._abort_if_unique_id_mismatch(reason="wrong_tenant")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reconfigure", data_schema=_connection_schema(user_input or entry.data), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: UteamupConfigEntry) -> UteamupOptionsFlow:
        return UteamupOptionsFlow()


class UteamupOptionsFlow(OptionsFlow):
    """Polling interval, live events and the to-do list's assignee filter."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assignee = user_input.get(CONF_ASSIGNEE_GUID, "").strip()
            try:
                user_input[CONF_ASSIGNEE_GUID] = valid_guid(assignee) if assignee else ""
            except vol.Invalid:
                errors[CONF_ASSIGNEE_GUID] = "invalid_guid"
            else:
                return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                ),
                vol.Required(CONF_ENABLE_WEBHOOKS, default=options.get(CONF_ENABLE_WEBHOOKS, True)): cv.boolean,
                vol.Optional(CONF_ASSIGNEE_GUID, default=options.get(CONF_ASSIGNEE_GUID, "")): cv.string,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
