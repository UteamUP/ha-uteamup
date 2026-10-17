"""Receives signed UteamUP webhook deliveries and fires them as Home Assistant events."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections import deque

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.network import NoURLAvailableError

from .api import UteamupClient, UteamupError, UteamupForbiddenError, UteamupNotFoundError
from .const import (
    CONF_CLOUDHOOK_URL,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_SUBSCRIPTION_GUID,
    DOMAIN,
    EVENT_UTEAMUP,
    WEBHOOK_EVENTS,
)
from .coordinator import UteamupConfigEntry

_LOGGER = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-UteamUP-Signature"
EVENT_HEADER = "X-UteamUP-Event"
DELIVERY_HEADER = "X-UteamUP-Delivery"
SEEN_DELIVERIES = 256


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
    """UteamUP signs the raw body with HMAC-SHA256 and sends `sha256=<lowercase hex>`."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _issue(hass: HomeAssistant, entry: UteamupConfigEntry, key: str | None) -> None:
    for candidate in ("webhook_url_unavailable", "webhooks_not_allowed"):
        issue_id = f"{candidate}_{entry.entry_id}"
        if candidate == key:
            ir.async_create_issue(
                hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=candidate, translation_placeholders={"title": entry.title},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def _cloudhook_url(hass: HomeAssistant, entry: UteamupConfigEntry, webhook_id: str) -> str | None:
    if "cloud" not in hass.config.components:
        return None
    from homeassistant.components import cloud  # noqa: PLC0415 - only when Home Assistant Cloud is loaded

    if not cloud.async_active_subscription(hass):
        return None
    # The cloudhook URL is stable, so it stays valid while Cloud reconnects after a restart.
    if url := entry.data.get(CONF_CLOUDHOOK_URL):
        return url
    if not cloud.async_is_connected(hass):
        return None
    url = await cloud.async_create_cloudhook(hass, webhook_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CLOUDHOOK_URL: url})
    return url


async def _public_url(hass: HomeAssistant, entry: UteamupConfigEntry, webhook_id: str) -> str | None:
    if url := await _cloudhook_url(hass, entry, webhook_id):
        return url
    try:
        # UteamUP refuses private and loopback addresses, so only an external URL can work.
        return webhook.async_generate_url(hass, webhook_id, allow_internal=False, allow_ip=False)
    except NoURLAvailableError:
        return None


def _cloud_connecting(hass: HomeAssistant) -> bool:
    if "cloud" not in hass.config.components:
        return False
    from homeassistant.components import cloud  # noqa: PLC0415 - only when Home Assistant Cloud is loaded

    return cloud.async_active_subscription(hass) and not cloud.async_is_connected(hass)


async def async_setup_webhook(hass: HomeAssistant, entry: UteamupConfigEntry) -> None:
    """Register the receiver and make sure UteamUP has a matching subscription."""
    data = dict(entry.data)
    data.setdefault(CONF_WEBHOOK_ID, webhook.async_generate_id())
    data.setdefault(CONF_WEBHOOK_SECRET, secrets.token_hex(32))
    if data != entry.data:
        hass.config_entries.async_update_entry(entry, data=data)

    webhook.async_register(
        hass, DOMAIN, f"UteamUP {entry.title}", data[CONF_WEBHOOK_ID], _handler(entry), allowed_methods=["POST"]
    )

    if "cloud" in hass.config.components:
        from homeassistant.components import cloud  # noqa: PLC0415 - only when Home Assistant Cloud is loaded

        # Cloud connects after startup; sync again once it does instead of reporting no URL.
        async def _cloud_state_changed(state: cloud.CloudConnectionState) -> None:
            if state is cloud.CloudConnectionState.CLOUD_CONNECTED:
                await _async_sync_subscription(hass, entry)

        entry.async_on_unload(cloud.async_listen_connection_change(hass, _cloud_state_changed))

    await _async_sync_subscription(hass, entry)


async def _async_sync_subscription(hass: HomeAssistant, entry: UteamupConfigEntry) -> None:
    url = await _public_url(hass, entry, entry.data[CONF_WEBHOOK_ID])
    if url is None:
        if not _cloud_connecting(hass):
            _issue(hass, entry, "webhook_url_unavailable")
        return

    client = entry.runtime_data.client
    try:
        existing = await client.async_list_webhook_subscriptions()
        stored = entry.data.get(CONF_WEBHOOK_SUBSCRIPTION_GUID)
        if any(str(item.get("guid")) == stored and item.get("url") == url for item in existing):
            _issue(hass, entry, None)
            return
        if stored:
            await _delete_subscription(client, stored)
        created = await client.async_create_webhook_subscription(
            f"Home Assistant ({hass.config.location_name})", url, entry.data[CONF_WEBHOOK_SECRET], list(WEBHOOK_EVENTS)
        )
    except UteamupForbiddenError:
        _issue(hass, entry, "webhooks_not_allowed")
        return
    except UteamupError as err:
        _LOGGER.warning("Could not register the UteamUP webhook; polling continues: %s", err)
        return
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_WEBHOOK_SUBSCRIPTION_GUID: str(created.get("guid") or "")}
    )
    _issue(hass, entry, None)


def async_unload_webhook(hass: HomeAssistant, entry: UteamupConfigEntry) -> None:
    if webhook_id := entry.data.get(CONF_WEBHOOK_ID):
        webhook.async_unregister(hass, webhook_id)


async def _delete_subscription(client: UteamupClient, guid: str) -> None:
    try:
        await client.async_delete_webhook_subscription(guid)
    except UteamupNotFoundError:
        pass


async def async_remove_webhook(hass: HomeAssistant, entry: UteamupConfigEntry, client: UteamupClient) -> None:
    """Remove the UteamUP subscription and the cloudhook when the entry is deleted."""
    try:
        if guid := entry.data.get(CONF_WEBHOOK_SUBSCRIPTION_GUID):
            await _delete_subscription(client, guid)
    except UteamupError as err:
        _LOGGER.warning("Could not remove the UteamUP webhook subscription; delete it in UteamUP: %s", err)
    if entry.data.get(CONF_CLOUDHOOK_URL) and (webhook_id := entry.data.get(CONF_WEBHOOK_ID)):
        from homeassistant.components import cloud  # noqa: PLC0415 - a cloudhook implies Cloud is installed

        await cloud.async_delete_cloudhook(hass, webhook_id)


def _handler(entry: UteamupConfigEntry):
    seen: deque[str] = deque(maxlen=SEEN_DELIVERIES)

    async def handle(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        body = await request.read()
        if not signature_valid(entry.data[CONF_WEBHOOK_SECRET], body, request.headers.get(SIGNATURE_HEADER)):
            return web.Response(status=401)
        delivery = request.headers.get(DELIVERY_HEADER)
        if delivery and delivery in seen:
            return web.Response(status=200)
        try:
            payload = json.loads(body)
        except ValueError:
            return web.Response(status=400)
        if delivery:
            seen.append(delivery)
        event = payload.get("event") or request.headers.get(EVENT_HEADER)
        hass.bus.async_fire(
            EVENT_UTEAMUP,
            {"config_entry_id": entry.entry_id, "event": event, "delivery_id": delivery, "data": payload.get("data")},
        )
        if event != "test.ping":
            await entry.runtime_data.coordinator.async_request_refresh()
        return web.Response(status=200)

    return handle
