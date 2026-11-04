"""Native options for a reviewed direct Matter collector."""

from copy import deepcopy
from collections.abc import Mapping
from dataclasses import asdict

import voluptuous as vol
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig, TextSelector, TextSelectorConfig, TextSelectorType

from . import create_client
from .api import UteamupError, UteamupForbiddenError
from .const import CONF_COMPONENT_TELEMETRY, CONF_DEVICE_KEY, CONF_ENABLE_TELEMETRY, CONF_TELEMETRY, CONF_TENANT_GUID
from .measurements import build_component_selections
from .telemetry_config import ComponentTelemetrySettings, TelemetrySettings, environment_settings, public_guid


class ComponentOptionsFlow:
    """Extend the existing options flow without writing to the stamp or CMMS during setup."""

    async def _component_environment_for(self, source: str, generation: str) -> dict[str, str]:
        if self._client is None:
            self._client = create_client(self.hass, dict(self.config_entry.data))
        environment = environment_settings(await self._client.async_get_iot_environment(), self.config_entry.data[CONF_TENANT_GUID])
        rows = await self._client.async_get_iot_source_generations(source)
        if not any(row.get("sourceKind") == "homeassistant" and row.get("retiredAt") is None
                   and row.get("sourceInstallationGuid") == source and row.get("sourceGenerationGuid") == generation for row in rows):
            raise ValueError("component_generation_missing")
        return environment

    @staticmethod
    def _component_form_error(error: Exception) -> str:
        if isinstance(error, UteamupForbiddenError):
            return "component_not_allowed"
        if isinstance(error, UteamupError):
            return "cannot_connect"
        if isinstance(error, ValueError) and str(error) in {
            "component_generation_missing", "environment_unavailable", "invalid_component_selection", "component_configuration_changed",
        }:
            return str(error)
        return "invalid_component_settings"

    def _component_current_options(self) -> dict:
        return dict(self.config_entry.options.get(CONF_TELEMETRY, {}))

    async def async_step_component_collector(self, user_input=None):
        errors = {}
        prior = self._component_current_options()
        previous_raw = prior.get(CONF_COMPONENT_TELEMETRY)
        previous = dict(previous_raw) if isinstance(previous_raw, Mapping) else {}
        if user_input is not None:
            self._component_options_before = deepcopy(dict(self.config_entry.options))
            self._component_prior = deepcopy(prior)
            if user_input.get("disable_component_telemetry") and previous_raw is not None:
                return await self.async_step_component_disable()
            try:
                source = public_guid(user_input.get("source_installation_guid"))
                generation = public_guid(user_input.get("source_generation_guid"))
                environment = await self._component_environment_for(source, generation)
                same_collector = (source == previous.get("source_installation_guid") and generation == previous.get("source_generation_guid")
                                  and environment["environment_guid"] == previous.get("environment_guid"))
                key = user_input.get(CONF_DEVICE_KEY) or (previous.get(CONF_DEVICE_KEY) if same_collector else "")
                if not isinstance(key, str) or not 8 <= len(key) <= 500 or any(not 32 < ord(character) < 127 for character in key):
                    raise ValueError("invalid_component_settings")
                self._component_collector = (source, generation, key)
                self._component_environment = environment
                return await self.async_step_component_sources()
            except (KeyError, TypeError, ValueError, UteamupError) as error:
                errors["base"] = self._component_form_error(error)
        fields = {
            vol.Optional("source_installation_guid", default=(user_input or previous).get("source_installation_guid", "")): cv.string,
            vol.Optional("source_generation_guid", default=(user_input or previous).get("source_generation_guid", "")): cv.string,
            vol.Optional(CONF_DEVICE_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }
        if previous_raw is not None:
            fields[vol.Optional("disable_component_telemetry", default=False)] = cv.boolean
        return self.async_show_form(step_id="component_collector", data_schema=vol.Schema(fields), errors=errors)

    async def async_step_component_sources(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._component_source_input = deepcopy(user_input)
                self._component_reviewed = self._component_settings_from_selection()
                return await self.async_step_component_confirm()
            except (KeyError, TypeError, ValueError) as error:
                errors["base"] = self._component_form_error(error)
        try:
            previous = ComponentTelemetrySettings.from_dict(self._component_prior.get(CONF_COMPONENT_TELEMETRY))
            readings = [item for selection in previous.selections for item in selection.readings]
        except (KeyError, TypeError, ValueError):
            readings = []
        registry = er.async_get(self.hass)
        prior_entities = list(dict.fromkeys(entity.entity_id for item in readings
                                            if (entity := registry.async_get(item.source_registry_id)) is not None))
        return self.async_show_form(step_id="component_sources", errors=errors, data_schema=vol.Schema({
            vol.Required("entities", default=(user_input or {}).get("entities", prior_entities)): EntitySelector(
                EntitySelectorConfig(integration="matter", domain=["light", "switch", "binary_sensor", "sensor"], multiple=True)),
            vol.Optional("availability", default=(user_input or {}).get("availability", any(
                item.source_attribute == "availability" for item in readings))): cv.boolean,
            vol.Optional("light_attributes", default=(user_input or {}).get("light_attributes", any(
                item.source_attribute in {"brightness", "color_temp_kelvin"} for item in readings))): cv.boolean,
        }))

    def _component_settings_from_selection(self) -> ComponentTelemetrySettings:
        source, generation, key = self._component_collector
        values = self._component_source_input
        selections = build_component_selections(self.hass, values["entities"], availability=values.get("availability", False),
                                                 light_attributes=values.get("light_attributes", False))
        settings = ComponentTelemetrySettings(source, generation, self._component_environment["environment_guid"], selections, key)
        return ComponentTelemetrySettings.from_dict(asdict(settings))

    async def async_step_component_confirm(self, user_input=None):
        errors = {}
        reviewed = self._component_reviewed
        if user_input is not None:
            try:
                environment = await self._component_environment_for(reviewed.source_installation_guid, reviewed.source_generation_guid)
                if (dict(self.config_entry.options) != self._component_options_before or environment != self._component_environment
                        or self._component_settings_from_selection() != reviewed):
                    raise ValueError("component_configuration_changed")
                saved = {**self._component_prior, **environment, CONF_COMPONENT_TELEMETRY: asdict(reviewed)}
                TelemetrySettings.from_dict(saved, self.config_entry.data[CONF_TENANT_GUID])
                return self.async_create_entry(data={**self._options, CONF_ENABLE_TELEMETRY: True, CONF_TELEMETRY: saved})
            except (KeyError, TypeError, ValueError, UteamupError) as error:
                errors["base"] = self._component_form_error(error)
        return self.async_show_form(step_id="component_confirm", data_schema=vol.Schema({}), errors=errors,
                                    description_placeholders={"devices": str(len(reviewed.selections)),
                                        "readings": str(sum(len(selection.readings) for selection in reviewed.selections)),
                                        "endpoint": self._component_environment["endpoint"]})

    async def async_step_component_disable(self, user_input=None):
        if user_input is not None:
            if dict(self.config_entry.options) != self._component_options_before:
                return self.async_show_form(step_id="component_disable", data_schema=vol.Schema({}),
                                            errors={"base": "component_configuration_changed"})
            saved = dict(self._component_prior)
            saved.pop(CONF_COMPONENT_TELEMETRY, None)
            has_legacy = any(saved.get(key) for key in ("mappings", "network_mappings", "measurement_mappings"))
            enabled = bool(self.config_entry.options.get(CONF_ENABLE_TELEMETRY) and has_legacy)
            return self.async_create_entry(data={**self._options, CONF_ENABLE_TELEMETRY: enabled, CONF_TELEMETRY: saved})
        return self.async_show_form(step_id="component_disable", data_schema=vol.Schema({}))
