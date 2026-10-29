"""Public diagnostics retain health information without identifying a company or its records."""

from __future__ import annotations

from copy import deepcopy
import json
from unittest.mock import MagicMock

import pytest

from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL

from custom_components.uteamup.const import (
    CONF_API_SECRET,
    CONF_ASSIGNEE_GUID,
    CONF_CLOUDHOOK_URL,
    CONF_DEVICE_KEY,
    CONF_ENABLE_TELEMETRY,
    CONF_ENABLE_WEBHOOKS,
    CONF_TELEMETRY,
    CONF_TENANT_GUID,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_SUBSCRIPTION_GUID,
)
from custom_components.uteamup.coordinator import UteamupData, UteamupRuntimeData
from custom_components.uteamup.diagnostics import async_get_config_entry_diagnostics

from .conftest import ANALYTICS, API_KEY, API_SECRET, BASE, STOCK, TENANT, WORK_ORDERS, mock_login, setup_entry


async def test_diagnostics_export_counts_without_account_or_record_details(hass, aioclient_mock, config_entry):
    work_order = {
        **WORK_ORDERS["items"][0],
        "description": "Private work order description",
        "locationName": "Private boiler room",
    }
    analytics = {
        **deepcopy(ANALYTICS),
        "stock": {"lowStockWarnings": [work_order], "categoryDistribution": [{"label": work_order["name"]}]},
        "customers": [{"name": "Private customer"}],
    }
    stock = {**STOCK, "futureDetails": [work_order]}
    iot = {"items": [{**work_order, "resolutionNotes": "Private incident notes"}], "totalItems": 2}
    options = {
        CONF_ENABLE_WEBHOOKS: False,
        CONF_ASSIGNEE_GUID: work_order["primaryAssigneeGuid"],
        CONF_SCAN_INTERVAL: 180,
    }
    entry_data = {
        **config_entry.data,
        CONF_WEBHOOK_ID: "private-webhook-id",
        CONF_WEBHOOK_SECRET: "private-webhook-secret",
        CONF_WEBHOOK_SUBSCRIPTION_GUID: "private-subscription-guid",
        CONF_CLOUDHOOK_URL: "https://hooks.example.test/private-webhook-url",
    }
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data=entry_data, options=options)
    mock_login(aioclient_mock)
    for path, body in (
        ("/api/analytics/summary", analytics),
        ("/api/stock/alerts/summary", stock),
        ("/api/iot/alerts", iot),
        ("/api/workorder", {"items": [work_order], "totalItems": 1}),
    ):
        aioclient_mock.get(f"{BASE}{path}", json=body)
    await setup_entry(hass, config_entry)
    original_data = deepcopy(config_entry.runtime_data.coordinator.data)

    diagnostic = await async_get_config_entry_diagnostics(hass, config_entry)

    serialized = json.dumps(diagnostic)
    for private in (
        API_KEY, API_SECRET, BASE, TENANT, *work_order.values(), "Private customer", "Private incident notes",
        entry_data[CONF_WEBHOOK_ID], entry_data[CONF_WEBHOOK_SECRET],
        entry_data[CONF_WEBHOOK_SUBSCRIPTION_GUID], entry_data[CONF_CLOUDHOOK_URL],
    ):
        assert private not in serialized
    assert diagnostic["entry"][CONF_API_KEY] == "**REDACTED**"
    assert diagnostic["entry"][CONF_API_SECRET] == "**REDACTED**"
    assert diagnostic["entry"][CONF_URL] == "**REDACTED**"
    assert diagnostic["entry"][CONF_TENANT_GUID] == "**REDACTED**"
    assert diagnostic["options"][CONF_ASSIGNEE_GUID] == "**REDACTED**"
    assert diagnostic["entry"][CONF_VERIFY_SSL] is True
    assert diagnostic["options"][CONF_SCAN_INTERVAL] == 180
    assert diagnostic["data"] == {
        "analytics": ANALYTICS,
        "stock_alerts": STOCK,
        "iot_alerts": {"totalItems": 2},
        "open_work_orders_count": 1,
        "denied": [],
    }
    assert diagnostic["telemetry"] is None
    assert config_entry.runtime_data.coordinator.data == original_data
    assert dict(config_entry.data) == entry_data
    assert dict(config_entry.options) == options


