"""Source truth, immutable replay, degraded HA setup and independent delivery health."""

from dataclasses import asdict, replace
from datetime import timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.uteamup.const import CONF_ENABLE_TELEMETRY, CONF_ENABLE_WEBHOOKS, CONF_TELEMETRY
from custom_components.uteamup.coordinator import UteamupData, UteamupRuntimeData
from custom_components.uteamup.diagnostics import async_get_config_entry_diagnostics
from custom_components.uteamup.telemetry import SourceUnavailable, UteamupTelemetry, sample_envelope, sample_network_envelope
from custom_components.uteamup.telemetry_config import (
    TelemetrySettings, build_mapping, build_network_mapping, environment_settings,
)
from custom_components.uteamup.telemetry_transport import DeliveryResult

from .conftest import API_KEY, API_SECRET, TENANT, mock_login, setup_entry
from .test_init import _state
from .test_telemetry_config import (
    ATTRIBUTES, DEVICE, DEVICE_GUID, DEVICE_KEY, NETWORK_ATTRIBUTES, NETWORK_DEVICE, NETWORK_DEVICE_GUID,
    NETWORK_METRICS, environment_response, register_deco_sources, register_socket_sources, register_sources,
)


def configured(hass, config_entry):
    light, power, energy, inputs = register_sources(hass, config_entry)
    mapping = build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    saved = {**environment_settings(environment_response(), TENANT), "mappings": [asdict(mapping)]}
    settings = TelemetrySettings.from_dict(saved, TENANT)
    return settings, saved, light, power, energy


async def test_current_observation_exports_estimate_metadata_and_correct_meter_units(hass, config_entry):
    settings, _, _, _, _ = configured(hass, config_entry)
    event = sample_envelope(hass, settings, settings.mappings[0], dt_util.utcnow())
    body = json.loads(event.payload)
    assert body["eventId"] == event.event_id
    assert body["metadata"]["powerBasis"] == "estimated"
    assert body["metadata"]["estimateProfile"] == "demo-linear-v1"
    assert [(item["unit"], item["value"], item["quality"]) for item in body["points"]] == [
        ("W", 0.3, "estimated"), ("kWh", 1.2, "estimated"),
    ]
    assert len({item["pointId"] for item in body["points"]}) == 2


async def test_socket_exports_measured_values_without_estimation_metadata(hass, config_entry):
    *_, inputs = register_socket_sources(hass, config_entry)
    mapping = build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    settings = TelemetrySettings.from_dict({
        **environment_settings(environment_response(), TENANT), "mappings": [asdict(mapping)],
    }, TENANT)
    body = json.loads(sample_envelope(hass, settings, mapping, dt_util.utcnow()).payload)
    assert body["metadata"]["powerBasis"] == "measured"
    assert "estimateProfile" not in body["metadata"]
    assert "estimateParameters" not in body["metadata"]
    assert [(point["unit"], point["value"], point["quality"]) for point in body["points"]] == [
        ("W", 88, "measured"), ("kWh", 11.937, "measured"),
    ]


@pytest.mark.parametrize("reason", ["unavailable", "measured_source_required"])
async def test_socket_stops_capture_when_sources_disappear_or_become_estimated(hass, config_entry, reason):
    socket, power, _, inputs = register_socket_sources(hass, config_entry)
    mapping = build_mapping(hass, DEVICE, ATTRIBUTES, inputs)
    settings = TelemetrySettings.from_dict({
        **environment_settings(environment_response(), TENANT), "mappings": [asdict(mapping)],
    }, TENANT)
    if reason == "unavailable":
        hass.states.async_set(socket.entity_id, STATE_UNAVAILABLE)
    else:
        hass.states.async_set(power.entity_id, "88", {"unit_of_measurement": "W", "power_basis": "estimated"})
    telemetry = UteamupTelemetry(hass, config_entry, settings)
    telemetry.transport.async_send = AsyncMock()
    await telemetry.async_sample_once()
    assert telemetry.health.sources == {DEVICE_GUID: reason}
    assert (await telemetry.queue.async_stats()).pending == 0
    assert not await telemetry.async_send_once()
    telemetry.transport.async_send.assert_not_awaited()


