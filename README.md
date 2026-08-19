# EG4 6000XP Modbus Poller

Reads the inverter over RS485 Modbus RTU and publishes one JSON blob per
poll to MQTT. Read-only. Replaces Solar Assistant as the Modbus master.

## Before you start

**Solar Assistant must be stopped.** Two Modbus masters on one RS485 bus
collide. This is a cutover, not a parallel run.

```bash
sudo systemctl stop solar-assistant     # verify the actual unit name
```

## 1. Serial devices need no manual setup

`/dev/ttyUSB0` is not stable across reboots with multiple adapters, but
`/dev/serial/by-id/...` already is -- it's kernel-maintained, keyed to each
adapter's own USB serial number, no udev rule required. The container sees
every by-id path (see `## Stack`, below); the Config page in the web UI is
where you pick which one is which device. No `/etc/udev/rules.d/` file to
write.

Cheap CH340 adapters sometimes report no unique serial, in which case
by-id won't distinguish two of them -- if that happens, plug them in one
at a time and match by insertion order, or pin one by USB port path with a
udev rule as before (`KERNELS=="1-1.2"`).

## 2. Configure

```bash
cp .env.example .env    # then edit -- see Secrets, below
```

This covers infrastructure only -- MQTT/InfluxDB credentials, the deploy
target, the dialout GID. Devices, poll rates, and site coordinates are
configured through the web UI after the container is running (step 3),
not here.

Check the dialout GID on the host and match `DIALOUT_GID` in `.env`:

```bash
getent group dialout       # usually 20 on RPi OS
```

## 3. Run

```bash
docker compose up --build
```

Then open `http://<host>/config.html`, add your devices (name them,
pick each one's USB adapter from what's actually plugged in, set poll
rates), enter site coordinates, and Save -- the poller restarts itself to
pick up the new config. `solar_dash.html` and `solar_settings.html` are
linked from the same page once there's something to show.

## 4. Verify against Solar Assistant BEFORE removing it

Bring SA back up briefly, note its values, stop it, start the poller, compare.
Registers worth checking first:

| Field | Cross-check |
|---|---|
| `battery_voltage` | DMM at the pack terminals |
| `battery_soc` | BMS-reported SOC |
| `pv1_voltage` | DMM at the string |
| `pv1_power` | clamp amps x measured volts |
| `grid_voltage` | should read ~240 |
| `state` | inverter LCD |

`pv1_power` is the one under suspicion -- there is no PV *current* register
on this inverter, so SA's PV amps were computed as power / voltage. If
`pv1_power` reads high here too, the error is in the inverter's own sensor,
not in SA.

## Topic

`energy/inverter/state`

```json
{
  "device": "inverter",
  "tick_ts": 1754976000.0,
  "read_ts": 1754976000.184,
  "connected": true,
  "spans_ok": 10,
  "spans_total": 10,
  "values": { "pv1_voltage": 160.2, "battery_soc": 98, ... }
}
```

`tick_ts` is the scheduled sampling instant, shared across every device on
the same poll interval (see `poll_interval` on the Config page -- devices on
different intervals get their own independent tick). `read_ts` is when this
device's read finished. `app/derive.py`'s bank-join aligns packs on
`tick_ts`; use the delta between `tick_ts` and `read_ts` to see how far
apart the buses actually landed.

Key set is fixed. Failed reads publish `null`, never a missing key.

## Where each device's map lives

| Device | Map |
|---|---|
| EG4 6000XP | `config/eg4_6000xp_registers.yaml` |
| Eco-Worthy Cubix (JBD) | `app/jbd.py` -- `decode_basic` / `decode_cells` |
| EG4 LL-S | `app/eg4ll.py` -- `decode_block` |

Only the inverter's map is data. The battery maps need u32 spans, packed
sub-byte fields, checksummed frames and variable-length payloads that a flat
register table can't express, so they stay in code. That means a battery
scale change needs a rebuild and a version bump -- which is the right trade:
a firmware change should leave a changelog entry, not just a silent config edit.

The register filename is model-specific on purpose. Addresses and scales
differ across the EG4 line, and several scales here were corrected against
live readings after the upstream source had them wrong.

## Register map caveats

Derived from `adamksmith/ESPHome-Projects` `Patched-Parent-Inverter.yaml`.

* Registers 81, 82, 96, 97, 98, 103, 104 are **BMS-relayed** and reflect the
  comms-master pack ONLY -- not the bank. With the Cubix as master and the
  LL-S riding along uncommunicated, `bms_capacity` reads 100 Ah and
  `bms_packs_parallel` reads 1 even though the bank is 200 Ah across two packs.
  Any charge/discharge limit the inverter honours is therefore computed against
  half the real capacity. In full open loop these read zero or stale.
