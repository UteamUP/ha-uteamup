"""Public diagnostics contain counts and health information, without record details or identities."""

from __future__ import annotations

from typing import Any
from collections.abc import Mapping

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant

from .const import (
    CONF_API_SECRET,
    CONF_ASSIGNEE_GUID,
    CONF_CLOUDHOOK_URL,
    CONF_DEVICE_KEY,
    CONF_TENANT_GUID,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_SUBSCRIPTION_GUID,
)
from .coordinator import UteamupConfigEntry

TO_REDACT = {
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_ASSIGNEE_GUID,
    CONF_CLOUDHOOK_URL,
    CONF_DEVICE_KEY,
    CONF_TENANT_GUID,
    CONF_URL,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_SUBSCRIPTION_GUID,
    "endpoint",
    "environment_guid",
    "device_guid",
    "asset_guid",
    "name",
    "ha_device_id",
    "mac_address",
    "source_registry_id",
    "power_registry_id",
    "energy_registry_id",
    "power_attribute_guid",
    "energy_attribute_guid",
    "estimate_profile",
    "retry_nonce",
    "tracker_registry_id",
    "download_registry_id",
    "upload_registry_id",
    "clients_registry_id",
    "internet_registry_id",
    "router_registry_id",
    "download_attribute_guid",
    "upload_attribute_guid",
    "clients_attribute_guid",
    "connected_attribute_guid",
    "internet_attribute_guid",
    "router_attribute_guid",
    "anchor_registry_id",
    "anchor_unique_id",
    "attribute_guid",
    "true_state",
    "known_states",
}


def _counts(section: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, int | None] | None:
    """Only nonnegative integer counts may leave the diagnostic boundary."""
    if not isinstance(section, dict):
        return None
    counts = {}
    for key in keys:
        value = section.get(key)
        counts[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    return counts


def _json_containers(value: Any) -> Any:
    """HA redaction traverses dictionaries/lists; freshly saved dataclasses contain tuples."""
    if isinstance(value, Mapping):
        return {key: _json_containers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_containers(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: UteamupConfigEntry) -> dict[str, Any]:
    data = entry.runtime_data.coordinator.data
    telemetry = entry.runtime_data.telemetry.diagnostics() if entry.runtime_data.telemetry else None
    return {
        "entry": async_redact_data(_json_containers(entry.data), TO_REDACT),
        "options": async_redact_data(_json_containers(entry.options), TO_REDACT),
        "data": {
            "analytics": {
                "workorders": _counts(
                    data.analytics.get("workorders"), ("open", "inProgress", "onHold", "overdue")
                ),
            } if data.analytics is not None else None,
            "stock_alerts": _counts(
                data.stock_alerts,
                ("totalActive", "criticalCount", "lowStockCount", "stockoutCount", "actionRequiredCount"),
            ),
            "iot_alerts": _counts(data.iot_alerts, ("totalItems",)),
            "open_work_orders_count": len(data.open_work_orders) if data.open_work_orders is not None else None,
            "denied": sorted(data.denied),
        },
        "telemetry": {
            "sender": telemetry["sender"],
            "sources": list(telemetry["sources"].values()),
            "queue": _counts(
                telemetry["queue"],
                ("pending", "quarantined", "bytes", "blocked_devices", "missed_samples", "retrying_devices"),
            ),
            "last_success": telemetry["last_success"],
        } if telemetry is not None else None,
    }