async def test_diagnostics_redact_nested_telemetry_identities_and_anonymize_sources(hass, config_entry):
    identity_fields = (
        "device_guid", "asset_guid", "name", "ha_device_id", "mac_address", "source_registry_id",
        "power_registry_id", "energy_registry_id", "power_attribute_guid", "energy_attribute_guid",
        "estimate_profile", "tracker_registry_id", "download_registry_id", "upload_registry_id",
        "clients_registry_id", "internet_registry_id", "router_registry_id", "download_attribute_guid",
        "upload_attribute_guid", "clients_attribute_guid", "connected_attribute_guid", "internet_attribute_guid",
        "router_attribute_guid",
        CONF_DEVICE_KEY,
    )
    mapping = {key: f"private-{key}" for key in identity_fields}
    mapping["power_basis"] = "estimated"
    options = {
        CONF_ENABLE_TELEMETRY: True,
        CONF_TELEMETRY: {
            "endpoint": "https://private-ingestion.example.test/events",
            CONF_TENANT_GUID: TENANT,
            "environment_guid": "private-environment-guid",
            "retry_nonce": "private-retry-nonce",
            "mappings": [mapping],
            "network_mappings": [{**deepcopy(mapping), "clients": [deepcopy(mapping)]}],
        },
    }
    health = {
        "sender": "retrying",
        "sources": {"private-device-guid-one": "available", "private-device-guid-two": "unavailable"},
        "queue": {
            "pending": 3, "quarantined": 1, "bytes": 2048, "blocked_devices": 1,
            "missed_samples": 2, "retrying_devices": 1,
        },
        "last_success": "2026-10-29T12:00:00+00:00",
        "future_payload": {"description": "private-telemetry-payload"},
    }
    original_health = deepcopy(health)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options=options)
    telemetry = MagicMock()
    telemetry.diagnostics.return_value = health
    config_entry.runtime_data = UteamupRuntimeData(MagicMock(), MagicMock(data=UteamupData()), telemetry)

    diagnostic = await async_get_config_entry_diagnostics(hass, config_entry)

    serialized = json.dumps(diagnostic)
    assert "private-" not in serialized
    assert TENANT not in serialized
    assert API_KEY not in serialized
    assert API_SECRET not in serialized
    for mappings_key in ("mappings", "network_mappings"):
        redacted = diagnostic["options"][CONF_TELEMETRY][mappings_key][0]
        assert all(redacted[key] == "**REDACTED**" for key in identity_fields)
        assert redacted["power_basis"] == "estimated"
    assert diagnostic["options"][CONF_ENABLE_TELEMETRY] is True
    assert diagnostic["telemetry"] == {
        "sender": "retrying",
        "sources": ["available", "unavailable"],
        "queue": health["queue"],
        "last_success": health["last_success"],
    }
    assert dict(config_entry.options) == options
    assert health == original_health


@pytest.mark.parametrize("work_orders, expected_count", [(None, None), ([], 0)])
async def test_diagnostics_distinguish_unavailable_sections_from_empty_results(
    hass, config_entry, work_orders, expected_count
):
    data = UteamupData(open_work_orders=work_orders, denied={"stock_alerts", "analytics"})
    config_entry.runtime_data = UteamupRuntimeData(MagicMock(), MagicMock(data=data))

    diagnostic = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostic["data"] == {
        "analytics": None,
        "stock_alerts": None,
        "iot_alerts": None,
        "open_work_orders_count": expected_count,
        "denied": ["analytics", "stock_alerts"],
    }


@pytest.mark.parametrize("invalid_count", ["private-count", {"name": "private-nested-count"}, [TENANT], True, -1, 1.5])
async def test_diagnostics_never_export_records_or_text_through_count_fields(hass, config_entry, invalid_count):
    data = UteamupData(
        analytics={"workorders": {"open": invalid_count, "inProgress": 3, "onHold": 0, "overdue": 2}},
        stock_alerts={**STOCK, "totalActive": invalid_count},
        iot_alerts={"totalItems": invalid_count},
        open_work_orders=[],
    )
    original = deepcopy(data)
    config_entry.runtime_data = UteamupRuntimeData(MagicMock(), MagicMock(data=data))

    diagnostic = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostic["data"]["analytics"]["workorders"]["open"] is None
    assert diagnostic["data"]["analytics"]["workorders"]["inProgress"] == 3
    assert diagnostic["data"]["analytics"]["workorders"]["onHold"] == 0
    assert diagnostic["data"]["stock_alerts"]["totalActive"] is None
    assert diagnostic["data"]["iot_alerts"]["totalItems"] is None
    assert "private-" not in json.dumps(diagnostic)
    assert TENANT not in json.dumps(diagnostic)
    assert data == original
