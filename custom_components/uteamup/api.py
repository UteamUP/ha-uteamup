"""Async client for the UteamUP REST API, authenticated with a tenant API key."""

from __future__ import annotations

import base64
import html
import json
import time
from datetime import UTC, datetime
from typing import Any

import aiohttp

TOKEN_REFRESH_MARGIN = 300
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class UteamupError(Exception):
    """Base error for the UteamUP client."""


class UteamupConnectionError(UteamupError):
    """The API could not be reached or answered with a server error."""


class UteamupAuthError(UteamupError):
    """The API key or secret was rejected."""


class UteamupMcpKeyError(UteamupAuthError):
    """The key is MCP-enabled, and MCP keys cannot sign in to the REST API."""


class UteamupPlanError(UteamupError):
    """The tenant's paid plan is not active yet."""


class UteamupForbiddenError(UteamupError):
    """The key's role, its audit actor or the tenant's plan does not allow the call."""


class UteamupNotFoundError(UteamupError):
    """The record does not exist in this tenant."""


class UteamupRateLimitError(UteamupError):
    """UteamUP asked the client to slow down."""

    def __init__(self, retry_after: int | None) -> None:
        super().__init__(f"Rate limited; retry after {retry_after} s")
        self.retry_after = retry_after


class UteamupApiError(UteamupError):
    """The API rejected the request (validation or conflict)."""


def _jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _retry_after(response: aiohttp.ClientResponse) -> int | None:
    value = response.headers.get("Retry-After")
    return int(value) if value and value.isdigit() else None


async def _error_message(response: aiohttp.ClientResponse) -> str:
    text = await response.text()
    try:
        body = json.loads(text)
    except ValueError:
        return text or f"HTTP {response.status}"
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body.get("title") or body)
    return str(body)


