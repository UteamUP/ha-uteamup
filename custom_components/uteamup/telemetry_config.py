"""Validated, stable mappings from HA energy sources to existing tenant devices."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_DEVICE_KEY, CONF_TENANT_GUID


def public_guid(value: Any) -> str:
    """Normalize a non-empty public identifier."""
    parsed = UUID(str(value))
    if parsed.int == 0:
        raise ValueError("invalid_guid")
    return str(parsed)


def ingestion_endpoint(value: Any) -> str:
    """Accept only an HTTPS origin, never credentials, queries or route overrides."""
    address = str(value).rstrip("/")
    parsed = urlsplit(address)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("environment_unavailable")
    return address


def environment_settings(response: dict[str, Any], tenant_guid: str) -> dict[str, str]:
    """Cache only the server-approved active, healthy tenant environment."""
    environment = response.get("environment") or {}
    if (
        not response.get("isEnabled")
        or str(response.get("status", "")).lower() != "active"
        or str(environment.get("status", "")).lower() != "active"
        or environment.get("isHealthy") is not True
        or public_guid(environment.get("tenantGuid")) != public_guid(tenant_guid)
    ):
        raise ValueError("environment_unavailable")
    return {
        "endpoint": ingestion_endpoint(environment.get("httpBaseUrl")),
        "environment_guid": public_guid(environment.get("environmentGuid")),
        CONF_TENANT_GUID: public_guid(tenant_guid),
    }


def normalize_mac(value: Any) -> str:
    """Compare physical MACs independently of separator and letter case."""
    result = str(value or "").lower().replace(":", "").replace("-", "")
    if len(result) != 12 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError("device_mismatch")
    return result


@dataclass(frozen=True)
class TelemetryMapping:
    """All identities are stable registry IDs or public UteamUP GUIDs."""

    device_guid: str
    asset_guid: str
    name: str
    ha_device_id: str
    mac_address: str
    source_registry_id: str
    power_registry_id: str
    energy_registry_id: str
    power_attribute_guid: str
    energy_attribute_guid: str
    estimate_profile: str
    device_key: str = field(repr=False)

    @property
    def fingerprint(self) -> str:
        """A credential rotation does not change the meaning of queued readings."""
        identity = asdict(self)
        identity.pop(CONF_DEVICE_KEY)
        identity.pop("name")
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @property
    def credential_hash(self) -> str:
        return hashlib.sha256(self.device_key.encode()).hexdigest()


@dataclass(frozen=True)
class TelemetrySettings:
    endpoint: str
    tenant_guid: str
    environment_guid: str
    mappings: tuple[TelemetryMapping, ...]
    retry_nonce: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], tenant_guid: str) -> TelemetrySettings:
        """Revalidate saved data before any network traffic after a restart."""
        tenant = public_guid(data[CONF_TENANT_GUID])
        if tenant != public_guid(tenant_guid):
            raise ValueError("device_mismatch")
        mappings = tuple(TelemetryMapping(**item) for item in data["mappings"])
        if not mappings or len({item.device_guid for item in mappings}) != len(mappings):
            raise ValueError("device_mismatch")
        for mapping in mappings:
            for identifier in (
                mapping.device_guid, mapping.asset_guid,
                mapping.power_attribute_guid, mapping.energy_attribute_guid,
            ):
                public_guid(identifier)
            normalize_mac(mapping.mac_address)
            if not mapping.device_key.strip() or not mapping.estimate_profile.strip():
                raise ValueError("invalid_mapping")
        return cls(
            ingestion_endpoint(data["endpoint"]), tenant,
            public_guid(data["environment_guid"]), mappings, str(data.get("retry_nonce", "")),
        )


def source_belongs_to_device(
    hass: HomeAssistant, entity_id: str, device_id: str, source_id: str, visited: set[str] | None = None,
) -> bool:
    """Follow native Integral sources or explicit helper provenance, with cycle protection."""
    registry = er.async_get(hass)
    entity = registry.async_get(entity_id)
    if entity is None or entity.disabled:
        return False
    if entity.device_id == device_id:
        return True
    visited = set() if visited is None else visited
    if entity.id in visited or len(visited) >= 4:
        return False
    visited.add(entity.id)
    state = hass.states.get(entity.entity_id)
    if state is None:
        return False
    attributes = state.attributes
    if attributes.get("source_device_id") == device_id:
        return True
    if attributes.get("source_entity_registry_id") == source_id:
        return True
    reference = attributes.get("source_entity") or attributes.get("source")
    parent = registry.async_get(reference) if isinstance(reference, str) else None
    return parent is not None and (
        parent.id == source_id
        or source_belongs_to_device(hass, parent.entity_id, device_id, source_id, visited)
    )


def source_unit(entity: er.RegistryEntry, state: State | None) -> str | None:
    return (state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None) or entity.unit_of_measurement


def build_mapping(
    hass: HomeAssistant, device: dict[str, Any], attributes: list[dict[str, Any]],
    inputs: dict[str, Any], previous: TelemetryMapping | None = None,
) -> TelemetryMapping:
    """Reject mismatched physical sources and incompatible meter semantics before saving."""
    registry = er.async_get(hass)
    source = registry.async_get(inputs["source_entity"])
    power = registry.async_get(inputs["power_entity"])
    energy = registry.async_get(inputs["energy_entity"])
    if source is None or source.domain != "light" or not source.device_id or source.disabled:
        raise ValueError("source_missing")
    physical = dr.async_get(hass).async_get(source.device_id)
    mac = normalize_mac(device.get("macAddress"))
    if physical is None or not any(
        kind == dr.CONNECTION_NETWORK_MAC and normalize_mac(address) == mac
        for kind, address in physical.connections
    ):
        raise ValueError("device_mismatch")
    for entity, unit in ((power, "W"), (energy, "kWh")):
        if entity is None or entity.domain != "sensor" or source_unit(entity, hass.states.get(entity.entity_id)) != unit:
            raise ValueError("unit_mismatch")
        if not source_belongs_to_device(hass, entity.entity_id, source.device_id, source.id):
            raise ValueError("device_mismatch")
    definitions = {item["attributeDefinitionGuid"]: item for group in attributes for item in group.get("definitions", [])}
    for field_name, unit in (("power_attribute_guid", "W"), ("energy_attribute_guid", "kWh")):
        definition = definitions.get(inputs[field_name]) or {}
        if not definition.get("isMeter") or definition.get("unit") != unit:
            raise ValueError("attribute_mismatch")
    profile = str(inputs["estimate_profile"]).strip()
    key = str(inputs.get(CONF_DEVICE_KEY, "")).strip() or (previous.device_key if previous else "")
    if not key or len(key) > 512 or len(profile) > 100 or not profile:
        raise ValueError("invalid_mapping")
    return TelemetryMapping(
        public_guid(device["externalGuid"]), public_guid(device["assetGuid"]), str(device["name"]),
        source.device_id, mac, source.id, power.id, energy.id,
        public_guid(inputs["power_attribute_guid"]), public_guid(inputs["energy_attribute_guid"]), profile, key,
    )
