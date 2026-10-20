"""Constants for the UteamUP integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "uteamup"

DEFAULT_BASE_URL: Final = "https://api.uteamup.com"

CONF_API_SECRET: Final = "api_secret"
CONF_TENANT_GUID: Final = "tenant_guid"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_WEBHOOK_SECRET: Final = "webhook_secret"
CONF_WEBHOOK_SUBSCRIPTION_GUID: Final = "webhook_subscription_guid"
CONF_CLOUDHOOK_URL: Final = "cloudhook_url"

CONF_ENABLE_WEBHOOKS: Final = "enable_webhooks"
CONF_ASSIGNEE_GUID: Final = "assignee_guid"
CONF_ENABLE_TELEMETRY: Final = "enable_telemetry"
CONF_TELEMETRY: Final = "telemetry"
CONF_DEVICE_KEY: Final = "device_key"

TELEMETRY_SAMPLE_SECONDS: Final = 60
TELEMETRY_QUEUE_MAX_COUNT: Final = 50_000
TELEMETRY_QUEUE_MAX_BYTES: Final = 64 * 1024 * 1024

DEFAULT_SCAN_INTERVAL: Final = 120
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 3600

EVENT_UTEAMUP: Final = "uteamup_event"

# UteamUP work order status ids (Models/WorkorderStatus.cs).
WORK_ORDER_STATUSES: Final = {
    "not_started": 0,
    "pending": 1,
    "in_progress": 2,
    "on_hold": 3,
    "completed": 4,
    "cancelled": 5,
}

# Asset operational states accepted as a target (Enums/AssetStateEnums.cs, camelCase on the wire).
ASSET_STATES: Final = {
    "online": "online",
    "offline": "offline",
    "standby": "standby",
    "under_maintenance": "underMaintenance",
    "degraded": "degraded",
    "lost": "lost",
    "stolen": "stolen",
    "in_transit": "inTransit",
    "in_storage": "inStorage",
}

QUICK_REPORT_SEVERITIES: Final = ("low", "medium", "high", "critical")

# Events UteamUP delivers that Home Assistant users are likely to automate on.
WEBHOOK_EVENTS: Final = (
    "workorder.created",
    "workorder.updated",
    "workorder.assigned",
    "workorder.completed",
    "asset.created",
    "asset.updated",
    "iot.alert.created",
    "iot.alert.resolved",
    "iot.device.online",
    "iot.device.offline",
    "stock.alert.created",
    "part.stock_low",
)