async def test_serialized_parameters_are_bounded_and_never_copy_unrelated_attributes(hass, config_entry):
    settings, _, _, power, _ = configured(hass, config_entry)
    attributes = dict(hass.states.get(power.entity_id).attributes)
    hass.states.async_set(power.entity_id, "0.5", {
        **attributes, "assumed_full_power_w": 8.0, "assumed_standby_power_w": 0.5,
        "device_key": DEVICE_KEY, "unrelated_secret": "do-not-export",
    })
    event = sample_envelope(hass, settings, settings.mappings[0], dt_util.utcnow())
    assert json.loads(event.payload)["metadata"]["estimateParameters"] == {
        "assumedFullPowerW": 8.0, "assumedStandbyPowerW": 0.5,
    }
    assert DEVICE_KEY.encode() not in event.payload
    assert b"do-not-export" not in event.payload
    hass.states.async_set(power.entity_id, "0.5", {
        **attributes, "assumed_full_power_w": float("inf"), "assumed_standby_power_w": -1,
    })
    event = sample_envelope(hass, settings, settings.mappings[0], dt_util.utcnow())
    assert json.loads(event.payload)["metadata"]["estimateParameters"] == {}


@pytest.mark.parametrize("unavailable_source", ["light", "power", "energy"])
async def test_unavailable_source_emits_neither_zero_nor_liveness(hass, config_entry, unavailable_source):
    settings, _, light, power, energy = configured(hass, config_entry)
    entity = {"light": light, "power": power, "energy": energy}[unavailable_source]
    hass.states.async_set(entity.entity_id, STATE_UNAVAILABLE)
    telemetry = UteamupTelemetry(hass, config_entry, settings)
    telemetry.transport.async_send = AsyncMock()
    await telemetry.async_sample_once()
    assert not await telemetry.async_send_once()
    assert telemetry.health.sources == {DEVICE_GUID: "unavailable"}
    assert telemetry.health.sender == "running"
    assert (await telemetry.queue.async_stats()).pending == 0
    telemetry.transport.async_send.assert_not_awaited()


async def test_registry_rename_keeps_the_same_physical_mapping(hass, config_entry):
    settings, _, light, _, _ = configured(hass, config_entry)
    renamed = er.async_get(hass).async_update_entity(light.entity_id, new_entity_id="light.renamed_entrance")
    hass.states.async_remove(light.entity_id)
    hass.states.async_set(renamed.entity_id, "off")
    assert json.loads(sample_envelope(hass, settings, settings.mappings[0], dt_util.utcnow()).payload)["deviceGuid"] == DEVICE_GUID


@pytest.mark.parametrize("value,attributes,reason", [
    ("nan", {"unit_of_measurement": "W"}, "invalid_value"),
    ("-1", {"unit_of_measurement": "W"}, "invalid_value"),
    ("1", {"unit_of_measurement": "kW"}, "unit_mismatch"),
    ("1", {"unit_of_measurement": "W", "source_device_id": "different"}, "device_mismatch"),
    ("1", {"unit_of_measurement": "W", "estimate_profile": "different"}, "profile_mismatch"),
])
async def test_changed_or_invalid_helper_is_not_silently_exported(hass, config_entry, value, attributes, reason):
    settings, _, _, power, _ = configured(hass, config_entry)
    original = dict(hass.states.get(power.entity_id).attributes)
    hass.states.async_set(power.entity_id, value, {**original, **attributes})
    telemetry = UteamupTelemetry(hass, config_entry, settings)
    await telemetry.async_sample_once()
    assert telemetry.health.sources[DEVICE_GUID] == reason
    assert (await telemetry.queue.async_stats()).pending == 0


