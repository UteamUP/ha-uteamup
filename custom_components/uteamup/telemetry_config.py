"""Validated, stable mappings from HA energy sources to existing tenant devices."""

from __future__ import annotations

import hashlib
import json
from ipaddress import IPv4Address, IPv4Network
from dataclasses import asdict, dataclass, field
from typing import Any
from unicodedata import category
from urllib.parse import urlsplit
from uuid import UUID

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (CONF_COMPONENT_TELEMETRY, CONF_DEVICE_KEY, CONF_EDGE_BRIDGE_CERTIFICATE_FINGERPRINT,
                    CONF_EDGE_BRIDGE_DEVICES, CONF_EDGE_BRIDGE_ENDPOINT, CONF_EDGE_BRIDGE_TOKEN, CONF_TENANT_GUID)

SOURCE_DOMAINS = ("light", "switch")
POWER_BASES = ("estimated", "measured")
MAX_COMPONENT_READINGS = 96


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
        or any(character.isspace() for character in address)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("environment_unavailable")
    # Accessing port validates malformed/out-of-range values without opening a connection.
    _ = parsed.port
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


def edge_bridge_endpoint(value: Any) -> str:
    address = str(value).rstrip("/")
    parsed = urlsplit(address)
    try:
        host = IPv4Address(parsed.hostname or "")
        private = any(host in IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
        valid_port = parsed.port is not None and 1024 <= parsed.port <= 65535
    except ValueError as error:
        raise ValueError("invalid_mapping") from error
    if (not private or not valid_port or parsed.scheme != "https" or "@" in parsed.netloc
            or parsed.path or parsed.query or parsed.fragment or any(character.isspace() for character in address)):
        raise ValueError("invalid_mapping")
    return address


def edge_bridge_certificate_fingerprint(value: Any) -> str:
    """Accept a complete SHA-256 certificate fingerprint copied from the collector."""
    if not isinstance(value, str):
        raise ValueError("invalid_mapping")
    if not value:
        return ""
    fingerprint = value.lower().replace(":", "")
    if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
        raise ValueError("invalid_mapping")
    return fingerprint


def normalize_mac(value: Any) -> str:
    """Compare physical MACs independently of separator and letter case."""
    result = str(value or "").lower().replace(":", "").replace("-", "")
    if len(result) != 12 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError("device_mismatch")
    return result


def device_has_mac(device: dr.DeviceEntry | None, expected_mac: str) -> bool:
    if device is None:
        return False
    for kind, address in device.connections:
        if kind == dr.CONNECTION_NETWORK_MAC:
            try:
                if normalize_mac(address) == expected_mac:
                    return True
            except ValueError:
                continue
    return False


def device_reference_matches(hass: HomeAssistant, stored_id: str | None, current_id: str | None) -> bool:
    """Follow only HA's recorded split of a previously shared device identity."""
    if not stored_id or not current_id:
        return False
    if stored_id == current_id:
        return True
    current = dr.async_get(hass).async_get(current_id)
    return current is not None and getattr(current, "composite_device_id", None) == stored_id


def valid_device_key(value: str) -> bool:
    """Device keys are printable ASCII tokens, never arbitrary header content."""
    return 0 < len(value) <= 512 and all(32 < ord(character) < 127 for character in value)


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
    power_basis: str = "estimated"

    @property
    def fingerprint(self) -> str:
        """A credential rotation does not change the meaning of queued readings."""
        identity = asdict(self)
        identity.pop(CONF_DEVICE_KEY)
        identity.pop("name")
        identity.pop("estimate_profile")
        # Existing estimated mappings must keep accepting their durable queued data.
        if self.power_basis == "estimated":
            identity.pop("power_basis")
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @property
    def credential_hash(self) -> str:
        return hashlib.sha256(self.device_key.encode()).hexdigest()


NETWORK_METRICS = (
    ("download", "sensor", "kB/s"),
    ("upload", "sensor", "kB/s"),
    ("clients", "sensor", "clients"),
    ("internet", "binary_sensor", "0/1"),
    ("router", "binary_sensor", "0/1"),
)


CLIENT_METRICS = (("download", "kB/s"), ("upload", "kB/s"), ("connected", "0/1"))


@dataclass(frozen=True)
class DecoClientMapping:
    tracker_registry_id: str
    mac_address: str
    asset_guid: str | None
    download_attribute_guid: str
    upload_attribute_guid: str
    connected_attribute_guid: str
    part_guid: str | None = None
    tool_guid: str | None = None
    link_guid: str | None = None
    link_revision: str | None = None

    @property
    def owner_payload(self) -> dict[str, str]:
        owners = {key: public_guid(value) for key, value in (
            ("assetGuid", self.asset_guid), ("partGuid", self.part_guid), ("toolGuid", self.tool_guid),
        ) if value is not None}
        if len(owners) != 1:
            raise ValueError("invalid_client_links")
        return owners


@dataclass(frozen=True)
class NetworkTelemetryMapping:
    """Stable Deco tracker, network sources and their corresponding public meters."""

    device_guid: str
    asset_guid: str
    name: str
    ha_device_id: str
    mac_address: str
    tracker_registry_id: str
    download_registry_id: str
    upload_registry_id: str
    clients_registry_id: str
    internet_registry_id: str
    router_registry_id: str
    download_attribute_guid: str
    upload_attribute_guid: str
    clients_attribute_guid: str
    internet_attribute_guid: str
    router_attribute_guid: str
    device_key: str = field(repr=False)
    clients: tuple[DecoClientMapping, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NetworkTelemetryMapping:
        return cls(**{key: value for key, value in data.items() if key != "clients"},
                   clients=tuple(DecoClientMapping(**client) for client in data.get("clients", [])))

    @property
    def fingerprint(self) -> str:
        identity = asdict(self)
        identity.pop(CONF_DEVICE_KEY)
        identity.pop("name")
        for client in identity["clients"]:
            # New optional server-owner fields must not invalidate already queued asset samples.
            for key in ("part_guid", "tool_guid", "link_guid", "link_revision"):
                if client[key] is None:
                    client.pop(key)
        if not self.clients:
            identity.pop("clients")
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @property
    def credential_hash(self) -> str:
        return hashlib.sha256(self.device_key.encode()).hexdigest()


@dataclass(frozen=True)
class MeasurementSource:
    """One explicitly selected numeric or binary observation, never a vendor-code guess."""

    source_registry_id: str
    attribute_guid: str
    unit: str
    true_state: str = ""
    known_states: tuple[str, ...] = ()
    source_attribute: str = "state"


@dataclass(frozen=True)
class MeasurementMapping:
    """Reviewed physical device and independent meters on its existing UteamUP asset."""

    device_guid: str
    asset_guid: str
    name: str
    ha_device_id: str
    anchor_registry_id: str
    anchor_unique_id: str
    anchor_platform: str
    readings: tuple[MeasurementSource, ...]
    device_key: str = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, require_device_key: bool = True) -> MeasurementMapping:
        readings = tuple(MeasurementSource(**{**item, "known_states": tuple(item.get("known_states", ()))})
                         for item in data.get("readings", ()))
        result = cls(**{**data, "readings": readings})
        for identifier in (result.device_guid, result.asset_guid):
            public_guid(identifier)
        for identifier in (result.ha_device_id, result.anchor_registry_id):
            UUID(identifier)
        if ((require_device_key and not valid_device_key(result.device_key)) or not result.anchor_unique_id or not result.anchor_platform
                or not 1 <= len(readings) <= 64
                or len({(item.source_registry_id, item.true_state, item.source_attribute)
                        for item in readings}) != len(readings)
                or len({item.attribute_guid for item in readings}) != len(readings)):
            raise ValueError("invalid_mapping")
        for reading in readings:
            UUID(reading.source_registry_id)
            public_guid(reading.attribute_guid)
            if (not isinstance(reading.unit, str) or len(reading.unit) > 50
                    or reading.source_attribute not in {"state", "availability", "brightness", "color_temp_kelvin"}
                    or (reading.source_attribute != "state" and (reading.true_state or reading.known_states))
                    or (reading.source_attribute == "availability" and reading.unit != "0/1")
                    or (reading.source_attribute == "brightness" and reading.unit != "%")
                    or (reading.source_attribute == "color_temp_kelvin" and reading.unit != "K")
                    or (reading.true_state and (reading.unit != "0/1" or reading.true_state not in reading.known_states))
                    or (reading.known_states and not reading.true_state)):
                raise ValueError("invalid_mapping")
        return result

    @property
    def fingerprint(self) -> str:
        identity = asdict(self)
        identity.pop(CONF_DEVICE_KEY)
        identity.pop("name")
        # Old state-only mappings must keep accepting their original durable queue.
        for reading in identity["readings"]:
            if reading["source_attribute"] == "state":
                reading.pop("source_attribute")
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @property
    def credential_hash(self) -> str:
        return hashlib.sha256(self.device_key.encode()).hexdigest()


def component_registry_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_registry_identity")
    identifier = UUID(value)
    if identifier.int == 0 or value not in {identifier.hex, str(identifier)}:
        raise ValueError("invalid_registry_identity")
    return value


@dataclass(frozen=True)
class ComponentMeasurementSource:
    """One reviewed local Matter observation, without a target GUID."""

    source_registry_id: str
    unit: str
    true_state: str = ""
    known_states: tuple[str, ...] = ()
    source_attribute: str = "state"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComponentMeasurementSource:
        if not isinstance(data, dict) or set(data) - {"source_registry_id", "unit", "true_state", "known_states", "source_attribute"}:
            raise ValueError("invalid_mapping")
        states = data.get("known_states", ())
        if not isinstance(states, (tuple, list)):
            raise ValueError("invalid_mapping")
        source = cls(**{**data, "known_states": tuple(states)})
        component_registry_id(source.source_registry_id)
        _ = source.channel_attribute
        return source

    @property
    def channel_attribute(self) -> str:
        attribute_units = {"availability": "0/1", "brightness": "%", "color_temp_kelvin": "K"}
        if (not isinstance(self.unit, str) or not self.unit or self.unit != self.unit.strip()
                or any(category(character) in {"Cc", "Cf", "Cs"} for character in self.unit)
                or len(self.unit.encode("utf-16-le")) > 100):
            raise ValueError("invalid_mapping")
        if not isinstance(self.source_attribute, str) or not isinstance(self.true_state, str):
            raise ValueError("invalid_mapping")
        if self.source_attribute != "state":
            if attribute_units.get(self.source_attribute) != self.unit or self.true_state or self.known_states:
                raise ValueError("invalid_mapping")
        if self.true_state or self.known_states:
            if (self.unit != "0/1" or self.known_states != ("on", "off") or self.true_state not in self.known_states):
                raise ValueError("invalid_mapping")
            return f"state.{self.true_state}"
        return self.source_attribute


@dataclass(frozen=True)
class ComponentMeasurementSelection:
    ha_device_id: str
    anchor_registry_id: str
    anchor_unique_id: str
    readings: tuple[ComponentMeasurementSource, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComponentMeasurementSelection:
        if not isinstance(data, dict) or set(data) != {"ha_device_id", "anchor_registry_id", "anchor_unique_id", "readings"}:
            raise ValueError("invalid_mapping")
        readings = data["readings"]
        if not isinstance(readings, (tuple, list)) or not 1 <= len(readings) <= 64:
            raise ValueError("invalid_mapping")
        selection = cls(component_registry_id(data["ha_device_id"]), component_registry_id(data["anchor_registry_id"]),
                        data["anchor_unique_id"], tuple(ComponentMeasurementSource.from_dict(reading) for reading in readings))
        if (not isinstance(selection.anchor_unique_id, str) or not selection.anchor_unique_id.strip()
                or len(selection.anchor_unique_id) > 512
                or len({(reading.source_registry_id, reading.channel_attribute) for reading in selection.readings}) != len(readings)):
            raise ValueError("invalid_mapping")
        return selection


@dataclass(frozen=True)
class ComponentTelemetrySettings:
    source_installation_guid: str
    source_generation_guid: str
    environment_guid: str
    selections: tuple[ComponentMeasurementSelection, ...]
    device_key: str = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComponentTelemetrySettings:
        fields = {"source_installation_guid", "source_generation_guid", "environment_guid", "selections", CONF_DEVICE_KEY}
        if (not isinstance(data, dict) or set(data) != fields or not isinstance(data[CONF_DEVICE_KEY], str)
                or not 8 <= len(data[CONF_DEVICE_KEY]) <= 500 or not valid_device_key(data[CONF_DEVICE_KEY])):
            raise ValueError("invalid_mapping")
        selections = data["selections"]
        if not isinstance(selections, (tuple, list)) or not 1 <= len(selections) <= 32:
            raise ValueError("invalid_mapping")
        result = cls(public_guid(data["source_installation_guid"]), public_guid(data["source_generation_guid"]),
                     public_guid(data["environment_guid"]),
                     tuple(ComponentMeasurementSelection.from_dict(item) for item in selections), data[CONF_DEVICE_KEY])
        if (sum(len(item.readings) for item in result.selections) > MAX_COMPONENT_READINGS
                or len({UUID(item.ha_device_id) for item in result.selections}) != len(result.selections)):
            raise ValueError("invalid_mapping")
        return result

    @property
    def fingerprint(self) -> str:
        data = asdict(self)
        data.pop(CONF_DEVICE_KEY)
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

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
    network_mappings: tuple[NetworkTelemetryMapping, ...] = ()
    measurement_mappings: tuple[MeasurementMapping, ...] = ()
    edge_bridge_endpoint: str = ""
    edge_bridge_token: str = field(default="", repr=False)
    edge_bridge_devices: tuple[str, ...] = ()
    edge_bridge_certificate_fingerprint: str = ""
    component_telemetry: ComponentTelemetrySettings | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], tenant_guid: str) -> TelemetrySettings:
        """Revalidate saved data before any network traffic after a restart."""
        tenant = public_guid(data[CONF_TENANT_GUID])
        environment = public_guid(data["environment_guid"])
        if tenant != public_guid(tenant_guid):
            raise ValueError("device_mismatch")
        bridge_devices = tuple(public_guid(value) for value in data.get(CONF_EDGE_BRIDGE_DEVICES, ()))
        bridge_endpoint = str(data.get(CONF_EDGE_BRIDGE_ENDPOINT, ""))
        bridge_token = str(data.get(CONF_EDGE_BRIDGE_TOKEN, ""))
        bridge_fingerprint = edge_bridge_certificate_fingerprint(data.get(CONF_EDGE_BRIDGE_CERTIFICATE_FINGERPRINT, ""))
        if bridge_devices or bridge_endpoint or bridge_token or bridge_fingerprint:
            bridge_endpoint = edge_bridge_endpoint(bridge_endpoint)
            if (not bridge_devices or len(bridge_devices) > 1000 or len(set(bridge_devices)) != len(bridge_devices)
                    or len(bridge_token) < 32 or not valid_device_key(bridge_token)):
                raise ValueError("invalid_mapping")
        mappings = tuple(TelemetryMapping(**item) for item in data.get("mappings", []))
        network_mappings = tuple(NetworkTelemetryMapping.from_dict(item) for item in data.get("network_mappings", []))
        measurement_mappings = tuple(MeasurementMapping.from_dict(item, require_device_key=item["device_guid"] not in bridge_devices)
                                     for item in data.get("measurement_mappings", []))
        combined = (*mappings, *network_mappings, *measurement_mappings)
        component_value = data.get(CONF_COMPONENT_TELEMETRY)
        components = ComponentTelemetrySettings.from_dict(component_value) if component_value is not None else None
        if (not combined and components is None) or len({item.device_guid for item in combined}) != len(combined):
            raise ValueError("device_mismatch")
        if len({item.ha_device_id for item in combined}) != len(combined):
            raise ValueError("device_mismatch")
        if components is not None and (components.environment_guid != environment
                or components.source_installation_guid in bridge_devices
                or components.source_installation_guid in {public_guid(item.device_guid) for item in combined}
                or {UUID(selection.ha_device_id) for selection in components.selections} & {UUID(item.ha_device_id) for item in combined}):
            raise ValueError("device_mismatch")
        for mapping in mappings:
            for identifier in (
                mapping.device_guid, mapping.asset_guid,
                mapping.power_attribute_guid, mapping.energy_attribute_guid,
            ):
                public_guid(identifier)
            normalize_mac(mapping.mac_address)
            for registry_id in (mapping.ha_device_id, mapping.source_registry_id, mapping.power_registry_id, mapping.energy_registry_id):
                UUID(registry_id)
            if (
                not valid_device_key(mapping.device_key) or mapping.power_basis not in POWER_BASES
                or (mapping.power_basis == "estimated" and not mapping.estimate_profile.strip())
            ):
                raise ValueError("invalid_mapping")
        for mapping in network_mappings:
            for identifier in (
                mapping.device_guid, mapping.asset_guid,
                *(getattr(mapping, f"{name}_attribute_guid") for name, _, _ in NETWORK_METRICS),
            ):
                public_guid(identifier)
            normalize_mac(mapping.mac_address)
            for registry_id in (
                mapping.ha_device_id, mapping.tracker_registry_id,
                *(getattr(mapping, f"{name}_registry_id") for name, _, _ in NETWORK_METRICS),
            ):
                UUID(registry_id)
            if not valid_device_key(mapping.device_key):
                raise ValueError("invalid_mapping")
            for client in mapping.clients:
                UUID(client.tracker_registry_id)
                normalize_mac(client.mac_address)
                _ = client.owner_payload
                for identifier in (client.link_guid, client.link_revision):
                    if identifier is not None:
                        public_guid(identifier)
                for name, _ in CLIENT_METRICS:
                    public_guid(getattr(client, f"{name}_attribute_guid"))
        clients = [client for mapping in network_mappings for client in mapping.clients]
        source_clients = {(mapping.device_guid, client.tracker_registry_id)
                          for mapping in network_mappings for client in mapping.clients}
        meters = {(tuple(client.owner_payload.items()), getattr(client, f"{name}_attribute_guid"))
                  for client in clients for name, _ in CLIENT_METRICS}
        if len(source_clients) != len(clients) or len(meters) != len(clients) * len(CLIENT_METRICS):
            raise ValueError("device_mismatch")
        return cls(
            endpoint=ingestion_endpoint(data["endpoint"]),
            tenant_guid=tenant,
            environment_guid=environment,
            mappings=mappings,
            retry_nonce=str(data.get("retry_nonce", "")),
            network_mappings=network_mappings,
            measurement_mappings=measurement_mappings,
            edge_bridge_endpoint=bridge_endpoint,
            edge_bridge_token=bridge_token,
            edge_bridge_devices=bridge_devices,
            edge_bridge_certificate_fingerprint=bridge_fingerprint,
            component_telemetry=components,
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
    if device_reference_matches(hass, attributes.get("source_device_id"), device_id):
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


def network_unit_matches(entity: er.RegistryEntry, state: State | None, domain: str, unit: str) -> bool:
    """Deco client counts are natively unitless; traffic rates must retain kB/s."""
    if domain == "binary_sensor":
        return True
    actual = source_unit(entity, state)
    return actual == unit or (unit == "clients" and not actual)


def compatible_meter(definition: dict[str, Any], unit: str) -> bool:
    return (
        definition.get("isMeter") is True and definition.get("unit") == unit
        and definition.get("isActive") is True and definition.get("allowIoTUpdates") is True
        and definition.get("dataType") in {2, 3, "integer", "float", "Integer", "Float"}
    )


def mapping_source_choices(hass: HomeAssistant, device: dict[str, Any]) -> dict[str, list[str]]:
    """Offer the physical light or socket and only its own meter sources."""
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    mac = normalize_mac(device.get("macAddress"))
    sources = [
        item for item in registry.entities.values() if item.domain in SOURCE_DOMAINS and not item.disabled
        and item.device_id and device_has_mac(devices.async_get(item.device_id), mac)
    ]
    choices = {"source_entity": [item.entity_id for item in sources], "power_entity": [], "energy_entity": []}
    for entity in registry.entities.values():
        if entity.domain != "sensor" or entity.disabled:
            continue
        unit = source_unit(entity, hass.states.get(entity.entity_id))
        if unit not in {"W", "kWh"} or not any(
            source_belongs_to_device(hass, entity.entity_id, source.device_id, source.id) for source in sources
        ):
            continue
        choices["power_entity" if unit == "W" else "energy_entity"].append(entity.entity_id)
    return choices


def measured_wiz_sources_match(
    hass: HomeAssistant, source: er.RegistryEntry, meters: tuple[er.RegistryEntry, er.RegistryEntry],
) -> bool:
    """Measured socket power must come from WiZ, with energy derived from that same meter."""
    power, energy = meters
    if source.domain != "switch" or source.platform != "wiz":
        return False
    if power.platform != "wiz" or power.device_id != source.device_id:
        return False
    for entity in meters:
        state = hass.states.get(entity.entity_id)
        if state is None or state.attributes.get("estimate_profile") or state.attributes.get("power_basis") == "estimated":
            return False
    if energy.platform == "wiz" and energy.device_id == source.device_id:
        return True
    energy_state = hass.states.get(energy.entity_id)
    return energy.platform == "integration" and energy_state.attributes.get("source") in {
        power.entity_id, power.id,
    }


def build_mapping(
    hass: HomeAssistant, device: dict[str, Any], attributes: list[dict[str, Any]],
    inputs: dict[str, Any], previous: TelemetryMapping | None = None,
) -> TelemetryMapping:
    """Reject mismatched physical sources and incompatible meter semantics before saving."""
    registry = er.async_get(hass)
    source = registry.async_get(inputs["source_entity"])
    power = registry.async_get(inputs["power_entity"])
    energy = registry.async_get(inputs["energy_entity"])
    if source is None or source.domain not in SOURCE_DOMAINS or not source.device_id or source.disabled:
        raise ValueError("source_missing")
    physical = dr.async_get(hass).async_get(source.device_id)
    mac = normalize_mac(device.get("macAddress"))
    if not device_has_mac(physical, mac):
        raise ValueError("device_mismatch")
    for entity, unit in ((power, "W"), (energy, "kWh")):
        if entity is None or entity.disabled or entity.domain != "sensor" or source_unit(entity, hass.states.get(entity.entity_id)) != unit:
            raise ValueError("unit_mismatch")
    definitions = {item["attributeDefinitionGuid"]: item for group in attributes for item in group.get("definitions", [])}
    for field_name, unit in (("power_attribute_guid", "W"), ("energy_attribute_guid", "kWh")):
        definition = definitions.get(inputs[field_name]) or {}
        if not compatible_meter(definition, unit):
            raise ValueError("attribute_mismatch")
    basis = inputs.get("power_basis", "estimated")
    profile = str(inputs.get("estimate_profile", "")).strip()
    key = str(inputs.get(CONF_DEVICE_KEY, "")).strip() or (previous.device_key if previous else "")
    if (
        not valid_device_key(key) or basis not in POWER_BASES or len(profile) > 100
        or (basis == "estimated" and not profile)
    ):
        raise ValueError("invalid_mapping")
    mapping = TelemetryMapping(
        public_guid(device["externalGuid"]), public_guid(device["assetGuid"]), str(device["name"]),
        source.device_id, mac, source.id, power.id, energy.id,
        public_guid(inputs["power_attribute_guid"]), public_guid(inputs["energy_attribute_guid"]),
        profile if basis == "estimated" else "", key, basis,
    )
    # An unavailable template can lose its live provenance attributes. Keeping an
    # identical saved mapping must not block configuring another device. Capture
    # still revalidates availability and provenance before producing any readings.
    unavailable = any(
        (state := hass.states.get(entity.entity_id)) is None
        or state.state in {STATE_UNAVAILABLE, STATE_UNKNOWN}
        for entity in (source, power, energy)
    )
    if mapping == previous and unavailable:
        return mapping
    for entity in (power, energy):
        if not source_belongs_to_device(hass, entity.entity_id, source.device_id, source.id):
            raise ValueError("device_mismatch")
    if basis == "measured" and not measured_wiz_sources_match(hass, source, (power, energy)):
        raise ValueError("measured_source_required")
    return mapping


def validate_deco_tracker(
    hass: HomeAssistant, tracker: er.RegistryEntry | None, expected_mac: str,
) -> State:
    """Authenticate the physical Deco through its tracker attributes, not registry connections."""
    if (
        tracker is None or tracker.disabled or tracker.domain != "device_tracker"
        or tracker.platform != "tplink_deco" or not tracker.device_id
    ):
        raise ValueError("source_missing")
    state = hass.states.get(tracker.entity_id)
    if state is None or state.state in {STATE_UNAVAILABLE, STATE_UNKNOWN} or state.attributes.get("restored"):
        raise ValueError("unavailable")
    try:
        matches = (
            state.attributes.get("device_type") == "deco"
            and normalize_mac(state.attributes.get("mac")) == expected_mac
        )
    except ValueError:
        matches = False
    if not matches:
        raise ValueError("device_mismatch")
    return state


def network_source_choices(hass: HomeAssistant, device: dict[str, Any]) -> dict[str, list[str]]:
    """Offer only entities on the Deco whose tracker has the configured physical MAC."""
    registry = er.async_get(hass)
    mac = normalize_mac(device.get("macAddress"))
    choices = {"tracker_entity": [], **{f"{name}_entity": [] for name, _, _ in NETWORK_METRICS}}
    for tracker in registry.entities.values():
        try:
            validate_deco_tracker(hass, tracker, mac)
        except ValueError:
            continue
        choices["tracker_entity"].append(tracker.entity_id)
        for entity in registry.entities.values():
            if entity.disabled or entity.device_id != tracker.device_id:
                continue
            for name, domain, unit in NETWORK_METRICS:
                if entity.domain != domain:
                    continue
                if network_unit_matches(entity, hass.states.get(entity.entity_id), domain, unit):
                    choices[f"{name}_entity"].append(entity.entity_id)
    return choices


def build_network_mapping(
    hass: HomeAssistant, device: dict[str, Any], attributes: list[dict[str, Any]],
    inputs: dict[str, Any], previous: NetworkTelemetryMapping | None = None,
) -> NetworkTelemetryMapping:
    """Validate one physical Deco and all five network telemetry mappings."""
    registry = er.async_get(hass)
    mac = normalize_mac(device.get("macAddress"))
    tracker = registry.async_get(inputs["tracker_entity"])
    validate_deco_tracker(hass, tracker, mac)
    assert tracker is not None and tracker.device_id is not None
    entities: dict[str, er.RegistryEntry] = {}
    for name, domain, unit in NETWORK_METRICS:
        entity = registry.async_get(inputs[f"{name}_entity"])
        if entity is None or entity.disabled or entity.domain != domain or entity.device_id != tracker.device_id:
            raise ValueError("device_mismatch")
        state = hass.states.get(entity.entity_id)
        if state is None or state.state in {STATE_UNAVAILABLE, STATE_UNKNOWN} or state.attributes.get("restored"):
            raise ValueError("unavailable")
        if domain == "binary_sensor":
            if state.state not in {"on", "off"}:
                raise ValueError("invalid_value")
        elif not network_unit_matches(entity, state, domain, unit):
            raise ValueError("unit_mismatch")
        entities[name] = entity
    definitions = {
        item["attributeDefinitionGuid"]: item
        for group in attributes for item in group.get("definitions", [])
    }
    for name, _, unit in NETWORK_METRICS:
        definition = definitions.get(inputs[f"{name}_attribute_guid"]) or {}
        if not compatible_meter(definition, unit):
            raise ValueError("attribute_mismatch")
    key = str(inputs.get(CONF_DEVICE_KEY, "")).strip() or (previous.device_key if previous else "")
    if not valid_device_key(key):
        raise ValueError("invalid_mapping")
    return NetworkTelemetryMapping(
        device_guid=public_guid(device["externalGuid"]),
        asset_guid=public_guid(device["assetGuid"]),
        name=str(device["name"]),
        ha_device_id=tracker.device_id,
        mac_address=mac,
        tracker_registry_id=tracker.id,
        **{f"{name}_registry_id": entities[name].id for name, _, _ in NETWORK_METRICS},
        **{
            f"{name}_attribute_guid": public_guid(inputs[f"{name}_attribute_guid"])
            for name, _, _ in NETWORK_METRICS
        },
        device_key=key,
        clients=previous.clients if previous else (),
    )
