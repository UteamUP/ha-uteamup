"""Config flow: connect one UteamUP tenant with a tenant API key."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, replace
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_client
from .api import (
    UteamupAuthError,
    UteamupClient,
    UteamupConnectionError,
    UteamupError,
    UteamupForbiddenError,
    UteamupMcpKeyError,
    UteamupPlanError,
    UteamupRateLimitError,
)
from .const import (
    CONF_API_SECRET,
    CONF_ASSIGNEE_GUID,
    CONF_ENABLE_WEBHOOKS,
    CONF_ENABLE_TELEMETRY,
    CONF_DEVICE_KEY,
    CONF_TELEMETRY,
    CONF_TENANT_GUID,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import UteamupConfigEntry
from .deco_clients import deco_clients, matching_assets, prepare_client_meters
from .services import valid_guid
from .measurement_flow import MeasurementOptionsFlow
from .telemetry_config import (
    NETWORK_METRICS, POWER_BASES, SOURCE_DOMAINS, DecoClientMapping, NetworkTelemetryMapping, TelemetryMapping,
    build_mapping, build_network_mapping, compatible_meter, environment_settings,
    mapping_source_choices, network_source_choices,
)

_LOGGER = logging.getLogger(__name__)

CREDENTIALS_SCHEMA = {
    vol.Required(CONF_API_KEY): vol.All(cv.string, vol.Length(min=32, max=32)),
    vol.Required(CONF_API_SECRET): vol.All(cv.string, vol.Length(min=32, max=64)),
}


def _connection_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, DEFAULT_BASE_URL)): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            **CREDENTIALS_SCHEMA,
            vol.Optional(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): cv.boolean,
        }
    )


class UteamupConfigFlow(ConfigFlow, domain=DOMAIN):
    """Validates the key by signing in; the key's tenant becomes the entry's unique id."""

    VERSION = 1

    async def _validate(self, data: dict[str, Any], errors: dict[str, str]) -> str | None:
        # Validated here, not in the schema: the frontend cannot render a cv.url validator.
        try:
            cv.url(data[CONF_URL])
        except vol.Invalid:
            errors[CONF_URL] = "invalid_url"
            return None
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


class UteamupOptionsFlow(MeasurementOptionsFlow, OptionsFlow):
    """Keep ordinary options compact; configure optional telemetry one device at a time."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._environment: dict[str, str] = {}
        self._devices: dict[str, dict[str, Any]] = {}
        self._selected: list[str] = []
        self._mappings: list[TelemetryMapping] = []
        self._network_devices: dict[str, dict[str, Any]] = {}
        self._network_selected: list[str] = []
        self._network_mappings: list[NetworkTelemetryMapping] = []
        self._attributes: list[dict[str, Any]] | None = None
        self._client: UteamupClient | None = None
        self._client_choices: dict[str, dict[str, Any]] = {}
        self._client_selected: list[str] = []
        self._client_links: list[dict[str, str]] = []
        self._assets: list[dict[str, Any]] | None = None
        self._client_selection_reviewed = False

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assignee = user_input.get(CONF_ASSIGNEE_GUID, "").strip()
            try:
                user_input[CONF_ASSIGNEE_GUID] = valid_guid(assignee) if assignee else ""
            except vol.Invalid:
                errors[CONF_ASSIGNEE_GUID] = "invalid_guid"
            else:
                configure_measurements = user_input.pop("configure_measurements", False)
                self._options = {**self.config_entry.options, **user_input}
                if configure_measurements:
                    return await self.async_step_measurement_device()
                if self._options.get(CONF_ENABLE_TELEMETRY, False):
                    return await self.async_step_telemetry_devices()
                return self.async_create_entry(data=self._options)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                ),
                vol.Required(CONF_ENABLE_WEBHOOKS, default=options.get(CONF_ENABLE_WEBHOOKS, True)): cv.boolean,
                vol.Optional(CONF_ASSIGNEE_GUID, default=options.get(CONF_ASSIGNEE_GUID, "")): cv.string,
                vol.Optional(
                    CONF_ENABLE_TELEMETRY,
                    description={"suggested_value": options.get(CONF_ENABLE_TELEMETRY, False)},
                ): cv.boolean,
                vol.Optional("configure_measurements", default=False): cv.boolean,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    async def async_step_telemetry_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Discover an approved endpoint and existing asset-bound devices; never provision."""
        errors: dict[str, str] = {}
        if not self._devices:
            self._client = create_client(self.hass, dict(self.config_entry.data))
            try:
                environment = await self._client.async_get_iot_environment()
                self._environment = environment_settings(environment, self.config_entry.data[CONF_TENANT_GUID])
                devices = await self._client.async_get_iot_devices()
                self._devices = {
                    item["externalGuid"]: item for item in devices
                    if item.get("isActive") and item.get("assetGuid") and item.get("macAddress")
                }
                if not self._devices:
                    errors["base"] = "no_devices"
            except UteamupAuthError:
                errors["base"] = "invalid_auth"
            except UteamupForbiddenError:
                errors["base"] = "telemetry_not_allowed"
            except UteamupError:
                errors["base"] = "cannot_connect"
            except (KeyError, ValueError, TypeError):
                errors["base"] = "environment_unavailable"
        if user_input is not None and not errors:
            self._selected = user_input["devices"]
            if any(item not in self._devices for item in self._selected):
                errors["base"] = "no_devices"
            else:
                if user_input.get("retry_quarantined", False):
                    self._environment["retry_nonce"] = str(uuid4())
                else:
                    self._environment["retry_nonce"] = self.config_entry.options.get(CONF_TELEMETRY, {}).get("retry_nonce", "")
                self._mappings = []
                if not self._selected:
                    return await self.async_step_telemetry_network_devices()
                return await self.async_step_telemetry_mapping()
        previous = self.config_entry.options.get(CONF_TELEMETRY, {}).get("mappings", [])
        schema = vol.Schema({
            vol.Required("devices", default=[item["device_guid"] for item in previous if item["device_guid"] in self._devices]): SelectSelector(
                SelectSelectorConfig(
                    options=[{"value": guid, "label": str(item["name"])} for guid, item in self._devices.items()],
                    multiple=True, mode=SelectSelectorMode.DROPDOWN, sort=True,
                )
            ),
            vol.Optional("retry_quarantined", default=False): cv.boolean,
        })
        return self.async_show_form(step_id="telemetry_devices", data_schema=schema, errors=errors)

    async def async_step_telemetry_mapping(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Review one physical device, its meter sources and its UteamUP attributes."""
        device = self._devices[self._selected[len(self._mappings)]]
        previous_data = next((
            item for item in self.config_entry.options.get(CONF_TELEMETRY, {}).get("mappings", [])
            if item["device_guid"] == device["externalGuid"]
        ), None)
        previous = TelemetryMapping(**previous_data) if previous_data else None
        errors: dict[str, str] = {}
        if self._attributes is None:
            try:
                assert self._client is not None
                self._attributes = await self._client.async_get_effective_attributes(device["assetGuid"])
            except UteamupForbiddenError:
                errors["base"] = "telemetry_not_allowed"
            except UteamupError:
                errors["base"] = "cannot_connect"
        if user_input is not None and not errors:
            try:
                mapping = build_mapping(self.hass, device, self._attributes or [], user_input, previous)
                if any(item.ha_device_id == mapping.ha_device_id for item in self._mappings):
                    raise ValueError("device_mismatch")
            except (ValueError, KeyError, TypeError) as err:
                errors["base"] = str(err) if str(err) in {
                    "source_missing", "device_mismatch", "unit_mismatch", "attribute_mismatch", "invalid_mapping",
                    "measured_source_required",
                } else "invalid_mapping"
            else:
                self._mappings.append(mapping)
                self._attributes = None
                if len(self._mappings) < len(self._selected):
                    return await self.async_step_telemetry_mapping()
                return await self.async_step_telemetry_confirm()

        defaults: dict[str, Any] = {}
        if previous:
            registry = er.async_get(self.hass)
            for name, registry_id in (
                ("source_entity", previous.source_registry_id), ("power_entity", previous.power_registry_id),
                ("energy_entity", previous.energy_registry_id),
            ):
                if entity := registry.async_get(registry_id):
                    defaults[name] = entity.entity_id
            defaults.update({
                "power_attribute_guid": previous.power_attribute_guid,
                "energy_attribute_guid": previous.energy_attribute_guid,
                "estimate_profile": previous.estimate_profile,
                "power_basis": previous.power_basis,
            })
        # A rejected form preserves selections, but never echoes the credential as a default.
        defaults.update({key: value for key, value in (user_input or {}).items() if key != CONF_DEVICE_KEY})
        try:
            choices = mapping_source_choices(self.hass, device)
        except ValueError:
            errors["base"] = "device_mismatch"
            choices = {name: [] for name in ("source_entity", "power_entity", "energy_entity")}
        fields: dict[Any, Any] = {}
        for name, domain in (("source_entity", list(SOURCE_DOMAINS)), ("power_entity", "sensor"), ("energy_entity", "sensor")):
            if name not in defaults and len(choices[name]) == 1:
                defaults[name] = choices[name][0]
            fields[vol.Required(name, description={"suggested_value": defaults.get(name)})] = EntitySelector(
                EntitySelectorConfig(domain=domain, include_entities=choices[name])
            )
        for name, unit in (("power_attribute_guid", "W"), ("energy_attribute_guid", "kWh")):
            choices = {
                item["attributeDefinitionGuid"]: item for group in self._attributes or []
                for item in group.get("definitions", []) if compatible_meter(item, unit)
            }
            fields[vol.Required(name, description={"suggested_value": defaults.get(name)})] = SelectSelector(
                SelectSelectorConfig(options=[
                    {"value": guid, "label": f"{item['name']} ({unit})"} for guid, item in choices.items()
                ], mode=SelectSelectorMode.DROPDOWN)
            )
            if not choices:
                errors.setdefault("base", "no_telemetry_meters")
        fields[vol.Required("power_basis", default=defaults.get("power_basis", "estimated"))] = SelectSelector(
            SelectSelectorConfig(options=list(POWER_BASES), translation_key="power_basis", mode=SelectSelectorMode.DROPDOWN)
        )
        fields[vol.Optional("estimate_profile", default=defaults.get("estimate_profile", ""))] = cv.string
        fields[vol.Optional(CONF_DEVICE_KEY)] = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        return self.async_show_form(
            step_id="telemetry_mapping", data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={"device": str(device["name"]), "position": str(len(self._mappings) + 1), "count": str(len(self._selected))},
        )

    async def async_step_telemetry_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Make endpoint and measurement semantics visible before enabling traffic."""
        if user_input is not None:
            return await self.async_step_telemetry_network_devices()
        return self.async_show_form(
            step_id="telemetry_confirm", data_schema=vol.Schema({}),
            description_placeholders={"count": str(len(self._mappings)), "endpoint": self._environment["endpoint"]},
        )

    async def async_step_telemetry_network_devices(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Optionally add Deco network mappings without changing energy mappings."""
        if not self._network_devices:
            for guid, device in self._devices.items():
                try:
                    choices = network_source_choices(self.hass, device)
                except ValueError:
                    continue
                if choices["tracker_entity"]:
                    self._network_devices[guid] = device
        if not self._network_devices:
            self._network_mappings = [
                NetworkTelemetryMapping.from_dict(item)
                for item in self.config_entry.options.get(CONF_TELEMETRY, {}).get("network_mappings", [])
            ]
            return self._finish_telemetry()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._network_selected = user_input.get("devices", [])
            if any(item not in self._network_devices for item in self._network_selected):
                errors["base"] = "no_devices"
            elif not self._network_selected:
                return self._finish_telemetry()
            else:
                self._network_mappings = []
                self._attributes = None
                return await self.async_step_telemetry_network_mapping()
        previous = self.config_entry.options.get(CONF_TELEMETRY, {}).get("network_mappings", [])
        schema = vol.Schema({
            vol.Optional(
                "devices",
                default=[item["device_guid"] for item in previous if item["device_guid"] in self._network_devices],
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": guid, "label": str(item["name"])}
                        for guid, item in self._network_devices.items()
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=True,
                )
            ),
        })
        return self.async_show_form(step_id="telemetry_network_devices", data_schema=schema, errors=errors)

    async def async_step_telemetry_network_mapping(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Review one Deco's tracker, five network sources and their meters."""
        device = self._network_devices[self._network_selected[len(self._network_mappings)]]
        previous_data = next((
            item for item in self.config_entry.options.get(CONF_TELEMETRY, {}).get("network_mappings", [])
            if item["device_guid"] == device["externalGuid"]
        ), None)
        previous = NetworkTelemetryMapping.from_dict(previous_data) if previous_data else None
        errors: dict[str, str] = {}
        if self._attributes is None:
            try:
                assert self._client is not None
                self._attributes = await self._client.async_get_effective_attributes(device["assetGuid"])
            except UteamupForbiddenError:
                errors["base"] = "telemetry_not_allowed"
            except UteamupError:
                errors["base"] = "cannot_connect"
        if user_input is not None and not errors:
            try:
                mapping = build_network_mapping(self.hass, device, self._attributes or [], user_input, previous)
                if any(
                    item.ha_device_id == mapping.ha_device_id
                    for item in (*self._mappings, *self._network_mappings)
                ):
                    raise ValueError("device_mismatch")
            except (ValueError, KeyError, TypeError) as err:
                errors["base"] = str(err) if str(err) in {
                    "source_missing", "device_mismatch", "unit_mismatch", "attribute_mismatch",
                    "invalid_mapping", "invalid_value", "unavailable",
                } else "invalid_mapping"
            else:
                self._network_mappings.append(mapping)
                self._attributes = None
                if len(self._network_mappings) < len(self._network_selected):
                    return await self.async_step_telemetry_network_mapping()
                return await self.async_step_telemetry_clients()

        defaults: dict[str, Any] = {}
        if previous:
            registry = er.async_get(self.hass)
            for name in ("tracker", *(name for name, _, _ in NETWORK_METRICS)):
                registry_id = getattr(previous, f"{name}_registry_id")
                if entity := registry.async_get(registry_id):
                    defaults[f"{name}_entity"] = entity.entity_id
            for name, _, _ in NETWORK_METRICS:
                defaults[f"{name}_attribute_guid"] = getattr(previous, f"{name}_attribute_guid")
        defaults.update({key: value for key, value in (user_input or {}).items() if key != CONF_DEVICE_KEY})
        try:
            choices = network_source_choices(self.hass, device)
        except ValueError:
            errors["base"] = "device_mismatch"
            choices = {"tracker_entity": [], **{f"{name}_entity": [] for name, _, _ in NETWORK_METRICS}}
        fields: dict[Any, Any] = {}
        for name, domain in (
            ("tracker", "device_tracker"),
            *((name, domain) for name, domain, _ in NETWORK_METRICS),
        ):
            field_name = f"{name}_entity"
            if field_name not in defaults and len(choices[field_name]) == 1:
                defaults[field_name] = choices[field_name][0]
            fields[vol.Required(field_name, description={"suggested_value": defaults.get(field_name)})] = EntitySelector(
                EntitySelectorConfig(domain=domain, include_entities=choices[field_name])
            )
        for name, _, unit in NETWORK_METRICS:
            field_name = f"{name}_attribute_guid"
            options = {
                item["attributeDefinitionGuid"]: item for group in self._attributes or []
                for item in group.get("definitions", []) if compatible_meter(item, unit)
            }
            fields[vol.Required(field_name, description={"suggested_value": defaults.get(field_name)})] = SelectSelector(
                SelectSelectorConfig(options=[
                    {"value": guid, "label": f"{item['name']} ({unit})"} for guid, item in options.items()
                ], mode=SelectSelectorMode.DROPDOWN)
            )
            if not options:
                errors.setdefault("base", "no_network_meters")
        fields[vol.Optional(CONF_DEVICE_KEY)] = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        return self.async_show_form(
            step_id="telemetry_network_mapping",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "device": str(device["name"]),
                "position": str(len(self._network_mappings) + 1),
                "count": str(len(self._network_selected)),
            },
        )

    async def async_step_telemetry_network_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                assert self._client is not None
                prepared: dict[str, list[DecoClientMapping]] = {}
                for link in self._client_links:
                    router = next(item for item in self._network_mappings if item.device_guid == link["router_guid"])
                    current = deco_clients(self.hass, router.tracker_registry_id, dt_util.utcnow())
                    if not any(item["trackerRegistryId"] == link["tracker_registry_id"]
                               and item["macAddress"] == link["mac_address"] for item in current):
                        raise ValueError("device_mismatch")
                    meters = await prepare_client_meters(self._client, link["asset_guid"])
                    prepared.setdefault(link["router_guid"], []).append(DecoClientMapping(
                        tracker_registry_id=link["tracker_registry_id"], mac_address=link["mac_address"],
                        asset_guid=link["asset_guid"], **meters,
                    ))
                if self._client_selection_reviewed:
                    self._network_mappings = [replace(item, clients=tuple(prepared.get(item.device_guid, [])))
                                              for item in self._network_mappings]
                return self._finish_telemetry()
            except UteamupForbiddenError:
                errors["base"] = "client_setup_not_allowed"
            except UteamupError:
                errors["base"] = "client_setup_failed"
            except ValueError as err:
                errors["base"] = str(err) if str(err) in {"ambiguous_client_type", "device_mismatch"} else "attribute_mismatch"
        return self.async_show_form(
            step_id="telemetry_network_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "count": str(len(self._network_mappings)),
                "clients": str(len(self._client_links) if self._client_selection_reviewed
                               else sum(len(item.clients) for item in self._network_mappings)),
                "endpoint": self._environment["endpoint"],
            },
        )

    async def async_step_telemetry_clients(self, user_input=None) -> ConfigFlowResult:
        if not self._client_choices:
            seen = set()
            for mapping in self._network_mappings:
                for client in deco_clients(self.hass, mapping.tracker_registry_id, dt_util.utcnow()):
                    registry_id = client["trackerRegistryId"]
                    if registry_id not in seen:
                        self._client_choices[registry_id] = {**client, "routerGuid": mapping.device_guid}
                        seen.add(registry_id)
        if not self._client_choices:
            return await self.async_step_telemetry_network_confirm()
        errors = {}
        if user_input is not None:
            self._client_selected = user_input.get("clients", [])
            if len(self._client_selected) > 200 or any(item not in self._client_choices for item in self._client_selected):
                errors["base"] = "invalid_client_selection"
            else:
                self._client_selection_reviewed = True
                self._client_links = []
                if self._client_selected:
                    return await self.async_step_telemetry_client_asset()
                return await self.async_step_telemetry_network_confirm()
        defaults = [client.tracker_registry_id for mapping in self._network_mappings for client in mapping.clients
                    if client.tracker_registry_id in self._client_choices]
        return self.async_show_form(step_id="telemetry_clients", errors=errors, data_schema=vol.Schema({
            vol.Optional("clients", default=defaults): SelectSelector(SelectSelectorConfig(
                options=[{"value": key, "label": f"{item['name']} · {item['macAddress']} · {item['ipAddress'] or '—'}"}
                         for key, item in self._client_choices.items()], multiple=True, mode=SelectSelectorMode.DROPDOWN,
            )),
        }))

    async def async_step_telemetry_client_asset(self, user_input=None) -> ConfigFlowResult:
        client = self._client_choices[self._client_selected[len(self._client_links)]]
        errors = {}
        if self._assets is None:
            try:
                assert self._client is not None
                self._assets = await self._client.async_get_assets()
            except UteamupForbiddenError:
                errors["base"] = "client_setup_not_allowed"
            except UteamupError:
                errors["base"] = "cannot_connect"
        if user_input is not None and not errors:
            asset_guid = user_input["asset_guid"]
            if (not any(asset["externalGuid"] == asset_guid for asset in self._assets or [])
                    or any(link["asset_guid"] == asset_guid for link in self._client_links)):
                errors["base"] = "client_asset_conflict"
            else:
                self._client_links.append({"router_guid": client["routerGuid"], "asset_guid": asset_guid,
                                           "mac_address": client["macAddress"],
                                           "tracker_registry_id": client["trackerRegistryId"]})
                if len(self._client_links) < len(self._client_selected):
                    return await self.async_step_telemetry_client_asset()
                return await self.async_step_telemetry_network_confirm()
        matches = matching_assets(client, self._assets or [])
        previous = next((item.asset_guid for mapping in self._network_mappings for item in mapping.clients
                         if item.tracker_registry_id == client["trackerRegistryId"]), None)
        suggestion = (user_input or {}).get("asset_guid", previous or (matches[0]["externalGuid"] if len(matches) == 1 else None))
        return self.async_show_form(step_id="telemetry_client_asset", errors=errors, data_schema=vol.Schema({
            vol.Required("asset_guid", description={"suggested_value": suggestion}): SelectSelector(SelectSelectorConfig(
                options=[{"value": item["externalGuid"], "label": item["name"]} for item in self._assets or []],
                mode=SelectSelectorMode.DROPDOWN, sort=True,
            )),
        }), description_placeholders={"client": client["name"], "mac": client["macAddress"],
                                      "position": str(len(self._client_links) + 1), "count": str(len(self._client_selected))})

    def _finish_telemetry(self) -> ConfigFlowResult:
        if (not self._mappings and not self._network_mappings
                and not self.config_entry.options.get(CONF_TELEMETRY, {}).get("measurement_mappings")):
            return self.async_show_form(step_id="telemetry_network_devices", data_schema=vol.Schema({}),
                                        errors={"base": "no_devices"})
        return self.async_create_entry(data={
            **self._options,
            CONF_TELEMETRY: {
                **self._environment,
                "mappings": [asdict(item) for item in self._mappings],
                "network_mappings": [asdict(item) for item in self._network_mappings],
                "measurement_mappings": self.config_entry.options.get(CONF_TELEMETRY, {}).get("measurement_mappings", []),
            },
        })
