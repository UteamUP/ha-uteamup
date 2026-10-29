# UteamUP for Home Assistant

Bring your UteamUP maintenance work into Home Assistant. See open and overdue work orders, stock
and IoT alerts. Report problems, complete work, set asset states and push sensor readings into
UteamUP meters from automations.

## Quick setup in plain English

This takes about ten minutes. You need two things:

- A UteamUP company on the **Premium** or **Enterprise** plan, and permission to manage it.
- Home Assistant, version **2025.11.3 or newer**, with **HACS** installed (from Home Assistant
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
| Actions | `uteamup.create_work_order`, `uteamup.create_quick_report`, `uteamup.set_work_order_status`, `uteamup.complete_work_order`, `uteamup.set_asset_state`, `uteamup.run_pm_schedule`, `uteamup.record_meter_reading`, `uteamup.append_journal_entry` |
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
   | Append journal entries | `Journal.View` and `Journal.Update` |
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
- **Send energy readings to dedicated IoT**: optional, reviewed per-device delivery described below.

## Deco clients and network meters (optional)

Use an existing healthy dedicated IoT environment and a Deco router already linked to an asset
and its MAC in UteamUP. The `tplink_deco` integration must expose the router tracker, download and
upload sensors (kB/s), client count, internet status and router status. In **UteamUP → Configure**,
enable telemetry, retain the existing energy selections (or select none for a network-only setup),
then select the Deco gateway and review its five sources, matching meters and device ingestion key.
The selected node's client count remains a node count; it is not relabelled as the whole network.

The gateway also sends a bounded inventory of client trackers in the same Deco integration:
name, MAC, local IP, connection state, reported download/upload rate and connected Deco node.
In **UteamUP → IoT → Monitoring**, search this inventory by name, MAC or local IP. These are reported
rates, not packet inspection or guaranteed WAN-only traffic. DNS queries, destination IP/port flows,
traffic history and per-client cumulative byte counters are not provided by this HA integration.
DNS/flow analysis requires another source, such as a DNS resolver's query log or a gateway flow exporter.

Optionally select clients to link to existing assets. Only exact MAC matches are suggested;
ambiguous matches require an explicit choice. Review every asset, then confirm. Confirmation creates
or reuses the tenant custom type **Deco client telemetry**, adds missing download/upload (kB/s) and
connected (0/1) meters, and attaches it as an additional asset type. Existing primary/additional
types and energy meters are retained. The key and its creator need **Asset.View**, **Asset.Update**,
**AssetType.View** and, when definitions must be created or extended, **AssetType.Create** and
**AssetType.Update**. The server enforces these permissions; the integration does not grant them.
Disabled or incompatible existing meters require review instead of being silently changed.

An interrupted confirmation can be retried: completed type/asset changes are reused. Unselecting
a client stops its network meter delivery; existing type assignments and historical readings remain.
Client readings use the gateway's ingestion identity, so existing WiZ IoT devices and keys are not
duplicated. A client's connectivity meter does not change its asset's operational state.

Sources keep their HA observation timestamps. Unknown, restored or more than ten-minute-old
observations never become zero readings. A fresh disconnected client records connected = 0 and
has no current rate/IP. A connected client with a measured zero rate retains that zero. The sender
runs every 60 seconds; this cannot make Deco's own polling faster. Older queued captures preserve
their original times. Monitoring requires the matching UteamUP API and ingestion-service update.

## Dedicated energy readings (optional)

This sender uses an existing dedicated UteamUP IoT environment. Enabling it does not purchase
anything or create cloud resources. A complimentary demo must already have its free entitlement
and a healthy dedicated endpoint in UteamUP; a grant alone does not establish ingestion readiness.
The sender requires Home Assistant **2025.11.3 or newer**. Work orders, calendars and the manual
meter action keep their existing behavior when the sender is disabled.

Prepare each physical light or socket before enabling delivery:

1. Link an existing active UteamUP IoT device to its asset and the device's actual MAC address.
2. Add active meter attributes for **power (W)** and **cumulative energy (kWh)** to that asset's
   effective type. Label estimated meters accordingly. Enable **Allow IoT updates** for those attributes.
   Do not use static rated power or a field describing monthly consumption for a cumulative counter.
3. Configure HA power/profile sources and a persistent cumulative energy helper. Estimated sources
   must expose `source_device_id` (the physical HA device UUID), `source_entity_registry_id`, or
   `source_entity` identifying that same light. Native Integral helpers may refer to their validated
   power source through `source`. Device UUID metadata survives entity renames.
4. Keep provisional assumptions visible and adjustable in HA. Set `estimate_profile` to a versioned
   name. When actual rated watts are unknown, label the profile provisional; never present it as a
   physical measurement. Verify that the energy helper excludes unavailable periods and Pi downtime.
5. Obtain a narrow, expiring ingestion key **for each represented device**. The ordinary company API
   key discovers configuration; it is never sent to the ingestion endpoint. Its role and creator
   need `IOT.Environment.Read`, `IOT.View`, `Asset.View` and `AssetType.View` for this setup.

For a WiZ socket with native power measurement, use its `switch` entity, its native WiZ W sensor
and a cumulative kWh sensor from that same device or an Integral helper whose `source` is that W
sensor. Choose **Measured by the WiZ socket** and leave the estimate profile empty. Estimated
helpers cannot be marked measured. No power or energy values are invented from the switch's state.

In **Settings → Devices & services → UteamUP → Configure**, enable **Send energy readings to
dedicated IoT**. Select the existing devices, then review each physical light or switch, W source,
cumulative kWh source, corresponding named meters, reading type and device ingestion key. Estimates
also require a profile version. Unique matching sources are preselected. Leave an existing key
blank to retain it. The final confirmation shows the discovered HTTPS endpoint and device count.
No traffic starts before that confirmation is saved.

Every 60 seconds, the sender observes available sources and durably stores one v1 envelope with
both readings and immutable event/point UUIDs. Native socket readings carry `quality: measured`
and `powerBasis: measured`; they do not include an estimate profile or assumed wattage. Estimated
readings retain `quality: estimated`, the power basis and profile version. Numeric
`assumed_full_power_w` and `assumed_standby_power_w` attributes
between 0 and 1,000 W are retained as estimate parameters with each observation; unrelated source
attributes are never copied. It does not calculate electricity cost or duplicate HA's energy integration.
Unavailable, unknown, restored, wrong-unit or cross-device sources produce no new reading or
heartbeat. Previously captured observations can still replay with their original timestamps while
a device is unavailable; replay does not mean that the physical device is currently online.

Delivery verifies TLS, refuses redirects and uses the device's own `X-Device-Key`. Only a matching
accepted/duplicate acknowledgement removes an envelope. Timeouts, server errors and throttling
back off with jitter and honor `Retry-After`. Rejected keys, endpoints or payloads quarantine the
affected event and stop new capture for that device; other devices continue.

The **Dedicated IoT delivery**, **Queued IoT observations**, **Quarantined IoT observations**,
**Available IoT device sources** and **Last acknowledged IoT delivery** diagnostic sensors separate
source availability from transport health. Queue attributes include bytes, retrying/blocked device
counts and `missed_samples`. Repairs explain storage, rejection and overflow conditions. Device
keys are recursively redacted from integration diagnostics and never stored in queue payloads.
HA stores the keys in its config entry like other integration credentials; protect its backups.

The restart-safe SQLite spool is `.storage/uteamup_telemetry_<entry-id>.db`, bounded to **50,000
envelopes and 64 MiB of payloads**. The physical database also contains indexes and transaction
overhead. At capacity, existing data is retained and new captures stop until space is available;
`missed_samples` records observations that were not captured. Quarantined events use the same
quota. Back up the spool before investigating it; do not delete it to clear an unexplained failure.

After fixing the cause, save a rotated per-device key to release authentication failures, or
select **Retry retained quarantined readings** in Configure. Retries keep the original data and
identities. Changing a physical mapping or target meter never rewrites older queued observations;
restore the reviewed mapping before retrying those observations. Old readings remain subject to
the dedicated environment's ingestion-retention policy.

A previously validated endpoint and mappings remain usable after a Pi restart during a company
API outage. CMMS entities stay unavailable and recover independently. This does not bypass an
expired/revoked device key or the dedicated endpoint's server-side authorization.

### Update, disable and recovery

For a published release, open **HACS → UteamUP → Redownload**, select the reviewed version and
restart Home Assistant. Keep the existing config entry; options, entity identities and queue
survive an update. Back up HA before replacing the integration, and never edit `.storage` while HA
is running.

A reviewed release archive contains `custom_components/uteamup/` at its root. For manual
installation, stop HA, replace that directory under `/config/custom_components/uteamup/` with the
reviewed archive's directory, then start HA. Do not copy a second nested `uteamup` directory. The
release must come from the reviewed committed revision, with its manifest version and checksum
recorded; an unpublished local change is not a HACS update.

To pause, clear **Send energy readings to dedicated IoT** and save the first options screen.
This requires no API connection, stops both background tasks, and retains mappings and queue.
Re-enable through the reviewed setup flow to resume. For immediate server-side revocation, revoke
the affected device ingestion keys in UteamUP; this does not erase meter history. Removing a bulb
from the selection preserves its queued data for review. Removing the integration also leaves its
spool for explicit backup/recovery; a new config entry does not automatically adopt that old spool.

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