* Registers 10/11 (`battery_charge_w`/`battery_discharge_w`) are the
  inverter's own measurement -- these are what SA was using.
* `ac_couple_power` (input 153) and `set_ac_first_end_1` (holding 153) share
  an address in different register spaces. Marked unverified.
* Time-window registers assume hour in low byte, minute in high byte.
  Verify against the LCD before trusting.

## Writes

Writes are an inherent part of the poller, not an opt-in mode. There is no
config flag to arm the whole process -- safety is per-register instead: a
register is writable only if the map says so, and only within the map's
min/max, so what can be written and how far is fixed at the register-map
level rather than toggled at runtime. The settings page also has its own
client-side arm/disarm switch (with a 5-minute auto-disarm) as a UI-level
guard against a stray click, independent of this.

### Command interface

```
publish  energy/<device>/set
  {"key":"set_ac_charge_current","value":40,"dry_run":false,"request_id":"abc"}

result   energy/<device>/set/result
  {"ok":true,"key":"...","addr":168,"requested":40,"before":28,"after":40,
   "request_id":"abc","ts":...}
```

A result is published for every command, including rejections, so a UI never
has to guess whether a command was seen.

`dry_run` validates and encodes a value without touching the bus, so it can
be checked before it is sent for real.

### The guards, and why each exists

| guard | prevents |
|---|---|
| `writable: true` required in the register map | a caller naming an arbitrary address |
| min/max from the map, not the request | a value the caller says is fine |
| same `asyncio.Lock` as polling | a write interleaving with a read on the shared RS485 bus |
| read-back and compare | a silent no-op, where the inverter accepts the frame and ignores the value |
| arm lock (`app/armlock.py`) | two browser tabs (or two people) both being able to send commands at once |

The arm lock is real server-side mutual exclusion, not just a UI toggle:
`solar_settings.html` generates a random session ID per page load and
claims the lock via `POST /api/arm` before it will let you Save; the
command handler in `Runner._commands` checks that ID against the current
holder before executing anything that isn't a dry run, so a second tab
gets rejected even if it also has the Arm button showing armed locally.
The lock auto-expires after 5 minutes (in-memory, resets on a poller
restart too), so a crashed or closed tab can't lock everyone else out.

Ranges are deliberately tighter than the inverter's own. It will accept a
discharge floor of 10%; the map's minimum is 15, so a typo of `1` cannot
reach the bus.

### Writable registers

| addr | key | range |
|---|---|---|
| 105 | `set_eod_pct` | 15-90 % |
| 160 | `set_ac_charge_start_soc` | 5-95 % |
| 161 | `set_ac_charge_stop_soc` | 20-100 % |
| 168 | `set_ac_charge_current` | 0-125 A |
| 196 | `set_gen_start_soc` | 5-95 % |
| 197 | `set_gen_stop_soc` | 20-100 % |
| 68-73 | AC charge windows 1-3 | HH:MM |

**Not writable:** `set_ac_charge_limit_pct` (addr 67) reads 0 while Solar
Assistant shows a real value, so the address is unconfirmed -- writing to an
address you cannot confirm is exactly how you change a setting you did not
intend to. The AC First windows (152-157) are unverified for the same reason.

### First write

Do it on `set_ac_charge_current`: it is bounded, its effect is visible
immediately, and a wrong value is recoverable. Set it to a value you can
recognise, then check the inverter LCD before trusting anything else. Modbus
convention says FC6 writes the register FC3 reads, but convention is not
proof, and the read-back only tells you the register changed -- not that it is
the register you meant.

## Stack

Everything runs from one `docker-compose.yml`, on one host, driven entirely
through the web UI -- no Node-RED, no hand-edited device list.

| service     | what it does                                                      |
|-------------|--------------------------------------------------------------------|
| `eg4poll`   | reads the devices; computes derived values (PV correction, energy integration, bank aggregation -- `app/derive.py`) and the solar forecast (`app/forecast.py`) in-process; publishes raw + derived state to MQTT; writes to InfluxDB directly; serves the Config API |
| `mosquitto` | the broker. 1883 published to the LAN (for anything you wire up yourself externally), websockets internal-only |
| `influxdb`  | local store for the dashboard -- 6-month retention, not a permanent record. Self-provisions its own org/bucket/username/password/admin token on first boot (`influx/entrypoint.sh`); nobody types any of them |
| `nginx`     | serves `dashboard/` (including `config.html`), proxies `/mqtt`, `/influx`, and `/api` so the browser only needs one host |

This image deliberately does not do Home Assistant discovery or export to
any external/permanent store -- see `dashboard/config.html`'s "MQTT topics"
panel. The broker grants anonymous clients read-only access to `energy/#`
(`mosquitto/acl`'s rule with no `user` line -- writing anything still needs
a real account) and is exposed on the LAN specifically so you can build
that yourself, separately, with no credentials to configure on that end,
if you want it (the way HA or a NAS recording pipeline would subscribe in).