async def test_restart_replays_old_data_while_bulb_is_currently_unavailable(hass, config_entry):
    settings, _, light, _, _ = configured(hass, config_entry)
    first = UteamupTelemetry(hass, config_entry, settings)
    await first.async_sample_once()
    queued = await first.queue.async_next(dt_util.utcnow().timestamp())
    hass.states.async_set(light.entity_id, STATE_UNAVAILABLE)
    restarted = UteamupTelemetry(hass, config_entry, settings)
    restarted.transport.async_send = AsyncMock(return_value=DeliveryResult("accepted"))
    await restarted.async_sample_once()
    assert (await restarted.queue.async_stats()).pending == 1
    assert await restarted.async_send_once()
    sent = restarted.transport.async_send.call_args.args[0]
    assert sent.payload == queued.payload
    assert sent.event_id == queued.event_id
    assert (await restarted.queue.async_stats()).pending == 0
    assert restarted.health.sources[DEVICE_GUID] == "unavailable"


async def test_retry_after_is_durable_and_exact_payload_is_reused(hass, config_entry):
    settings, *_ = configured(hass, config_entry)
    telemetry = UteamupTelemetry(hass, config_entry, settings)
    await telemetry.async_sample_once()
    now = dt_util.utcnow()
    original = await telemetry.queue.async_next(now.timestamp())
    telemetry.transport.async_send = AsyncMock(return_value=DeliveryResult("retrying", 7200))
    with patch("custom_components.uteamup.telemetry.dt_util.utcnow", return_value=now):
        await telemetry.async_send_once()
    assert telemetry.health.sender == "retrying"
    assert await telemetry.queue.async_next((now + timedelta(seconds=7199)).timestamp()) is None
    later = await telemetry.queue.async_next((now + timedelta(seconds=7200)).timestamp())
    assert later.payload == original.payload
    assert later.event_id == original.event_id


async def test_auth_block_stops_new_capture_until_key_rotation(hass, config_entry):
    settings, *_ = configured(hass, config_entry)
    telemetry = UteamupTelemetry(hass, config_entry, settings)
    await telemetry.queue.async_configure({DEVICE_GUID: settings.mappings[0].credential_hash}, "")
    await telemetry.async_sample_once()
    telemetry.transport.async_send = AsyncMock(return_value=DeliveryResult("authentication"))
    await telemetry.async_send_once()
    await telemetry.async_sample_once()
    assert telemetry.health.sender == "blocked"
    assert telemetry.health.sources[DEVICE_GUID] == "available"
    assert (await telemetry.queue.async_stats()).quarantined == 1
    assert (await telemetry.queue.async_stats()).pending == 0
    rotated = replace(settings.mappings[0], device_key="rotated-device-key")
    await telemetry.queue.async_configure({DEVICE_GUID: rotated.credential_hash}, "")
    assert await telemetry.queue.async_next(dt_util.utcnow().timestamp()) is not None


async def test_unload_stops_tasks_and_retains_queue(hass, config_entry):
    settings, *_ = configured(hass, config_entry)
    telemetry = UteamupTelemetry(hass, config_entry, settings)
    telemetry.transport.async_send = AsyncMock(return_value=DeliveryResult("retrying", 7200))
    await telemetry.async_start()
    await telemetry.async_stop()
    assert telemetry.health.sender == "disabled"
    assert not telemetry._tasks
    stats = await telemetry.queue.async_stats()
    # Whether the immediate capture ran before cancellation, retained data is never deleted by stop.
    assert stats.pending in {0, 1}


