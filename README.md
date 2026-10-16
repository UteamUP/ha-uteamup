# UteamUP for Home Assistant

Bring your UteamUP maintenance work into Home Assistant. See open and overdue work orders, stock
and IoT alerts. Report problems, complete work, set asset states and push sensor readings into
UteamUP meters from automations.

## What you get

| Type | Entities |
|------|----------|
| Sensors | Open, in progress, on hold and overdue work orders; active stock alerts; unacknowledged IoT alerts |
| Binary sensors | Critical stock, IoT alert active |
| Calendar | Work orders by start and due date |
| To-do list | Open work orders. Checking one completes it; adding one creates a work order |
| Actions | `uteamup.create_work_order`, `uteamup.create_quick_report`, `uteamup.set_work_order_status`, `uteamup.complete_work_order`, `uteamup.set_asset_state`, `uteamup.run_pm_schedule`, `uteamup.record_meter_reading` |
| Events | `uteamup_event`, fired for every webhook UteamUP delivers (`workorder.created`, `iot.alert.created`, …) |
| Blueprint | Push a sensor to a UteamUP meter on a schedule |

## Before you start: create an API key in UteamUP

API keys need the **Premium** or **Enterprise** plan. Live events need **Enterprise**.

1. In UteamUP, create a role for Home Assistant that holds only what you want it to do:

   | To use | The role needs |
   |--------|----------------|
   | Work order counts | `Analytics.View`, `WorkOrder.View` |
   | Stock alerts | `Stock.View` |
   | IoT alerts | `IOT.View` |
   | Calendar and to-do list | `WorkOrder.View` |
   | Create work orders and quick reports | `WorkOrder.Create` |
   | Change status | `WorkOrder.Update` |
   | Complete work orders | `WorkOrder.Complete` |
   | Set asset state | `Asset.SetOperationalState` |
   | Run preventive maintenance | `WorkOrderTemplate.Update` and `WorkOrder.Create` |
   | Record meter readings | `MeterReading.Update` |
   | Live events | `Webhook.View` and `Webhook.Manage` |

2. Create a tenant API key with that role. **Leave MCP access off**: MCP keys cannot sign in here.
3. Copy the key and secret. UteamUP shows them only once.

**The key acts as the person who created it.** Work orders it creates are recorded as theirs, and
an action is allowed only when **both** the key's role **and** that person hold the permission. A
powerful creator never widens a narrow key.

## Install

**HACS**, or **Settings → Marketplace** from Home Assistant 2026.11:

1. Add `https://github.com/UteamUP/ha-uteamup` as a custom repository of type *Integration*.
2. Install **UteamUP** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → UteamUP**.
4. Enter the API address (`https://api.uteamup.com`), the key and the secret.

Entities that the key may not read stay unavailable, and the log names the missing permission.

## Live events

With **Receive live events** on (the default), the integration registers a UteamUP webhook that
points at Home Assistant. Deliveries are signed (HMAC-SHA256) and checked before they become
`uteamup_event` events, and each one also refreshes the entities immediately.

UteamUP does not deliver to local network addresses, so Home Assistant needs a public URL, such
as **Home Assistant Cloud** or an external URL. Without one, a repair issue explains why, and the
entities keep updating on the refresh interval (120 s by default).

```yaml
triggers:
  - trigger: event
    event_type: uteamup_event
    event_data:
      event: iot.alert.created
actions:
  - action: notify.mobile_app_phone
    data:
      message: "UteamUP IoT alert: {{ trigger.event.data.data.title }}"
```

## Examples

Report a leak when a water sensor trips:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.boiler_room_leak
    to: "on"
actions:
  - action: uteamup.create_quick_report
    data:
      description: Water detected in the boiler room
      severity: critical
      asset_guid: 2f1c…   # the boiler's GUID in UteamUP
```

Push an energy meter every hour: import the blueprint
`blueprints/automation/uteamup/push_sensor_to_meter.yaml`. Every reading is stored permanently in
UteamUP as a manual reading, so keep the interval coarse.

## Options

- **Refresh interval**: 30 to 3600 seconds.
- **Receive live events**: on or off.
- **Only list work orders assigned to**: a UteamUP user GUID, for a personal to-do list.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest
```

`tests/live` runs the integration against a real UteamUP API and is skipped unless
`UTEAMUP_LIVE_KEY_FILE` points at a JSON file with `base`, `tenant`, `apiKey`, `secret`,
`assetGuid` and `attributeDefinitionGuid`. Use a development API: it creates a quick report,
a meter reading and a temporary webhook subscription.

## License

MIT. See [LICENSE](LICENSE).