class UteamupClient:
    """Talks to one UteamUP tenant with a non-MCP tenant API key."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
        api_secret: str,
        tenant_guid: str | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._tenant_guid = tenant_guid
        self._token: str | None = None
        self._expires_at = 0.0
        self._telemetry_attribute_types: dict[str, dict[str, Any]] = {}

    @property
    def tenant_guid(self) -> str | None:
        """The tenant the key belongs to, known after the first login."""
        return self._tenant_guid

    async def async_login(self) -> str:
        """Exchange the key and secret for a JWT and return the key's tenant GUID."""
        body: dict[str, Any] = {"apiKey": self._api_key, "secret": self._api_secret}
        if self._tenant_guid:
            body["tenantGuid"] = self._tenant_guid
        try:
            async with self._session.post(
                f"{self._base_url}/api/auth/apikey-login", json=body, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status == 429:
                    raise UteamupRateLimitError(_retry_after(response))
                if response.status == 400:
                    message = await _error_message(response)
                    if "MCP" in message:
                        raise UteamupMcpKeyError(message)
                    raise UteamupAuthError(message)
                if response.status == 401:
                    raise UteamupAuthError(await _error_message(response))
                if response.status >= 400:
                    raise UteamupConnectionError(f"Login failed with HTTP {response.status}")
                token = (await response.json())["accessToken"]
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UteamupConnectionError(str(err)) from err

        claims = _jwt_claims(token)
        self._token = token
        self._expires_at = float(claims.get("exp", time.time() + 3600))
        self._tenant_guid = str(claims["TenantGuid"])
        return self._tenant_guid

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        form: aiohttp.FormData | None = None,
        retry_auth: bool = True,
    ) -> Any:
        if self._token is None or time.time() > self._expires_at - TOKEN_REFRESH_MARGIN:
            await self.async_login()
        headers = {"Authorization": f"Bearer {self._token}", "X-Tenant-Guid": str(self._tenant_guid)}
        if method != "GET":
            # The API's CSRF guard requires this header on every state-changing request.
            headers["X-Requested-With"] = "XMLHttpRequest"
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=json_body,
                data=form,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 401 and retry_auth:
                    self._token = None
                    return await self._request(
                        method, path, params=params, json_body=json_body, form=form, retry_auth=False
                    )
                if response.status == 401:
                    raise UteamupAuthError(await _error_message(response))
                if response.status == 402:
                    raise UteamupPlanError(await _error_message(response))
                if response.status == 403:
                    raise UteamupForbiddenError(f"{method} {path} is not allowed for this API key")
                if response.status == 404:
                    raise UteamupNotFoundError(await _error_message(response))
                if response.status == 429:
                    raise UteamupRateLimitError(_retry_after(response))
                if response.status >= 500:
                    raise UteamupConnectionError(f"{method} {path} failed with HTTP {response.status}")
                if response.status >= 400:
                    raise UteamupApiError(await _error_message(response))
                text = await response.text()
                return json.loads(text) if text else None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UteamupConnectionError(str(err)) from err

    async def async_get_analytics_summary(self, timeframe: str = "30d") -> dict[str, Any]:
        return await self._request("GET", "/api/analytics/summary", params={"timeframe": timeframe})

    async def async_get_stock_alert_summary(self) -> dict[str, Any]:
        return await self._request("GET", "/api/stock/alerts/summary")

    async def async_get_iot_alerts(self) -> dict[str, Any]:
        return await self._request(
            "GET", "/api/iot/alerts", params={"unacknowledgedOnly": "true", "page": 1, "pageSize": 100}
        )

    async def async_get_iot_environment(self) -> dict[str, Any]:
        """Read the tenant's dedicated ingestion endpoint, without provisioning it."""
        return await self._request("GET", "/api/iot/environment")

    async def async_get_iot_devices(self) -> list[dict[str, Any]]:
        """Read existing devices visible to this tenant API key."""
        items: list[dict[str, Any]] = []
        for page in range(1, 101):
            result = await self._request("GET", "/api/iot/devices", params={"page": page, "pageSize": 100})
            batch = result["items"]
            items.extend(batch)
            if not batch or len(items) >= result["totalItems"]:
                return items
        raise UteamupApiError("Too many devices to configure telemetry in one connection")

    async def async_get_effective_attributes(self, asset_guid: str) -> list[dict[str, Any]]:
        """Enrich effective public GUIDs with authoritative active/IoT meter flags."""
        groups = await self._request(
            "GET", f"/api/asset/by-guid/{asset_guid}/effective-attribute-definitions"
        )
        result = []
        for group in groups:
            type_guid = group["assetTypeGuid"]
            if type_guid not in self._telemetry_attribute_types:
                self._telemetry_attribute_types[type_guid] = await self._request(
                    "GET", f"/api/asset-type/{type_guid}/with-inherited-attributes"
                )
            asset_type = self._telemetry_attribute_types[type_guid]
            definitions = {item["externalGuid"]: item for item in asset_type.get("attributeDefinitions", [])}
            enriched = []
            for definition in group.get("definitions", []):
                full = definitions.get(definition["attributeDefinitionGuid"], {})
                enriched.append({
                    **definition,
                    "isActive": asset_type.get("isActive") is True and full.get("isActive") is True,
                    "allowIoTUpdates": full.get("allowIoTUpdates") is True,
                    "isMeter": full.get("isMeter") is True,
                    "unit": full.get("unit"),
                    "dataType": full.get("dataType"),
                })
            result.append({**group, "definitions": enriched})
        return result

    async def async_get_assets(self) -> list[dict[str, Any]]:
        items = []
        for page in range(1, 101):
            result = await self._request("GET", "/api/asset", params={"page": page, "pageSize": 100})
            batch = result["assets"]
            items.extend(item for item in batch if item.get("isActive"))
            if not batch or page >= result["totalPages"]:
                return items
        raise UteamupApiError("Too many assets to configure in one connection")

    async def async_get_asset(self, asset_guid: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/asset/by-guid/{asset_guid}")

    async def async_get_asset_types(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/asset-type")

    async def async_get_asset_type(self, type_guid: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/asset-type/{type_guid}/with-inherited-attributes")

    async def async_create_asset_type(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/asset-type", json_body=body)

    async def async_add_asset_type_attribute(self, type_guid: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/api/asset-type/{type_guid}/attributes", json_body=body)

    async def async_patch_asset(self, asset_guid: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/asset/by-guid/{asset_guid}", json_body=body)

    async def async_get_open_work_orders(self) -> dict[str, Any]:
        return await self._request(
            "GET", "/api/workorder", params={"openOnly": "true", "page": 1, "pageSize": 100}
        )

    async def async_get_work_order_calendar(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            "/api/workorder/calendar",
            params={"startDate": start.isoformat(), "endDate": end.isoformat()},
        )

    async def async_create_work_order(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/workorder", json_body=body)

    async def async_set_work_order_status(
        self, work_order_guid: str, status: int, expected_status: int | None = None
    ) -> dict[str, Any]:
        params = {"expectedStatus": expected_status} if expected_status is not None else None
        return await self._request(
            "PUT", f"/api/workorder/by-guid/{work_order_guid}/status/{status}", params=params
        )

    async def async_complete_work_order(self, work_order_guid: str, notes: str | None = None) -> dict[str, Any]:
        body = {"report": {"description": notes}} if notes else {}
        return await self._request("POST", f"/api/workorder/by-guid/{work_order_guid}/complete", json_body=body)

    async def async_create_quick_report(
        self,
        description: str,
        severity: str | None = None,
        asset_guid: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        # The endpoint only accepts multipart; without a file aiohttp would send a URL-encoded form (HTTP 415).
        form = aiohttp.FormData(default_to_multipart=True)
        form.add_field("Description", description)
        if severity:
            form.add_field("Severity", severity.upper())
        if asset_guid:
            form.add_field("AssetGuid", asset_guid)
        for tag in tags or []:
            form.add_field("Tags", tag)
        return await self._request("POST", "/api/quickreport", form=form)

    async def async_set_asset_state(self, asset_guid: str, state: str, reason: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"toState": state}
        if reason:
            body["reason"] = reason
        return await self._request("PUT", f"/api/assetstate/asset/by-guid/{asset_guid}/operational", json_body=body)

    async def async_run_pm_schedule(self, schedule_guid: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/workordertemplate/schedules/{schedule_guid}/runnow")

    async def async_record_meter_reading(
        self,
        asset_guid: str,
        attribute_definition_guid: str,
        value: float,
        timestamp: datetime | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"attributeDefinitionGuid": attribute_definition_guid, "readingValue": value}
        if timestamp:
            body["readingTimestamp"] = timestamp.isoformat()
        if notes:
            body["notes"] = notes
        return await self._request("POST", f"/api/assets/{asset_guid}/meter-readings", json_body=body)

    async def async_get_journal(self, journal_guid: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/journal/by-guid/{journal_guid}")

    async def async_append_journal_entry(self, journal_guid: str, entry: str) -> dict[str, Any]:
        current = await self.async_get_journal(journal_guid)
        existing_content = (current.get("content") or "").rstrip()
        new_line = f"<p>{html.escape(entry)}</p>"
        updated_content = f"{existing_content}\n{new_line}" if existing_content else new_line

        form = aiohttp.FormData(default_to_multipart=True)
        form.add_field("Title", str(current.get("title") or ""))
        form.add_field("Content", updated_content)
        form.add_field(
            "JournalDate",
            str(current.get("journalDate") or datetime.now(UTC).isoformat()),
        )
        form.add_field("Priority", str(current.get("priority") or 1))
        form.add_field("IsTemplate", "true" if current.get("isTemplate") else "false")
        form.add_field("IsArchived", "true" if current.get("isArchived") else "false")
        if current.get("summary"):
            form.add_field("Summary", str(current["summary"]))
        if current.get("categoryGuid"):
            form.add_field("CategoryGuid", str(current["categoryGuid"]))
        if current.get("locationGuid"):
            form.add_field("LocationGuid", str(current["locationGuid"]))
        if current.get("locationFloorGuid"):
            form.add_field("LocationFloorGuid", str(current["locationFloorGuid"]))
        return await self._request("PUT", f"/api/journal/by-guid/{journal_guid}", form=form)

    async def async_list_webhook_subscriptions(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/webhooks/subscriptions")

    async def async_create_webhook_subscription(
        self, name: str, url: str, secret: str, events: list[str]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/webhooks/subscriptions",
            json_body={"name": name, "url": url, "secret": secret, "events": events, "isActive": True},
        )

    async def async_delete_webhook_subscription(self, subscription_guid: str) -> None:
        await self._request("DELETE", f"/api/webhooks/subscriptions/{subscription_guid}")