async def test_cmms_start_outage_keeps_cached_sender_and_health_available(hass, config_entry, aioclient_mock):
    settings, saved, light, *_ = configured(hass, config_entry)
    hass.config_entries.async_update_entry(config_entry, options={
        CONF_ENABLE_WEBHOOKS: False, CONF_ENABLE_TELEMETRY: True, CONF_TELEMETRY: saved,
    })
    seed = UteamupTelemetry(hass, config_entry, settings)
    await seed.async_sample_once()
    hass.states.async_set(light.entity_id, STATE_UNAVAILABLE)
    mock_login(aioclient_mock, status=503, body={})
    with patch.object(UteamupTelemetry, "async_start", new=AsyncMock()) as start:
        await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    start.assert_awaited_once()
    assert _state(hass, "sensor", "open_work_orders").state == STATE_UNAVAILABLE
    assert _state(hass, "sensor", "telemetry_sender").state == "starting"
    sender = config_entry.runtime_data.telemetry
    sender.transport.async_send = AsyncMock(return_value=DeliveryResult("accepted"))
    await sender.async_sample_once()
    assert await sender.async_send_once()
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "telemetry_sender").state == "running"
    assert _state(hass, "sensor", "telemetry_sources_available").state == "0"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_diagnostics_recursively_redact_device_keys_and_do_not_export_payloads(hass, config_entry):
    settings, saved, *_ = configured(hass, config_entry)
    hass.config_entries.async_update_entry(config_entry, options={CONF_TELEMETRY: saved})
    telemetry = UteamupTelemetry(hass, config_entry, settings)
    config_entry.runtime_data = UteamupRuntimeData(MagicMock(), MagicMock(data=UteamupData()), telemetry)
    diagnostic = await async_get_config_entry_diagnostics(hass, config_entry)
    text = json.dumps(diagnostic, default=str)
    assert all(secret not in text for secret in (DEVICE_KEY, API_KEY, API_SECRET))
    assert diagnostic["options"][CONF_TELEMETRY]["mappings"][0]["device_key"] == "**REDACTED**"
    assert "payload" not in diagnostic["telemetry"]


def network_configured(hass, config_entry):
    tracker, entities, inputs = register_deco_sources(hass, config_entry)
    mapping = build_network_mapping(hass, NETWORK_DEVICE, NETWORK_ATTRIBUTES, inputs)
    settings = TelemetrySettings.from_dict({
        **environment_settings(environment_response(), TENANT),
        "mappings": [],
        "network_mappings": [asdict(mapping)],
    }, TENANT)
    return settings, mapping, tracker, entities


async def test_deco_envelope_exports_all_five_metrics_and_binary_states(hass, config_entry):
    settings, mapping, _, _ = network_configured(hass, config_entry)
    event = sample_network_envelope(hass, settings, mapping, dt_util.utcnow())
    body = json.loads(event.payload)
    assert body["deviceGuid"] == NETWORK_DEVICE_GUID
    assert body["metadata"]["telemetryKind"] == "network"
    assert [point["value"] for point in body["points"]] == [32658.875, 740.875, 19, 1, 0]
    assert [point["unit"] for point in body["points"]] == ["kB/s", "kB/s", "clients", "0/1", "0/1"]
    assert all(point["quality"] == "measured" for point in body["points"])
    assert len({point["pointId"] for point in body["points"]}) == 5
    assert DEVICE_KEY.encode() not in event.payload


@pytest.mark.parametrize("name", [name for name, _, _ in NETWORK_METRICS])
async def test_deco_unavailable_source_emits_no_reading(hass, config_entry, name):
    settings, mapping, _, entities = network_configured(hass, config_entry)
    hass.states.async_set(entities[name].entity_id, STATE_UNAVAILABLE)
    with pytest.raises(SourceUnavailable, match="unavailable"):
        sample_network_envelope(hass, settings, mapping, dt_util.utcnow())


async def test_deco_capture_rechecks_tracker_mac(hass, config_entry):
    settings, mapping, tracker, _ = network_configured(hass, config_entry)
    hass.states.async_set(tracker.entity_id, "not_home", {
        "device_type": "deco",
        "mac": "00:11:22:33:44:55",
    })
    with pytest.raises(SourceUnavailable, match="device_mismatch"):
        sample_network_envelope(hass, settings, mapping, dt_util.utcnow())


@pytest.mark.parametrize("value,attributes,reason", [
    ("nan", {"unit_of_measurement": "kB/s"}, "invalid_value"),
    ("-1", {"unit_of_measurement": "kB/s"}, "invalid_value"),
    ("1", {"unit_of_measurement": "MB/s"}, "unit_mismatch"),
])
async def test_deco_invalid_numeric_source_is_not_exported(hass, config_entry, value, attributes, reason):
    settings, mapping, _, entities = network_configured(hass, config_entry)
    hass.states.async_set(entities["download"].entity_id, value, attributes)
    with pytest.raises(SourceUnavailable, match=reason):
        sample_network_envelope(hass, settings, mapping, dt_util.utcnow())