Devices are not declared in `docker-compose.yml` -- the container can see
any USB-serial adapter (`device_cgroup_rules`, scoped to the ttyUSB/ttyACM
device classes, not `privileged: true`), and the Config page
(`dashboard/config.html` + `app/devconfig.py` + `app/webapp.py`) is where
you assign one to each device slot. That assignment, plus poll rates and
site coordinates, is runtime state on a Docker volume, not a tracked file
-- `config/config.example.yaml` is the tracked template it seeds from on
first boot. Saving a change restarts the poller to apply it; a broken
config can't lock you out of the page that would let you fix it (see the
comments in `app/webapp.py` for how).

`mosquitto/` and `influx/` each hold a config file plus a small init script
that provisions a secret Docker itself can't be handed directly -- see the
comments in each for why.

## Layout

```
eg4_poller/
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env                     # secrets -- gitignored, NOT in the image
├── .env.example
├── app/                     # code -- baked into the image
│   ├── poller.py            # tick loop, MQTT, device registry, Runner
│   ├── jbd.py                # JBD driver (Cubix)
│   ├── eg4ll.py               # EG4 LL-S driver
│   ├── derive.py              # PV correction, energy integration, bank aggregation
│   ├── forecast.py            # Open-Meteo solar forecast
│   ├── influx_write.py        # line-protocol writer
│   ├── devconfig.py           # device/site config: schema, load/save, USB discovery
│   └── webapp.py               # Config API (device discovery, config read/write)
├── config/                  # bind-mounted to /config
│   ├── config.example.yaml         # TEMPLATE -- seeds the writable instance on first boot
│   └── eg4_6000xp_registers.yaml   # also ships in the image as a fallback
├── dashboard/                # solar_dash.html, solar_settings.html, config.html
├── mosquitto/                 # broker config + ACL (no secrets -- passwd is generated)
├── influx/                     # read-only-token provisioning script
├── nginx/                       # reverse proxy config
└── tools/                        # validate.py, check_secrets.py -- run by deploy.sh
```

Code and config are deliberately separate: `app/` changes mean a rebuild,
device/site config changes happen through the web UI and mean a restart.

## Secrets

`SETTINGS_MQTT_PASS` is the only real credential left in `./.env`, which
compose loads via `env_file`. It is excluded from the image by
`.dockerignore` and from git by `.gitignore`.

```bash
cp .env.example .env    # then edit -- see that file for what each var seeds
```

The `poller` MQTT account and InfluxDB both have no `.env` entries at all --
their usernames, passwords, org, bucket, and (for InfluxDB) admin token are
all generated inside their own containers on first boot
(`mosquitto/init-passwd.sh`, `influx/entrypoint.sh`) and never typed by
anyone. Neither is exposed to a browser, so there's nothing a human would
ever need to see or rotate by hand; each secret leaves its generating
container only over a Docker volume (`mqtt_shared`, `influx_shared`) rather
than an environment variable, read back out by `app/poller.py`'s
`_read_secret_file()`, and only by the container(s) that actually need to
authenticate:
- `eg4poll` reads the poller password to connect to `mosquitto` as itself,
  and the Influx admin token to write derived values and forecast data.
- `influx-init` reads the same Influx admin token once, to mint a separate
  bucket-scoped *read-only* token for `nginx.conf` to inject into dashboard
  queries (`influx/init-read-token.sh`).

InfluxDB's admin *password* (as opposed to its token) is used once, by
InfluxDB's own setup step, and then thrown away -- it's never written
anywhere, so nobody (including this stack) can log into Influx's own UI
with it.

Nothing credential-bearing is baked into an image or committed.

## Register spaces are independent

Modbus keeps input registers and holding registers in **separate address
spaces**. The same number means different things in each:

| addr | input | holding |
|---|---|---|
| 12 | `grid_voltage` | `clk_month_year` |
| 13 | — | `clk_hour_day` |
| 14 | — | `clk_sec_min` |

`read_input_registers` and `read_holding_registers` are different function
codes, so there is no ambiguity on the wire -- but it is easy to put an entry
in the wrong list, and the symptom is subtle: the read succeeds and one key
silently reports the other register's value.

The poller now refuses to start on a duplicate address within one space.
Sharing an address is legitimate **only** for packed sub-fields distinguished
by `bitmask`/`shift` -- input register 5 carries SOC in the low byte and SOH
in the high byte. Anything else aborts with the offending keys named.

## Naming and tags

`name` is the MQTT topic segment (`energy/<name>/state`) and must be unique.
The poller **refuses to start** on a duplicate rather than let two devices
silently overwrite each other's topic.

Use a numbered scheme so a second pack of the same model slots in cleanly:

```
cubix_1, cubix_2, lls_1, lls_2
```

Every envelope carries a `tags` object built from the device's config.
`model`, `role`, and `location` are promoted automatically; anything under an
explicit `tags:` mapping is passed through as well.

```yaml
- name: cubix_1
  type: jbd
  model: Eco-Worthy Cubix 100
  role: battery
  location: garage
  tags:
    chemistry: LiFePO4
    capacity_ah: 100
    installed: "2026-08"
```

publishes:

```json
"tags": {
  "device": "cubix_1", "model": "Eco-Worthy Cubix 100",
  "role": "battery", "location": "garage",
  "chemistry": "LiFePO4", "capacity_ah": 100, "installed": "2026-08"
}
```

`app/derive.py` maps `tags` straight onto InfluxDB tags. **Query by `role`,
not by `name`** -- a dashboard filtered on `role=battery` keeps working when
you add a third pack, whereas one listing `cubix_1` and `lls_1` by name does
not.

Adding a pack is a Config-page edit -- no docker-compose.yml change, no code
change: plug in the adapter, add a device on the Config page, pick it from
the discovered list, Save.

## Adding a pack

Most of the stack auto-detects. Verified with three packs and no code
change: `derive.bank_join` finds batteries by `role`, weights bank SOC by
capacity, and generates a `share_<device>_pct` field per pack. The dashboard
renders a card for any `energy/derived/<name>` that is not `inverter_1`,
`bank`, or `forecast`.

The one thing that's still a code edit, not a Config-page one:

