# EG4 6000XP Modbus Poller

Reads the inverter over RS485 Modbus RTU and publishes one JSON blob per
poll to MQTT. Read-only. Replaces Solar Assistant as the Modbus master.

## Before you start

**Solar Assistant must be stopped.** Two Modbus masters on one RS485 bus
collide. This is a cutover, not a parallel run.

```bash
sudo systemctl stop solar-assistant     # verify the actual unit name
```

## 1. Pin the serial device

With multiple USB adapters, `/dev/ttyUSB0` is not stable across reboots.

```bash
udevadm info -a -n /dev/ttyUSB0 | grep -E 'idVendor|idProduct|serial' | head -5
```

`/etc/udev/rules.d/99-rs485.rules`:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", ATTRS{serial}=="XXXX", SYMLINK+="rs485-inverter"
```

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
ls -l /dev/rs485-inverter
```

Cheap CH340 adapters often report no unique serial. If so, pin by USB port
path instead using `KERNELS=="1-1.2"`.

## 2. Configure

Edit `docker-compose.yml` for your MQTT host/credentials. Check the dialout
GID on the host and match `group_add`:

```bash
getent group dialout       # usually 20 on RPi OS
```

## 3. Run

```bash
docker compose up --build
```

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

`tick_ts` is the scheduled sampling instant, shared across all devices.
`read_ts` is when this device's read finished. Align on `tick_ts` in Node-RED;
use the delta to see how far apart the buses actually landed.

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

**Off by default.** `allow_writes: false` in config.yaml. A poller that can
only read cannot misconfigure an inverter, which is the right default for
something a compose file restarts unattended.

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

`dry_run` works even when `allow_writes` is false -- it validates and encodes
without touching the bus, so a value can be checked without the system being
armed.

### The guards, and why each exists

| guard | prevents |
|---|---|
| `writable: true` required in the register map | a caller naming an arbitrary address |
| min/max from the map, not the request | a value the caller says is fine |
| same `asyncio.Lock` as polling | a write interleaving with a read on the shared RS485 bus |
| read-back and compare | a silent no-op, where the inverter accepts the frame and ignores the value |
| `allow_writes` off by default | an unattended restart arming the system |

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

## Layout

```
eg4_6000xp/
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env                  # secrets -- gitignored, NOT in the image
├── .env.example
├── nodered/              # transform + bank-join function nodes
├── app/                  # code -- baked into the image
│   ├── poller.py         # tick loop, MQTT, Modbus device
│   ├── jbd.py            # JBD driver (Cubix)
│   └── jbdtest.py        # standalone probe, not imported at runtime
└── config/               # bind-mounted to /config
    ├── config.yaml                  # REQUIRED mount; excluded from the image
    └── eg4_6000xp_registers.yaml    # also ships in the image as a fallback
```

Code and config are deliberately separate: `app/` changes mean a rebuild,
`config/` changes mean a restart.

On the Pi you only need `docker-compose.yml` and `config/`.

## Secrets

MQTT credentials live in `./.env`, which compose loads via `env_file`. It is
excluded from the image by `.dockerignore` and from git by `.gitignore`.

```bash
cp .env.example .env    # then edit
```

`config.yaml` references them as `${MQTT_HOST}` etc; the poller substitutes
environment variables at load time. Nothing credential-bearing is baked into
the image or committed.

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

Map `tags` straight onto InfluxDB tags in Node-RED. **Query by `role`, not by
`name`** -- a dashboard filtered on `role=battery` keeps working when you add
a third pack, whereas one listing `cubix_1` and `lls_1` by name does not.

Adding a pack is then a config edit plus a compose device line. No code change.

## Adding a pack

Most of the stack auto-detects. Verified with three packs and no code change:
`02_bank_join` finds batteries by `role` in flow context, weights bank SOC by
capacity, and generates a `share_<device>_pct` field per pack. The dashboard
renders a card for any `energy/derived/<name>` that is not `inverter_1`,
`bank`, or `forecast`.

What does need editing:

| file | change |
|---|---|
| `config/config.yaml` | a device block |
| `docker-compose.yml` | the by-id device mapping |
| `nodered/03_ha_discovery.js` | one line in `DEVICES` |
| `dashboard/solar_dash.html` | one line in `MODELS` (cosmetic) |

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
