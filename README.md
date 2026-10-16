# UteamUP for Home Assistant

Bring your UteamUP maintenance work into Home Assistant. See open and overdue work orders, stock
and IoT alerts. Report problems, complete work, set asset states and push sensor readings into
UteamUP meters from automations.

## Quick setup in plain English

This takes about ten minutes. You need two things:

- A UteamUP company on the **Premium** or **Enterprise** plan, and permission to manage it.
- Home Assistant, version **2025.2 or newer**, with **HACS** installed (from Home Assistant
  2026.11 the same store is built in as **Settings → Marketplace**).

### Step 1: Get a "key" from UteamUP

Home Assistant signs in to UteamUP with a key and a secret instead of your password. They work
like a username and password made just for Home Assistant, and you can switch them off at any time.

1. Sign in to UteamUP.
2. Open **Tenant Management** and choose the **API Keys** tab.
3. Click **Create API Key**. Don't create it from the **MCP Keys** tab: those keys are for AI
   assistants and Home Assistant can't use them.
4. Give it a name you'll recognise, such as *Home Assistant*.
5. Choose a **role**. The role decides what Home Assistant may see and do. *Manager* lets it do
   everything below. A narrower role works too (see [the permission list](#what-the-key-may-do)).
6. Click **Create Key**. UteamUP shows the **key** and the **secret** once. Copy both somewhere
   safe now: you can't see the secret again later.

Good to know: anything Home Assistant does in UteamUP is recorded under the name of the person who
created the key.

### Step 2: Install UteamUP in Home Assistant

1. In Home Assistant, open **HACS** (or **Settings → Marketplace**).
2. Open the menu (⋮ in the top right) and choose **Custom repositories**.
3. Paste `https://github.com/UteamUP/ha-uteamup`, choose the type **Integration**, and click **Add**.
4. Search for **UteamUP**, open it and click **Download**.
5. Restart Home Assistant when it asks you to (**Settings → System → Restart**).

### Step 3: Connect it to your UteamUP company

1. Go to **Settings → Devices & services** and click **Add integration** (bottom right).
2. Search for **UteamUP** and select it.
3. Fill in the form:
   - **API address**: leave it as `https://api.uteamup.com`.
   - **API key** and **API secret**: paste the two values from Step 1.
   - **Verify the SSL certificate**: leave it ticked.
4. Click **Submit**. Home Assistant checks the key and adds your company.

### Step 4: Use it

You now have a **UteamUP** device with:

- **Counts** of open, in-progress, on-hold and overdue work orders, stock alerts and IoT alerts.
  Put them on a dashboard or use them in automations.
- A **Work orders** calendar.
- An **Open work orders** to-do list. Tick an item to complete that work order in UteamUP, or add
  an item to create a new work order.
- **Actions** for automations, for example *UteamUP: Report a problem* when a leak sensor goes off
  (see [Examples](#examples)).

The counts refresh every two minutes. Instant updates ("live events") need the Enterprise plan
and Home Assistant Cloud or another public address for Home Assistant; see [Live events](#live-events).

### If something doesn't work

| What you see | What to do |
|--------------|------------|
| "UteamUP rejected the API key or secret" | Copy both values again; the secret is easy to cut short. If it's lost, create a new key. |
| "This key is MCP-enabled" | Create a new key from the **API Keys** tab, not **MCP Keys**. |
| "The company's UteamUP plan is not active yet" | Your plan must be Premium or Enterprise and active. |
| Some sensors show **Unavailable** | The key's role (or the person who made the key) lacks a permission. See [the permission list](#what-the-key-may-do). |
| A notice about live events under **Settings → Repairs** | Live events are optional. Everything else keeps working and refreshes every two minutes. |
| Home Assistant asks you to sign in to UteamUP again | The key was switched off, rotated or has expired. Create a new key and enter it there. |

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

## What the key may do

The technical details behind Step 1, for administrators.

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