| file | change |
|---|---|
| `dashboard/solar_dash.html` | one line in `MODELS` (cosmetic -- the model string shown on the pack's card) |

### Each JBD pack needs its OWN adapter

JBD is not an addressable protocol -- there is no slave ID, so two JBD packs
cannot share an RS485 bus. This is unlike the LL-S, which is Modbus and does
have an address. Three packs, two of them JBD, means three USB adapters.

### Consider CAN-chaining the two Cubix packs

The pair are the same brand, so they can link over their inter-battery CAN
chain and present to the inverter as one aggregated unit -- set the DIP
switch addresses per the Eco-Worthy manual.

That matters more than it sounds. `bms_capacity` currently reads **100 Ah**
while the bank is 200, and the comms master computes its charge taper against
that wrong denominator -- the whole reason both packs used to stall short of
full. Chaining the Cubix pair makes the inverter see 200 of 300 Ah instead of
100 of 300. Still wrong, but a third of the error rather than two thirds.

### Electrical

* Match resting voltage across all three before closing the breaker -- within
  ~50 mV, measured with the same meter.
* Own breaker or Class T fuse, equal-length cables to the busbar.
* 300 Ah at a 20% floor is 60 Ah of reserve, up from 40. If that floor was
  sized for backup hours rather than cycle life, it can come down.
* Charge current: three 100 A BMSes can take 300 A, but the inverter caps at
  125. Not a constraint, just no longer the limiting factor.
* **800 W into 300 Ah is 0.037C.** It was already marginal at 200 Ah. Solar
  alone will not routinely bring this bank to full, which makes the periodic
  grid-charge-to-100% that resyncs the counters more important, not less.

## Changelog

### 1.0.1
* **Fixed the inverter clock drift/sync comparing local time against UTC
  as if they were the same clock.** The clock registers carry no zone --
  the LCD is set to site-local time -- but `datetime.now()` with no
  argument returns the CONTAINER's clock, which is UTC in this image (no
  `TZ` set). Comparing the two naively meant "drift" was always just the
  site's UTC offset (a Pacific site read exactly -420 minutes, regardless
  of the inverter's actual clock), and, worse, `sync_time()` (the "Sync to
  now" button) wrote the container's UTC time directly into the inverter's
  real registers -- had it ever been used, it would have silently set the
  inverter's clock 7 hours off, which is what AC charge windows are
  scheduled against. Both now use the site's configured tz
  (`app/poller.py`'s `InverterDevice` takes it from `site_cfg`) via
  `zoneinfo.ZoneInfo`, same fix pattern already used in `app/forecast.py`
  for the equivalent Open-Meteo timestamp bug. Verified live: drift
  dropped from -420min to a real -9s RTC drift, and `sync_time()` was
  proven correct against the site's actual local clock before the fix
  reached the Pi.
* **Fixed a broker ACL gap that silently blocked both real writes and the
  settings page's display.** `mosquitto/acl`'s global (no-`user`) read
  rule only applies to truly anonymous clients -- verified live (mosquitto
  2.1.2): authenticating with a username drops a client out of it
  entirely, whether or not that username has its own `user` block. The
  0.10.0 MQTT-account-split changelog entry assumed it was additive to
  every client; it isn't. In practice this meant `poller` (authenticated)
  could no longer read `energy/+/set`, so real inverter write commands
  were silently never received, and `settings` (also authenticated)
  couldn't read `energy/derived/+` or `energy/+/set/result`, so
  `solar_settings.html` connected and looked "live" but never displayed a
  value or a write confirmation. Both accounts now grant themselves every
  topic they read explicitly rather than relying on the global rule.
* **Fixed a secure-context crash that left the settings page blank.**
  `crypto.randomUUID()` (used for the arm-lock's per-page-load session ID)
  only exists in secure contexts -- HTTPS or `localhost` -- and this stack
  is served over plain HTTP, so it was `undefined` and threw at the top of
  the script, before `build()` or `connect()` ever ran. Found by loading
  the actual page in a headless browser and reading the console, not by
  code inspection. Fixed with a fallback that builds a v4-shaped UUID from
  `crypto.getRandomValues()`, which has no such restriction.
* **Fixed a startup-time crash loop that silently blocked all polling.**
  `_forecast_loop()` returned immediately (no exception) whenever site
  lat/lon weren't set -- and `Runner.run()` races every task against
  shutdown with `asyncio.wait(..., FIRST_COMPLETED)`, treating whichever
  task finishes first as "the MQTT connection broke, reconnect." A disabled
  forecast finishing early tore down the whole session -- including the
  real device-polling and command tasks -- and immediately reconnected,
  forever, at roughly 100 times a second, with device polling never
  actually running. Fixed by having that path wait on shutdown instead of
  returning, like every other long-running task. Found on first real
  deploy: `energy/+/state` had nothing on it despite the container
  appearing to run.
* **Fixed two cross-device field-type mismatches** between `app/jbd.py`
  (Cubix packs) and `app/eg4ll.py` (LL-S): `remaining_ah` and
  `temperatures_c` were raw ints from the LL-S but floats from Cubix.
  Both feed InfluxDB's shared `battery` measurement, where a field's type
  locks to whichever value lands first -- so one driver's writes for that
  field always got silently rejected, depending on poll order. Both now
  cast to float in `eg4ll.py`, matching `jbd.py` and `nominal_ah`'s
  existing convention. Swept all 9 fields the two drivers share to confirm
  nothing else has the same latent mismatch.
* **Fixed a container-permissions bug that crashed `eg4poll` on any fresh
  volume.** The image runs as non-root (`poller`, uid 1000), but nothing
  created `/data` (the device/site config volume) with that ownership, so
  a fresh `eg4poll_config` volume came up `root`-owned and unwritable.
  `Dockerfile` now creates it with the right ownership before `USER
  poller`, so Docker seeds any new volume mounted there correctly.
  Same root cause, quieter failure mode, in `mosquitto/init-passwd.sh` and
  `influx/entrypoint.sh`: both wrote their shared secret files (the
  poller's MQTT password, the Influx admin token) as `root`-owned `600`,
  which `eg4poll` couldn't read either -- it just silently disabled MQTT
  and Influx writes rather than crashing. Both now `chown 1000:1000`
  before the existing `chmod 600`.

### 1.0.0
* **The poller MQTT account is now fully internal.** `MQTT_USER`/`MQTT_PASS`
  are gone from `.env` -- `mosquitto/init-passwd.sh` generates a random
  password for the `poller` account on first boot and shares it with
  `eg4poll` only via a Docker volume (`mqtt_shared`), the same pattern
  `influx/entrypoint.sh` already used for the InfluxDB admin token. There
  was never a reason for this one to be user-set: it's pure
  container-to-container auth, never exposed to a browser, so `.env` now
  holds exactly one real MQTT credential -- `SETTINGS_MQTT_PASS`, the only
  account a browser ever authenticates as.
* **MQTT read/write accounts split.** `solar_dash.html` and `config.html`
  are pure-read and now connect with no MQTT account at all -- the existing
  global anonymous-read rule already covered them, so the shared
  `dashboard` account's password was doing nothing for those two pages.
  Only `solar_settings.html`, which publishes inverter commands, still
  authenticates -- to a renamed `settings` account (`SETTINGS_MQTT_PASS`,
  replacing `DASHBOARD_MQTT_PASS`). That password is deliberately still not
  meant to be a strong secret: its job is to stop an accidental or scripted
  publish from the anonymous path from ever being mistaken for a real
  command, not to resist a determined LAN attacker -- the ACL is what
  actually scopes it.
* **InfluxDB fully self-provisions** (`influx/entrypoint.sh`). Username,
  password, org, bucket, and admin token used to come from
  `DOCKER_INFLUXDB_INIT_*` values in `.env`; now `influxdb` generates all of
  them itself on first boot, and none are typed by anyone. Only the admin
  token leaves the container, over a Docker volume (`influx_shared`), never
  as an environment variable -- `eg4poll` reads it via
  `app/poller.py`'s `_read_secret_file()`, and `influx-init` uses it once to
  mint the dashboard's separate read-only token, same as before. The admin
  password is generated, used once by Influx's own setup, and then never
  written anywhere -- not even this stack can log into Influx's own UI with
  it. Verified live: builds and self-provisions with zero Influx-related
  values in `.env`, the generated token authenticates and writes/queries
  correctly, and a container restart does not regenerate or invalidate it.
* **Real server-side arm lock** (`app/armlock.py`). "Arm" on the settings
  page used to be client-side JS state only -- nothing stopped a second
  browser tab from also arming and sending commands. Now `POST /api/arm`
  claims an in-memory lock keyed by a random per-page-load session ID, and
  `Runner._commands` checks that ID against the current holder before
  executing any non-dry-run write. 5-minute auto-expiry, same as the UI
  already had; dry-run validation stays exempt, since it never touches the
  bus.
* MQTT reads no longer need any account at all: `allow_anonymous true` plus
  a global (no-`user`) `topic read energy/#` rule in `mosquitto/acl`. The
  `external` account is gone -- whatever you wire up yourself (another
  Node-RED instance, HA, a NAS pipeline) just subscribes with no
  credentials. Writes still require a real, authenticated account
  (`poller`, `dashboard`) -- verified live that an anonymous publish to a
  command topic, and an anonymous publish impersonating the poller's own
  state topic, are both silently dropped.
* `tools/validate.py` now hard-fails if `config/config.example.yaml` (the
  tracked template) ever contains a real device or real site coordinates --
  a device's `port` is a USB adapter's own serial number, and this is the
  one file in the repo that could carry one into a commit. The live config
  itself was never at risk of this: it lives only in a Docker volume,
  edited exclusively through the validated `/api/config` API, never on the
  host and never in git.
* `dashboard/config.js` (and the `config.example.js` template) removed
  entirely, replaced by `GET /api/site`. It was a second, hand-maintained
  copy of values that already lived somewhere else -- MQTT/Influx
  credentials in `.env`, timezone in `config.yaml` -- eg4poll now just
  serves its own environment and config back to the pages that need it.

### 0.9.0
* **The whole thing became one appliance-like web stack**, driven entirely
  through the browser: a new Config tab (`dashboard/config.html`) picks
  devices from whatever USB-serial adapters are actually plugged in
  (`device_cgroup_rules`, not a docker-compose `devices:` list -- see
  `## Stack`), sets per-device poll rates, and sets site coordinates for
  the forecast. mosquitto, InfluxDB, and nginx joined the compose stack as
  real, version-controlled services (`mosquitto/`, `influx/`, `nginx/`).
* **Node-RED came in, then came back out.** It was bundled briefly to run
  the derive/forecast logic, then dropped once its only remaining job
  (HA discovery and external-Influx export are explicitly out of scope for
  this image) was pure computation with nothing left to justify a second
  language runtime. That logic -- PV correction, energy integration, bank
  aggregation (`app/derive.py`), and the Open-Meteo forecast
  (`app/forecast.py`) -- was ported into Python instead, faithfully
  verified field-by-field against the live Node-RED flow's real output
  before it was removed. Porting it surfaced two real bugs in the original
  JS, both fixed rather than reproduced: a hardcoded `"inverter_1"` device
  name in the bank-join cross-check (now found by role, since device names
  are user-configurable), and a timezone bug in the forecast model (Open-
  Meteo's local-time timestamps were parsed as the CONTAINER's own system
  timezone, not the site's -- silently wrong by however many hours they
  differ, for every deploy that didn't explicitly set the container's `TZ`
  to match `SITE_TZ`, which was all of them).
* Device config moved from a hand-edited, git-tracked `config.yaml` to
  runtime state on a Docker volume, written by the web UI
  (`app/devconfig.py`); `config/config.example.yaml` is the tracked
  template it seeds from. A bad save is validated before it's ever written,
  and the Config page stays reachable even if the on-disk config is
  broken some other way -- see `app/webapp.py`.
* `allow_writes` removed. It was meant as a temporary flag while the write
  path was being verified; per-register `writable:`/min/max in the register
  map was always the actual gate on what could be written, so the flag was
  redundant once that verification was done.
* `deploy.sh` now stages files before validating (`check_secrets.py` reads
  `git ls-files`, which only sees tracked files -- validating before staging
  meant a file's first commit was also the one commit that skipped the
  scan) and no longer deploys the dashboard to a separate nginx host, since
  nginx is now part of the same stack.
* Fixed: `_commands()`'s command listener blocked on `async for msg in
  mqtt.messages`, which nothing about a stop request could ever interrupt
  -- `docker stop`/SIGTERM would hang until Docker's kill timeout forced
  it. `Runner.run()` now races every task against the stop signal and
  actually cancels them, rather than waiting for each to notice
  cooperatively.

### 0.8.1
* **The clock decode was never actually in the build.** The edit that added it
  was anchored on a comment removed back in 0.4.0, so it silently applied to
  nothing -- `registers.yaml` is bind-mounted and picked up the new registers
  immediately, but `poller.py` is baked into the image, so the raw `clk_*`
  words were published with nothing decoding them. Now present and verified
  against live register values.
* Drift is reported as `inverter_time - host`, so a positive figure means the
  inverter is ahead.

### 0.8.0
* **Fixed: the clock registers were in the wrong space.** 12/13/14 were added
  to the `input` list, where 12 is already `grid_voltage`. They are HOLDING
  registers -- the ESPHome reference writes them with FC6, which only
  addresses holding. Moved, and the collision is gone.
* **Startup check for duplicate addresses within a register space.** The
  poller now aborts, naming the offending keys, rather than starting with one
  key silently reporting another register's value. Packed sub-fields sharing
  an address are still allowed when distinguished by `bitmask`/`shift`
  (input register 5 = SOC low byte + SOH high byte).
* Holding is now 22 registers in 5 spans; a full poll is 11 transactions.

### 0.5.0
* **Per-device publish.** Each device now publishes the moment its own read
  completes, instead of every device waiting on the slowest. Observed: a
  Modbus timeout-and-retry pushed one inverter read to 3360 ms against a
  375 ms median, and that delayed the Cubix and LL-S -- both long since
  finished -- by 3.4 s. `tick_ts` still ties the samples together for the
  downstream join; only delivery is decoupled.
* Log line flags a read exceeding half the tick budget as `SLOW`.
* Default `poll_interval` 10 -> 4 s.

#### Measured poll budget at 4 s

| device | median | range | transactions |
|---|---|---|---|
| inverter_1 | 383 ms | 371-386 | 10 |
| cubix_1 | 262 ms | 261-264 | 2 |
| lls_1 | 124 ms | 122-127 | 1 |

About 10% of the tick, ~250 ms spread across all three. One outlier at
3360 ms (two 1500 ms Modbus timeouts plus the normal read) is 84% of a 4 s
budget and would overrun a 2 s one -- which is why 4 s rather than 2.

### 0.4.0
* Battery current offset characterised: the inverter's derived figure reads a
  flat ~1.12 A low across 12.5-48.1 A. Fixed offset, not gain. Left
  **uncorrected** -- the BMS shunts are ground truth for battery power.
* **Poller now emits RAW values only.** All derived arithmetic moved to
  Node-RED: `battery_net_w`, `pv_total_w`, the PV current correction,
  `power_w`, cell min/max/delta/avg, cell-sum cross-check, temp min/max.
  Protocol decoding stays in the drivers -- `state` from `state_raw`,
  `balancing_cells` from the balance bitfield, `faults` from the protection
  bitfield -- since that is parsing, not computation.
* `pv_correction` removed from `config.yaml`; the coefficients now live in
  the Node-RED transform.
* `nodered/` added: transform and bank-join function nodes plus notes.

**Breaking:** fields removed from MQTT payloads. If anything downstream reads
`battery_net_w`, `pv_total_w_corrected`, `power_w`, `cell_delta_mv`,
`cell_sum_v`, `temp_max_c` or `temp_min_c`, repoint it at Influx.

### 0.3.0
* Renamed `registers.yaml` -> `eg4_6000xp_registers.yaml` to make the model
  scope explicit.
* Renamed `inverter` -> `inverter_1`; device symlinks now `-1` suffixed.
  **Topics changed**: `energy/inverter_1/state`, `energy/cubix_1/state`,
  `energy/lls_1/state`. Update any Node-RED subscriptions.
* Secrets moved to `.env` via compose `env_file`.
* **EG4 LL-S driver** (`eg4ll.py`). Modbus RTU, map from `tuxntoast/eg4-ll`
  verified field-by-field against a live pack.
* Block read extended to 47 registers -- upstream stops at 39, but this LL-S
  has live data through register 46.
* **Device tags** and duplicate-name detection.
* `lls_scan.py` / `lls_find.py` -- register mapping tools.

#### LL-S findings
* Config block (addr 45) and hardware block (addr 105) read **all zeros** on
  the LL-S. Upstream targets LL v1/v2; that firmware populates them, this one
  doesn't. Reads succeed, so it's empty rather than unsupported.
* Registers 720/1340/1920 return 0xFFFF across full chunks -- unmapped space.
* Registers 40-43 read 0x0007, 0x0FFF, 0x07FF, 0x000F: all-ones masks with
  3/12/11/4 bits set. Likely capability/alarm-enable bitfields, which also
  suggests the warning and protection words carry 12 and 11 meaningful bits.
* Registers 45-46 are a u32 advancing ~1 Hz. Reads as a Unix timestamp about
  75 days behind wall clock -- an unsynced RTC. Useful as a liveness marker.
* Current is x0.01, not x0.1: x0.1 would give -224.7 A on a 100 A BMS.

### 0.2.0
* Split `config/` out of `app/`. `CONFIG` defaults to `/config/config.yaml`;
  `register_map` points at `/config/registers.yaml`.
* **JBD driver added** (`jbd.py`) for the Eco-Worthy Cubix over RS485.
  Set the pack's RS485-1 protocol to **HNJD** -- that exposes native JBD
  framing, not an inverter protocol.
* Device dispatch by `type:` in config (`modbus` | `jbd`).
* `jbdtest.py` -- standalone probe, run before enabling in the poller.

### JBD protocol reference (verified against a real Cubix frame)

```
Request:  DD A5 <cmd> 00 <chk_hi> <chk_lo> 77
Response: DD <cmd> <status> <len> <data...> <chk_hi> <chk_lo> 77
checksum = (0x10000 - (len + sum(data))) & 0xFFFF, big-endian
```

`DD A5 03 00 FF FD 77` = basic info, `DD A5 04 00 FF FC 77` = cell voltages.

**cmd 0x03 payload does NOT contain per-cell voltages.** The trailing bytes
are NTC temperature sensors in 0.1 Kelvin. Misreading them as millivolts
produces plausible-looking cell values that are actually temperatures --
an easy and costly mistake. Per-cell data requires cmd 0x04.

Payload offsets for 0x03:

| Offset | Field | Units |
|---|---|---|
| 0-1 | total voltage | 10 mV |
| 2-3 | current (signed, + = charge) | 10 mA |
| 4-5 | remaining capacity | 10 mAh |
| 6-7 | nominal capacity | 10 mAh |
| 8-9 | cycles | count |
| 10-11 | production date | packed: `y=(v>>9)+2000, m=(v>>5)&0xF, d=v&0x1F` |
| 12-15 | balance bits | bitfield, cells 1-32 |
| 16-17 | protection bits | bitfield |
| 18 | software version | byte |
| **19** | **SOC** | **%** |
| 20 | FET status | bit0 charge, bit1 discharge |
| 21 | cell count | count |
| 22 | NTC count | count |
| 23+ | NTC values | 0.1 K each, 2 bytes |

Note offset 18 is the software version and 19 is SOC -- transposing them
is a common error.

### 0.1.4
* **`bms_battery_current` scale corrected to x0.1** (was 0.01). Verified
  against the Eco-Worthy app: raw -190 -> -19.0 A while the app reported
  19.53 A. The upstream ESPHome config uses 0.01 for this register.
* SOC/SOH byte order **confirmed**: observed raw 25699 = 0x6463 -> SOC 99,
  SOH 100. No longer an assumption.

### 0.1.2
* **SOC fix.** Register 5 packs two values — observed raw `25700` = `0x6464`.
  Now decoded as SOC (low byte) and SOH (high byte) via `bitmask`/`shift`.
  Byte order confirmed in 0.1.4.
* **Cell temps.** Registers 103/104 now scaled ×0.1 (288 → 28.8 °C), matching
  the `multiply: 0.1` filter in the source ESPHome config.
* **PV current correction.** New derived fields. Coefficients live in
  `config.yaml` under `pv_correction`, not in code.
* `set_ac_charge_limit_pct` (holding 67) marked unverified — read 0 while SA
  showed a real value.

### New derived fields

| Field | Meaning |
|---|---|
| `battery_soh` | State of health, high byte of reg 5 |
| `pvN_current` | `power / voltage`, uncorrected |
| `pvN_current_corrected` | offset-corrected, clamped at 0 |
| `pvN_power_corrected` | `corrected_current * voltage` |
| `pv_total_w_corrected` | sum across strings |

### About the PV correction

This inverter has **no PV current register** — only voltage (1, 2) and power
(7, 8). Anything reporting PV amps is computing `power / voltage`, and that
carries a fixed zero-point offset.

Fitted against DC clamp readings, 11 points from 0.3–4.5 A:

```
corrected = 1.0328 * raw - 0.6537     R2 = 0.994, RMSE 0.107 A
```

Slope within 3% of unity, so this is essentially pure offset (~0.63 A) rather
than a gain error. Zero-crossing at raw = 0.633 A, which matches the observed
first-light reading. Error is proportionally severe at low light (~50%+ below
1 A) and modest at full sun (~12%).

Re-fit if the array changes. Recheck seasonally — Hall-effect offsets drift
with temperature.

**Use the BMS shunts for energy accounting.** This correction is for a
diagnostic channel; the packs measure their own current directly and those
readings were verified against a clamp meter.
