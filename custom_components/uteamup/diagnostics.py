"""Diagnostics with every credential redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .const import CONF_API_SECRET, CONF_CLOUDHOOK_URL, CONF_WEBHOOK_ID, CONF_WEBHOOK_SECRET
from .coordinator import UteamupConfigEntry

TO_REDACT = {CONF_API_KEY, CONF_API_SECRET, CONF_WEBHOOK_SECRET, CONF_WEBHOOK_ID, CONF_CLOUDHOOK_URL}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: UteamupConfigEntry) -> dict[str, Any]:
    data = entry.runtime_data.coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "data": {**asdict(data), "denied": sorted(data.denied)},
    }
