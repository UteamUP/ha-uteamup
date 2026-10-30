"""Review general measurements without reconfiguring existing energy or Deco mappings."""

from dataclasses import asdict

import voluptuous as vol
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector, EntitySelector, EntitySelectorConfig, SelectSelector, SelectSelectorConfig,
    TextSelector, TextSelectorConfig, TextSelectorType,
)

from . import create_client
from .api import UteamupError, UteamupForbiddenError
from .const import CONF_ENABLE_TELEMETRY, CONF_TELEMETRY, CONF_TENANT_GUID
from .measurements import build_measurement_source, measurement_choices, measurement_states
from .telemetry_config import MeasurementMapping, TelemetrySettings, compatible_meter, environment_settings, source_unit


class MeasurementOptionsFlow:
    """Mixin for the integration's existing options flow; saves only the reviewed device."""

    async def async_step_measurement_device(self, user_input=None):
        errors = {}
        if self._client is None:
            self._client = create_client(self.hass, dict(self.config_entry.data))
        try:
            self._environment = environment_settings(await self._client.async_get_iot_environment(),
                                                     self.config_entry.data[CONF_TENANT_GUID])
            devices = await self._client.async_get_iot_devices()
            saved = self.config_entry.options.get(CONF_TELEMETRY, {})
            used = {item["device_guid"] for key in ("mappings", "network_mappings") for item in saved.get(key, [])}
            self._devices = {item["externalGuid"]: item for item in devices
                             if item.get("isActive") and item.get("assetGuid") and item["externalGuid"] not in used}
            if user_input is not None:
                device = self._devices.get(user_input["device_guid"])
                anchor = er.async_get(self.hass).async_get(user_input["source_entity"])
                if not device or not anchor or anchor.disabled or not anchor.device_id:
                    raise ValueError("device_mismatch")
                previous = next((item for item in saved.get("measurement_mappings", [])
                                 if item["device_guid"] == device["externalGuid"]), {})
                self._measurement_device = device
                self._measurement_anchor = anchor
                self._measurement_key = user_input.get("device_key") or previous.get("device_key", "")
                self._measurement_previous = previous
                self._measurement_readings = []
                self._attributes = await self._client.async_get_effective_attributes(device["assetGuid"])
                return await self.async_step_measurement_sources()
        except UteamupForbiddenError:
            errors["base"] = "telemetry_not_allowed"
        except UteamupError:
            errors["base"] = "cannot_connect"
        except (KeyError, TypeError, ValueError):
            errors["base"] = "device_mismatch"
        if not self._devices:
            errors.setdefault("base", "no_devices")
        return self.async_show_form(step_id="measurement_device", errors=errors, data_schema=vol.Schema({
            vol.Required("device_guid"): SelectSelector(SelectSelectorConfig(options=[
                {"value": guid, "label": item["name"]} for guid, item in self._devices.items()
            ], mode="dropdown", sort=True)),
            vol.Required("source_entity"): EntitySelector(EntitySelectorConfig(domain=["vacuum", "sensor", "binary_sensor"])),
            vol.Optional("device_key"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }))

    async def async_step_measurement_sources(self, user_input=None):
        choices = measurement_choices(self.hass, self._measurement_anchor.device_id)
        available = {entity.entity_id: entity for entity in choices}
        errors = {}
        if user_input is not None:
            selected = user_input.get("entities", [])
            if not selected or len(selected) > 64 or len(set(selected)) != len(selected):
                errors["base"] = "no_measurements"
            elif any(item not in available for item in selected):
                errors["base"] = "source_missing"
            else:
                # Keep every previously reviewed condition when the device is edited.
                previous = self._measurement_previous.get("readings", [])
                self._measurement_selected = [available[item].id for item in selected
                    for _ in range(max(1, sum(reading["source_registry_id"] == available[item].id
                                              for reading in previous)))]
                if len(self._measurement_selected) > 64:
                    errors["base"] = "no_measurements"
                else:
                    self._measurement_readings = []
                    return await self.async_step_measurement_meter()
        previous = {item["source_registry_id"] for item in self._measurement_previous.get("readings", [])}
        return self.async_show_form(step_id="measurement_sources", errors=errors, data_schema=vol.Schema({
            vol.Required("entities", default=[item.entity_id for item in choices if item.id in previous]): EntitySelector(
                EntitySelectorConfig(include_entities=list(available), multiple=True)),
        }), description_placeholders={"device": self._measurement_device["name"]})

    async def async_step_measurement_meter(self, user_input=None):
        registry = er.async_get(self.hass)
        entity = registry.async_get(self._measurement_selected[len(self._measurement_readings)])
        if entity is None or entity.disabled or entity.device_id != self._measurement_anchor.device_id:
            return self.async_abort(reason="measurement_source_changed")
        state = self.hass.states.get(entity.entity_id)
        states = measurement_states(self.hass, entity)
        unit = "0/1" if states else source_unit(entity, state)
        definitions = {item["attributeDefinitionGuid"]: item for group in self._attributes or []
                       for item in group.get("definitions", []) if compatible_meter(item, unit)}
        errors = {}
        if user_input is not None:
            try:
                definition = definitions[user_input["attribute_guid"]]
                reading = build_measurement_source(self.hass, entity, definition, user_input.get("true_state", ""))
                if any(item.attribute_guid == reading.attribute_guid or
                       (item.source_registry_id == reading.source_registry_id and item.true_state == reading.true_state)
                       for item in self._measurement_readings):
                    raise ValueError("attribute_mismatch")
                if user_input.get("add_condition"):
                    if not states or len(self._measurement_selected) >= 64:
                        raise ValueError("attribute_mismatch")
                    self._measurement_selected.insert(len(self._measurement_readings) + 1, entity.id)
                self._measurement_readings.append(reading)
                if len(self._measurement_readings) < len(self._measurement_selected):
                    return await self.async_step_measurement_meter()
                return await self.async_step_measurement_confirm()
            except (KeyError, ValueError):
                errors["base"] = "attribute_mismatch"
        if not definitions:
            errors.setdefault("base", "no_measurement_meters")
        completed = [item for item in self._measurement_readings if item.source_registry_id == entity.id]
        previous_readings = [item for item in self._measurement_previous.get("readings", [])
                             if item["source_registry_id"] == entity.id]
        previous = previous_readings[len(completed)] if len(completed) < len(previous_readings) else {}
        fields = {vol.Required("attribute_guid", description={"suggested_value": previous.get("attribute_guid")}):
                  SelectSelector(SelectSelectorConfig(options=[{"value": guid, "label": item["name"]}
                                                              for guid, item in definitions.items()], mode="dropdown"))}
        if states:
            fields[vol.Required("true_state", description={"suggested_value": previous.get("true_state", "on" if "on" in states else None)})] = (
                SelectSelector(SelectSelectorConfig(options=list(states), mode="dropdown")))
            if len(self._measurement_selected) < 64 and len(completed) + 1 < len(states):
                fields[vol.Optional("add_condition", default=False)] = BooleanSelector()
        return self.async_show_form(step_id="measurement_meter", errors=errors, data_schema=vol.Schema(fields),
                                    description_placeholders={"source": state.name if state else entity.entity_id,
                                                              "unit": unit or "—",
                                                              "position": str(len(self._measurement_readings) + 1),
                                                              "count": str(len(self._measurement_selected))})

    async def async_step_measurement_confirm(self, user_input=None):
        errors = {}
        device, anchor = self._measurement_device, self._measurement_anchor
        if user_input is not None:
            try:
                current = next(item for item in await self._client.async_get_iot_devices()
                               if item["externalGuid"] == device["externalGuid"] and item.get("isActive"))
                if current.get("assetGuid") != device["assetGuid"]:
                    raise ValueError("device_mismatch")
                mapping = MeasurementMapping(device["externalGuid"], device["assetGuid"], device["name"],
                                             anchor.device_id, anchor.id, anchor.unique_id, anchor.platform,
                                             tuple(self._measurement_readings), self._measurement_key)
                old = self.config_entry.options.get(CONF_TELEMETRY, {})
                saved = {**old, **self._environment, "measurement_mappings": [
                    item for item in old.get("measurement_mappings", []) if item["device_guid"] != mapping.device_guid
                ] + [asdict(mapping)]}
                TelemetrySettings.from_dict(saved, self.config_entry.data[CONF_TENANT_GUID])
                return self.async_create_entry(data={**self._options, CONF_ENABLE_TELEMETRY: True, CONF_TELEMETRY: saved})
            except UteamupForbiddenError:
                errors["base"] = "telemetry_not_allowed"
            except UteamupError:
                errors["base"] = "cannot_connect"
            except (ValueError, KeyError, TypeError, StopIteration):
                errors["base"] = "invalid_mapping"
        return self.async_show_form(step_id="measurement_confirm", errors=errors, data_schema=vol.Schema({}),
                                    description_placeholders={"device": device["name"], "asset": device["assetGuid"],
                                                              "source": anchor.entity_id,
                                                              "count": str(len(self._measurement_readings)),
                                                              "endpoint": self._environment["endpoint"]})
