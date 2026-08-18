#!/usr/bin/env python3
"""
EG4 6000XP Modbus RTU poller -> MQTT.

Design notes:
  * One asyncio task per device, all fired from a shared tick so that
    multi-device sampling lines up as closely as the buses allow.
  * Each poll emits ONE JSON blob with a fixed key set. Fields that fail
    to read come back as null rather than being omitted, so downstream
    parsing never has to branch on presence.
  * Reads are batched into contiguous register spans to keep bus time low.
  * Read-only for now. Holding registers are read but never written;
    the write path is stubbed at the bottom.
"""

import asyncio
import glob
import hashlib
import json
from datetime import datetime
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import yaml
from eg4ll import Eg4LlDevice
from jbd import JbdDevice
from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException
import aiomqtt

import armlock
import derive
import devconfig
import forecast
import influx_write
import webapp

LOG = logging.getLogger("eg4poll")

# Stamped at build time by the Dockerfile. The recurring failure on this
# project has not been bad code, it has been not knowing WHICH code is
# running -- a bind-mounted config picks changes up instantly while the image
# does not, so the two drift and the symptom looks like a logic bug.
VERSION = os.environ.get("EG4POLL_VERSION", "dev")
GIT_SHA = os.environ.get("EG4POLL_SHA", "unknown")


# ----------------------------------------------------------------------------
# Register model
# ----------------------------------------------------------------------------

WORDS = {"u16": 1, "s16": 1, "u32": 2, "s32": 2, "time": 1}


@dataclass
class Reg:
    addr: int
    key: str
    type: str = "u16"
    scale: float = 1.0
    unit: str = ""
    verified: bool = True
    min: float | None = None
    max: float | None = None
    setting: int | None = None   # the number on the inverter's own LCD menu
    bitmask: int | None = None   # applied AFTER shift
    shift: int = 0               # right-shift before masking
    offset: float = 0.0          # added AFTER scaling
    writable: bool = False       # explicit opt-in; default read-only

    @property
    def words(self) -> int:
        return WORDS.get(self.type, 1)


@dataclass
class Span:
    """A contiguous run of registers read in one Modbus transaction."""
    start: int
    count: int
    regs: list[Reg] = field(default_factory=list)


def build_spans(regs: list[Reg], max_span: int = 100, max_gap: int = 8) -> list[Span]:
    """Group registers into contiguous reads.

    Bridging small gaps is cheaper than issuing another transaction --
    each extra request costs a full turnaround on the RS485 bus.
    """
    if not regs:
        return []
    ordered = sorted(regs, key=lambda r: r.addr)
    spans: list[Span] = []
    cur = Span(start=ordered[0].addr, count=ordered[0].words, regs=[ordered[0]])
    for r in ordered[1:]:
        end = cur.start + cur.count
        gap = r.addr - end
        new_count = (r.addr + r.words) - cur.start
        if gap <= max_gap and new_count <= max_span:
            cur.count = new_count
            cur.regs.append(r)
        else:
            spans.append(cur)
            cur = Span(start=r.addr, count=r.words, regs=[r])
    spans.append(cur)
    return spans


def decode(reg: Reg, words: list[int]) -> Any:
    """Turn raw register words into a scaled Python value."""
    if reg.type == "u16":
        raw = words[0]
    elif reg.type == "s16":
        raw = words[0] - 0x10000 if words[0] >= 0x8000 else words[0]
    elif reg.type in ("u32", "s32"):
        # low word first, matching the ESPHome modbus_controller convention
        raw = (words[1] << 16) | words[0]
        if reg.type == "s32" and raw >= 0x80000000:
            raw -= 0x100000000
    elif reg.type == "time":
        # hour in low byte, minute in high byte
        hour = words[0] & 0xFF
        minute = (words[0] >> 8) & 0xFF
        return f"{hour:02d}:{minute:02d}"
    else:
        raw = words[0]

    # Bit extraction: some registers pack two values into one word
    # (e.g. reg 5 = SOC in low byte, SOH in high byte).
    if reg.shift:
        raw >>= reg.shift
    if reg.bitmask is not None:
        raw &= reg.bitmask

    val = raw * reg.scale + reg.offset
    # avoid float dust like 53.900000000000006
    return round(val, 4) if isinstance(val, float) else val


def encode(reg: Reg, value) -> int:
    """Scaled value -> raw register word. Inverse of decode().

    Only single-word registers are writable. u32 spans would need FC16 and a
    read-modify-write to avoid clobbering the partner word; nothing writable
    needs it, so it is refused rather than half-implemented.
    """
    if reg.words != 1:
        raise ValueError(f"{reg.key}: multi-word writes are not supported")

    if reg.type == "time":
        # "HH:MM" -> hour in low byte, minute in high byte
        try:
            hh, mm = str(value).split(":")
            hh, mm = int(hh), int(mm)
        except Exception:
            raise ValueError(f"{reg.key}: expected HH:MM, got {value!r}")
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"{reg.key}: {hh:02d}:{mm:02d} is not a valid time")
        return ((mm & 0xFF) << 8) | (hh & 0xFF)

    v = float(value)
    if reg.min is not None and v < reg.min:
        raise ValueError(f"{reg.key}: {v} below minimum {reg.min}")
    if reg.max is not None and v > reg.max:
        raise ValueError(f"{reg.key}: {v} above maximum {reg.max}")

    raw = round((v - reg.offset) / (reg.scale or 1))
    if reg.bitmask is not None or reg.shift:
        raise ValueError(f"{reg.key}: packed registers are not writable")
    if reg.type == "s16":
        if not (-0x8000 <= raw <= 0x7FFF):
            raise ValueError(f"{reg.key}: {raw} out of s16 range")
        return raw & 0xFFFF
    if not (0 <= raw <= 0xFFFF):
        raise ValueError(f"{reg.key}: {raw} out of u16 range")
    return raw


# ----------------------------------------------------------------------------
# Device
# ----------------------------------------------------------------------------


class InverterDevice:
    def __init__(self, cfg: dict, regmap: dict):
        self.name: str = cfg["name"]
        self.port: str = cfg["port"]
        self.baud: int = cfg.get("baud", 19200)
        self.unit: int = cfg.get("unit_id", 1)
        self.timeout: float = cfg.get("timeout", 1.5)
        self.read_holding: bool = cfg.get("read_holding", True)

        self.input_regs = [Reg(**r) for r in regmap.get("input", [])]
        self.holding_regs = [Reg(**r) for r in regmap.get("holding", [])]
        self.state_map = {int(k): v for k, v in regmap.get("state_map", {}).items()}

        # Catch an address defined twice in the same space. Sharing an
        # address is legitimate ONLY for packed sub-fields, which are
        # distinguished by bitmask/shift -- reg 5 carries SOC in the low byte
        # and SOH in the high byte. Anything else is a copy-paste error, and
        # a silent one: the register reads fine and one of the two keys is
        # simply wrong. (This check would have caught the clock registers
        # landing in the input space, where 12 is already grid_voltage.)
        for space, regs in (("input", self.input_regs), ("holding", self.holding_regs)):
            by_addr: dict[int, list[Reg]] = {}
            for r in regs:
                by_addr.setdefault(r.addr, []).append(r)
            for addr, rs in by_addr.items():
                if len(rs) == 1:
                    continue
                sigs = {(r.bitmask, r.shift) for r in rs}
                if len(sigs) != len(rs):
                    raise SystemExit(
                        f"{self.name}: {space} register {addr} defined "
                        f"{len(rs)}x without distinct bitmask/shift: "
                        + ", ".join(r.key for r in rs))
                LOG.debug("%s: %s reg %d shared by packed fields: %s",
                          self.name, space, addr, [r.key for r in rs])

        self.input_spans = build_spans(self.input_regs)
        self.holding_spans = build_spans(self.holding_regs)

        self.client: AsyncModbusSerialClient | None = None
        self._lock = asyncio.Lock()  # serialises reads and (future) writes
        self._fail_streak = 0

        LOG.info(
            "%s: %d input regs in %d spans, %d holding regs in %d spans",
            self.name, len(self.input_regs), len(self.input_spans),
            len(self.holding_regs), len(self.holding_spans),
        )

    async def connect(self) -> bool:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = AsyncModbusSerialClient(
            port=self.port,
            baudrate=self.baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.timeout,
        )
        ok = await self.client.connect()
        if ok:
            LOG.info("%s: connected on %s @ %d", self.name, self.port, self.baud)
        else:
            LOG.warning("%s: connect failed on %s", self.name, self.port)
        return ok

    async def _read_span(self, span: Span, holding: bool) -> list[int] | None:
        try:
            if holding:
                rr = await self.client.read_holding_registers(
                    address=span.start, count=span.count, slave=self.unit
                )
            else:
                rr = await self.client.read_input_registers(
                    address=span.start, count=span.count, slave=self.unit
                )
            if rr.isError():
                LOG.debug("%s: span %d+%d error: %s", self.name, span.start, span.count, rr)
                return None
            return list(rr.registers)
        except ModbusException as e:
            LOG.debug("%s: span %d+%d modbus exc: %s", self.name, span.start, span.count, e)
            return None
        except Exception as e:
            LOG.debug("%s: span %d+%d exc: %s", self.name, span.start, span.count, e)
            return None

    async def poll(self, tick_ts: float) -> dict:
        """Read every configured register. Returns a fixed-shape dict."""
        # Fixed key set: start everything at None so the blob shape never varies.
        values: dict[str, Any] = {r.key: None for r in self.input_regs}
        if self.read_holding:
            values.update({r.key: None for r in self.holding_regs})

        ok_spans = 0
        total_spans = 0

        async with self._lock:
            if self.client is None or not self.client.connected:
                if not await self.connect():
                    return self._envelope(tick_ts, values, 0, 0, connected=False)

            jobs = [(s, False) for s in self.input_spans]
            if self.read_holding:
                jobs += [(s, True) for s in self.holding_spans]

            for span, is_holding in jobs:
                total_spans += 1
                words = await self._read_span(span, is_holding)
                if words is None:
                    continue
                ok_spans += 1
                for reg in span.regs:
                    off = reg.addr - span.start
                    if off + reg.words > len(words):
                        continue
                    try:
                        values[reg.key] = decode(reg, words[off:off + reg.words])
                    except Exception as e:
                        LOG.debug("%s: decode %s failed: %s", self.name, reg.key, e)

        if ok_spans == 0 and total_spans > 0:
            self._fail_streak += 1
            if self._fail_streak >= 3:
                LOG.warning("%s: %d consecutive failed polls, reconnecting",
                            self.name, self._fail_streak)
                self.client = None
                self._fail_streak = 0
        else:
            self._fail_streak = 0

        return self._envelope(tick_ts, values, ok_spans, total_spans, connected=True)

    def _envelope(self, tick_ts, values, ok_spans, total_spans, connected) -> dict:
        # `state` is protocol decoding (raw word -> label), not arithmetic,
        # so it stays here. All computed values -- net power, totals, unit
        # conversions, sensor corrections -- are deliberately NOT produced by
        # the poller. Raw at collection, derived in Node-RED.
        state_raw = values.get("state_raw")
        values["state"] = self.state_map.get(state_raw) if state_raw is not None else None

        # Inverter clock (LCD setting 1), assembled from three packed holding
        # registers. Decoding is protocol parsing, not arithmetic, so it
        # belongs here rather than in Node-RED.
        #
        #   12 = (month  << 8) | year_last_two
        #   13 = (hour   << 8) | day
        #   14 = (second << 8) | minute
        #
        # Drift is worth surfacing: the AC charge windows are scheduled
        # against THIS clock, not wall time, so a drifted or wrongly-zoned
        # inverter grid-charges at the wrong hour and nothing else reveals it.
        my, hd, sm = (values.get(k) for k in
                      ("clk_month_year", "clk_hour_day", "clk_sec_min"))
        values["inverter_time"] = None
        values["inverter_clock_drift_s"] = None
        if None not in (my, hd, sm):
            my, hd, sm = int(my), int(hd), int(sm)
            yr, mo = 2000 + (my & 0xFF), (my >> 8) & 0xFF
            day, hr = hd & 0xFF, (hd >> 8) & 0xFF
            mi, se = sm & 0xFF, (sm >> 8) & 0xFF
            try:
                dt = datetime(yr, mo, day, hr, mi, se)
                values["inverter_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                values["inverter_clock_drift_s"] = round(
                    (dt - datetime.now()).total_seconds(), 1)
            except ValueError:
                # An inverter whose clock was never set reports month 0 or
                # day 0. Leave both null rather than inventing a date.
                LOG.debug("%s: invalid clock %04d-%02d-%02d %02d:%02d:%02d",
                          self.name, yr, mo, day, hr, mi, se)

        return {
            "device": self.name,
            # tick_ts = when the sampling round was scheduled (for cross-device alignment)
            # read_ts = when this device's read actually finished
            "tick_ts": round(tick_ts, 3),
            "read_ts": round(time.time(), 3),
            "connected": connected,
            "spans_ok": ok_spans,
            "spans_total": total_spans,
            "values": values,
        }

    async def sync_time(self) -> dict:
        """Set the inverter clock from the host (LCD setting 1).

        Three packed registers written together. Exposed as one command
        rather than three writable fields: a half-applied clock (new hour,
        old date) is worse than a drifted one, and nothing sensible wants to
        set the minute without the hour.
        """
        now = datetime.now()
        words = {
            12: ((now.month & 0xFF) << 8) | (now.year % 100),
            13: ((now.hour & 0xFF) << 8) | (now.day & 0xFF),
            14: ((now.second & 0xFF) << 8) | (now.minute & 0xFF),
        }
        async with self._lock:
            if self.client is None or not self.client.connected:
                if not await self.connect():
                    return {"ok": False, "error": "not connected"}
            for addr, w in words.items():
                try:
                    wr = await self.client.write_register(
                        address=addr, value=w, slave=self.unit)
                    if wr.isError():
                        return {"ok": False,
                                "error": f"reg {addr} write error: {wr}"}
                except Exception as e:
                    return {"ok": False, "error": f"reg {addr} failed: {e}"}
                await asyncio.sleep(0.05)
        LOG.warning("WRITE %s: clock set to %s", self.name, now.isoformat(" ", "seconds"))
        return {"ok": True, "key": "sync_time",
                "set_to": now.strftime("%Y-%m-%d %H:%M:%S")}

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------
    async def write_holding(self, key: str, value, dry_run: bool = False) -> dict:
        """Validate, write, read back, verify. Returns a result dict.

        Every guard here is deliberate:
          * the register must be declared `writable: true` in the map -- the
            caller cannot name an arbitrary address
          * min/max come from the map, not the request
          * the write takes the same lock as polling, so it cannot interleave
            with a read on the shared RS485 bus
          * the value is read back and compared; a silent no-op (the inverter
            rejecting a value it does not like) is otherwise invisible
        """
        reg = next((r for r in self.holding_regs if r.key == key), None)
        if reg is None:
            return {"ok": False, "error": f"unknown register {key!r}"}
        if not reg.writable:
            return {"ok": False, "error": f"{key} is not writable"}

        try:
            raw = encode(reg, value)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if dry_run:
            return {"ok": True, "dry_run": True, "key": key,
                    "value": value, "raw": raw, "addr": reg.addr}

        async with self._lock:
            if self.client is None or not self.client.connected:
                if not await self.connect():
                    return {"ok": False, "error": "not connected"}

            before = None
            try:
                rr = await self.client.read_holding_registers(
                    address=reg.addr, count=1, slave=self.unit)
                if not rr.isError():
                    before = decode(reg, [rr.registers[0]])
            except Exception:
                pass   # not fatal; the read-back below is the real check

            try:
                wr = await self.client.write_register(
                    address=reg.addr, value=raw, slave=self.unit)
                if wr.isError():
                    return {"ok": False, "error": f"modbus write error: {wr}"}
            except Exception as e:
                return {"ok": False, "error": f"write failed: {e}"}

            await asyncio.sleep(0.15)   # let the inverter commit before re-reading

            try:
                rr = await self.client.read_holding_registers(
                    address=reg.addr, count=1, slave=self.unit)
                if rr.isError():
                    return {"ok": False, "error": "write sent but read-back failed",
                            "key": key, "requested": value}
                after = decode(reg, [rr.registers[0]])
            except Exception as e:
                return {"ok": False, "error": f"read-back failed: {e}",
                        "key": key, "requested": value}

        verified = (rr.registers[0] == raw)
        LOG.warning(
            "WRITE %s: %s -> %s (raw %s, addr %s) %s",
            self.name, before, after, raw, reg.addr,
            "VERIFIED" if verified else "MISMATCH",
        )
        return {
            "ok": verified, "key": key, "addr": reg.addr,
            "requested": value, "before": before, "after": after,
            "error": None if verified else
                     "value did not stick -- the inverter may have rejected it",
        }


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------


class Runner:
    def __init__(self, devices_cfg: list[dict], mqtt_cfg: dict, site_cfg: dict, influx_cfg: dict,
                 armlock_: armlock.ArmLock | None = None):
        self.mqtt_cfg = mqtt_cfg
        self.base_topic: str = mqtt_cfg.get("base_topic", "energy")
        self.site_cfg = site_cfg
        self.influx_cfg = influx_cfg
        # Shared with webapp.py's /api/arm -- same object, so a session that
        # armed through the HTTP API is exactly what gets checked here
        # before any real (non-dry-run) write is executed. See app/armlock.py.
        self.armlock = armlock_ if armlock_ is not None else armlock.ArmLock()
        self.devices: list[InverterDevice] = []
        # name -> tag dict, merged into every published envelope. Kept out of
        # the device classes so it applies uniformly to any driver type.
        self.tags: dict[str, dict] = {}
        # name -> poll interval. Devices sharing an interval share a tick
        # (see run()) so their tick_ts still lines up for derive.bank_join;
        # a device on its own interval just gets its own tick loop.
        self.intervals: dict[str, float] = {}
        self._stop = asyncio.Event()
        self.derive_state = derive.DeriveState()
        self._http: aiohttp.ClientSession | None = None  # set in run()

        seen_ports: dict[str, str] = {}
        for dev_cfg in devices_cfg:
            if not dev_cfg.get("enabled", True):
                LOG.info("skipping disabled device %s", dev_cfg.get("name"))
                continue
            name = dev_cfg.get("name")
            if not name:
                raise SystemExit("every device needs a 'name'")
            if name in self.tags:
                # Two devices with the same name would publish to the same
                # topic and silently overwrite each other.
                raise SystemExit(f"duplicate device name {name!r} -- names must be unique")
            port = dev_cfg.get("port")
            if port:
                # Used to be implicitly caught by docker-compose's explicit
                # per-device mapping; that mapping is gone now that the
                # container can see any attached USB-serial adapter (see
                # devconfig.py), so this has to be checked here instead.
                if port in seen_ports:
                    raise SystemExit(f"devices {seen_ports[port]!r} and {name!r} both claim port {port!r}")
                seen_ports[port] = name

            tags = dict(dev_cfg.get("tags", {}))
            # Promote a few common fields so they don't have to be duplicated
            # under tags:. Explicit tags win.
            for k in ("model", "role", "location"):
                if k in dev_cfg and k not in tags:
                    tags[k] = dev_cfg[k]
            tags.setdefault("device", name)
            self.tags[name] = tags
            self.intervals[name] = float(dev_cfg.get("poll_interval", devconfig.DEFAULT_POLL_INTERVAL))

            dtype = dev_cfg.get("type", "modbus")
            if dtype == "modbus":
                with open(dev_cfg["register_map"]) as f:
                    regmap = yaml.safe_load(f)
                self.devices.append(InverterDevice(dev_cfg, regmap))
            elif dtype == "jbd":
                self.devices.append(JbdDevice(dev_cfg))
            elif dtype == "eg4ll":
                self.devices.append(Eg4LlDevice(dev_cfg))
            else:
                raise SystemExit(f"unknown device type {dtype!r} for {dev_cfg.get('name')}")

        if not self.devices:
            raise SystemExit("no enabled devices in config")

        for n, t in self.tags.items():
            LOG.info("device %-12s tags=%s poll=%ss", n, t, self.intervals[n])

    def stop(self):
        self._stop.set()

    async def run(self):
        m = self.mqtt_cfg
        async with aiohttp.ClientSession() as http:
            self._http = http
            while not self._stop.is_set():
                try:
                    async with aiomqtt.Client(
                        hostname=m["host"],
                        port=m.get("port", 1883),
                        username=m.get("username") or None,
                        password=m.get("password") or None,
                        identifier=m.get("client_id", "eg4poll"),
                    ) as mqtt:
                        LOG.info("MQTT connected to %s:%s", m["host"], m.get("port", 1883))
                        # Every interval group gets its own tick loop, plus
                        # the command listener and the forecast timer, all
                        # concurrent: a command must not wait for the next
                        # poll, and a slow device on one interval must not
                        # delay a fast device on another.
                        groups: dict[float, list[InverterDevice]] = {}
                        for dev in self.devices:
                            groups.setdefault(self.intervals[dev.name], []).append(dev)
                        coros = [self._loop(mqtt, interval, devs) for interval, devs in groups.items()]
                        coros.append(self._commands(mqtt))
                        coros.append(self._forecast_loop(mqtt))
                        tasks = [asyncio.ensure_future(c) for c in coros]
                        # _commands blocks on `async for msg in mqtt.messages`,
                        # which nothing about self._stop being set can ever
                        # unblock on its own -- a plain gather() would hang
                        # past shutdown waiting for it. Race everything
                        # against the stop event instead, and actually cancel
                        # (not just wait for) whatever is still running.
                        stop_waiter = asyncio.ensure_future(self._stop.wait())
                        try:
                            done, _ = await asyncio.wait(
                                [stop_waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
                            if stop_waiter not in done:
                                for t in done:
                                    exc = t.exception()
                                    if exc:
                                        raise exc
                        finally:
                            stop_waiter.cancel()
                            for t in tasks:
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                    if self._stop.is_set():
                        break
                except Exception as e:
                    if self._stop.is_set():
                        break
                    LOG.warning("MQTT connection lost (%s); retrying in 5s", e)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass

    async def _commands(self, mqtt):
        """Listen on <base>/<device>/set and apply validated writes.

        Payload:
            {"key": "set_ac_charge_current", "value": 40, "dry_run": false,
             "session_id": "...", "request_id": "optional"}

        session_id must currently hold the arm lock (see app/armlock.py,
        POST /api/arm) for anything other than a dry run -- solar_settings.html
        arms through that HTTP endpoint before it ever publishes a real
        write, so only one browser tab, anywhere, can have live commands
        land at a time. dry_run is exempt: it never touches the bus, so
        there is nothing for a second concurrent session to conflict with.

        Result is published to <base>/<device>/set/result, always -- including
        on rejection, so a UI never has to guess whether a command was seen.
        """
        topic = f"{self.base_topic}/+/set"
        await mqtt.subscribe(topic)
        LOG.info("listening for commands on %s", topic)

        async for msg in mqtt.messages:
            parts = str(msg.topic).split("/")
            if len(parts) < 3 or parts[-1] != "set":
                continue
            name = parts[-2]
            dev = next((d for d in self.devices if d.name == name), None)

            try:
                cmd = json.loads(msg.payload.decode())
            except Exception as e:
                LOG.warning("bad command payload on %s: %s", msg.topic, e)
                continue

            rid = cmd.get("request_id")
            key = cmd.get("key")
            val = cmd.get("value")
            dry = bool(cmd.get("dry_run", False))
            session_id = cmd.get("session_id")

            async def reply(res):
                res = dict(res, request_id=rid, ts=round(time.time(), 3))
                await mqtt.publish(f"{self.base_topic}/{name}/set/result",
                                   json.dumps(res, separators=(",", ":")))

            if dev is None:
                await reply({"ok": False, "error": f"unknown device {name!r}"})
                continue
            if not hasattr(dev, "write_holding"):
                await reply({"ok": False, "error": f"{name} does not support writes"})
                continue
            if not dry and not self.armlock.check(session_id):
                await reply({"ok": False, "error": "not armed -- arm this session via POST /api/arm first"})
                continue

            LOG.warning("command %s %s=%r dry_run=%s rid=%s",
                        name, key, val, dry, rid)
            try:
                if key == "sync_time":
                    res = ({"ok": True, "dry_run": True, "key": "sync_time"}
                           if dry else await dev.sync_time())
                else:
                    res = await dev.write_holding(key, val, dry_run=dry)
            except Exception as e:
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            await reply(res)

    async def _loop(self, mqtt, interval: float, devices: list[InverterDevice]):
        # Align ticks to wall-clock multiples of the interval so samples land
        # on predictable boundaries -- makes cross-source comparison in Influx
        # much easier to reason about.
        while not self._stop.is_set():
            now = time.time()
            next_tick = (now // interval + 1) * interval
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=next_tick - now)
                break
            except asyncio.TimeoutError:
                pass

            tick_ts = next_tick

            # Poll every device on this interval concurrently off the same
            # tick, and publish each one the moment IT finishes rather than
            # waiting for the slowest. A single slow Modbus read (a
            # timeout-and-retry can take 3 s against a 375 ms median) would
            # otherwise stall MQTT for every device -- the fast packs would
            # sit finished-but-unpublished. tick_ts still ties the samples
            # together for the downstream join; only delivery is decoupled.
            async def poll_and_publish(dev):
                try:
                    res = await dev.poll(tick_ts)
                except Exception as e:
                    LOG.error("%s: poll raised %s", dev.name, e)
                    return

                res["tags"] = self.tags.get(dev.name, {})
                topic = f"{self.base_topic}/{dev.name}/state"
                try:
                    await mqtt.publish(
                        topic, json.dumps(res, separators=(",", ":")),
                        qos=0, retain=False,
                    )
                except Exception as e:
                    LOG.warning("publish failed for %s: %s", dev.name, e)
                    raise

                v = res["values"]
                soc = v.get("battery_soc", v.get("soc"))
                elapsed = (res["read_ts"] - res["tick_ts"]) * 1000
                extra = ""
                if v.get("pv1_power") is not None:
                    extra = (f" pv={v['pv1_power']}W"
                             f" chg={v.get('battery_charge_w')}W"
                             f" dis={v.get('battery_discharge_w')}W")
                elif v.get("voltage") is not None:
                    mv = v.get("cells_mv") or []
                    d = f" delta={max(mv) - min(mv)}mV" if mv else ""
                    extra = f" {v['voltage']}V {v.get('current')}A{d}"
                # Flag a read that ate a meaningful slice of the tick budget.
                slow = "  SLOW" if elapsed > interval * 1000 * 0.5 else ""
                LOG.info(
                    "%s: %d/%d ok, %.0fms, soc=%s%%%s%s",
                    dev.name, res["spans_ok"], res["spans_total"],
                    elapsed, soc, extra, slow,
                )

                await self._derive_and_publish(mqtt, res)

            results = await asyncio.gather(
                *(poll_and_publish(d) for d in devices),
                return_exceptions=True,
            )
            # A publish failure bubbles out so the outer loop reconnects MQTT.
            for r in results:
                if isinstance(r, Exception):
                    raise r

    async def _derive_and_publish(self, mqtt, envelope: dict):
        """PV correction, energy integration, cell math, bank aggregation --
        everything poller.py deliberately does not compute at the point of
        reading a register. See derive.py. Publishes energy/derived/<device>
        (and energy/derived/bank, if this poll completed a fresh pair of
        packs) and writes the same points to InfluxDB."""
        try:
            out = derive.transform(self.derive_state, envelope)
        except Exception as e:
            LOG.error("%s: derive failed: %s", envelope["device"], e)
            return
        if out is None:
            return

        derived_topic = f"{self.base_topic}/derived/{envelope['device']}"
        payload = dict(out["fields"], tick_ts=out["tick_ts"])
        payload.update(out["cell_fields"])
        try:
            await mqtt.publish(derived_topic, json.dumps(payload, separators=(",", ":")), qos=0, retain=False)
        except Exception as e:
            LOG.warning("derived publish failed for %s: %s", envelope["device"], e)

        points = [{"measurement": out["measurement"], "tags": out["tags"],
                   "fields": out["fields"], "timestamp": out["tick_ts"]}]
        # Per-cell voltages go to their own measurement -- the least likely
        # thing anyone wants at full resolution a year from now, so they can
        # be dropped independently without touching SOC/current/temp history.
        if out["cell_fields"]:
            points.append({"measurement": "battery_cells", "tags": out["tags"],
                            "fields": out["cell_fields"], "timestamp": out["tick_ts"]})
        await self._write_influx(points)

        bank = derive.bank_join(self.derive_state)
        if bank is not None:
            bank_topic = f"{self.base_topic}/derived/bank"
            bank_payload = dict(bank["fields"], tick_ts=bank["tick_ts"])
            try:
                await mqtt.publish(bank_topic, json.dumps(bank_payload, separators=(",", ":")), qos=0, retain=False)
            except Exception as e:
                LOG.warning("bank publish failed: %s", e)
            await self._write_influx([{"measurement": "bank", "tags": bank["tags"],
                                        "fields": bank["fields"], "timestamp": bank["tick_ts"]}])

    async def _write_influx(self, points: list[dict]):
        if not self.influx_cfg.get("token"):
            return  # not configured -- MQTT publish still happened regardless
        try:
            await influx_write.write_points(
                self._http, self.influx_cfg["url"], self.influx_cfg["token"],
                self.influx_cfg["org"], self.influx_cfg["bucket"], points,
            )
        except Exception as e:
            # Influx is history; MQTT is the live path. A write failure here
            # should never take the poll/publish loop down with it.
            LOG.warning("influx write failed: %s", e)

    async def _forecast_loop(self, mqtt):
        lat, lon = self.site_cfg.get("lat"), self.site_cfg.get("lon")
        tz = self.site_cfg.get("tz") or "UTC"
        if lat is None or lon is None:
            LOG.warning("forecast disabled -- set site lat/lon on the Config page")
            return

        # Matches the original inject timing: a short startup delay, then
        # hourly.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=20)
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                raw = await forecast.fetch_raw(self._http, lat, lon, tz)
                result = forecast.model(raw, tz)
                f = result["fields"]

                topic = f"{self.base_topic}/derived/forecast"
                payload = dict(f, tick_ts=result["tick_ts"], series=result["series_for_mqtt"])
                # RETAINED, unlike the live state topics -- this publishes
                # only once an hour, so a page loaded in between would
                # otherwise see nothing. A slightly-old hourly forecast is
                # still correct, unlike a stale power reading.
                await mqtt.publish(topic, json.dumps(payload, separators=(",", ":")), qos=0, retain=True)

                points = [{"measurement": "forecast", "tags": {"role": "forecast", "source": "open-meteo"},
                           "fields": f, "timestamp": result["tick_ts"]}]
                points += result["hourly_points"]
                await self._write_influx(points)

                LOG.info("forecast: now=%sW today=%.2fkWh remaining=%.2fkWh tomorrow=%.2fkWh",
                          f["forecast_w"], f["forecast_today_kwh"],
                          f["forecast_remaining_kwh"], f["forecast_tomorrow_kwh"])
            except Exception as e:
                LOG.warning("forecast fetch failed: %s", e)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3600)
                break
            except asyncio.TimeoutError:
                pass


def _sha(path: str) -> str:
    """Short digest of a mounted file, logged at startup.

    A hash is the only cheap way to tell a stale bind mount from a current
    one. Comparing it against the repo answers the question directly instead
    of by inference from behaviour.
    """
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except Exception as e:
        return f"unreadable ({e})"


def _sha_glob(pattern: str) -> str:
    """Digest of every file matching pattern, concatenated in sorted order.

    Mirrors `cat app/*.py | sha256sum | cut -c1-12`, which is how deploy.sh
    computes the code hash this startup banner is checked against.
    """
    try:
        h = hashlib.sha256()
        for p in sorted(glob.glob(pattern)):
            with open(p, "rb") as f:
                h.update(f.read())
        return h.hexdigest()[:12]
    except Exception as e:
        return f"unreadable ({e})"


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"{name} is not set -- check .env / docker-compose.yml")
    return v


async def _read_secret_file(path: str, what: str, timeout: float = 30.0) -> str:
    """Reads a secret generated by another container and shared only via a
    Docker volume file -- never an env var, never .env (see
    influx/entrypoint.sh for the InfluxDB admin token, mosquitto/
    init-passwd.sh for the poller MQTT password). Waits up to `timeout` for
    it to appear (the writer's own init can race this container's
    startup), then gives up and returns "" rather than blocking forever."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(path) as f:
                secret = f.read().strip()
            if secret:
                return secret
        except OSError:
            pass
        await asyncio.sleep(1)
    LOG.warning("%s never appeared after %.0fs -- %s (a restart will retry)",
                path, timeout, what)
    return ""


async def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    LOG.info("starting eg4poll %s (%s)", VERSION, GIT_SHA)
    # deploy.sh parses these two lines back out of the container's logs to
    # prove the running code and config match the repo, rather than
    # inferring it from behaviour later.
    app_dir = os.path.dirname(os.path.abspath(__file__))
    LOG.info("code /app  sha256 %s", _sha_glob(os.path.join(app_dir, "*.py")))

    # The device/site config is now app-managed state (see devconfig.py),
    # written by the web Config page rather than hand-edited -- so unlike
    # code, there is nothing to compare this hash against; it is here
    # purely so a support log shows whether it changed between two runs.
    devcfg_path = os.environ.get("DEVCONFIG", "/data/config.yaml")
    cfg = devconfig.load(devcfg_path)
    LOG.info("config %s  sha256 %s", devcfg_path, _sha(devcfg_path))

    mqtt_cfg = {
        "host": _require_env("MQTT_HOST"),
        "port": int(os.environ.get("MQTT_PORT", 1883)),
        # Username is a fixed literal, not user-configurable -- mosquitto/acl's
        # `user poller` block requires this exact name. The password is
        # generated inside the mosquitto container itself (see
        # mosquitto/init-passwd.sh) and shared only via this file -- this
        # account is purely internal (eg4poll <-> its own bundled broker),
        # never exposed to a browser, so there is nothing for a human to type.
        "username": "poller",
        "password": await _read_secret_file(
            os.environ.get("MQTT_POLLER_PASS_FILE", "/mqtt-shared/mqtt-poller-pass"),
            "MQTT auth as poller will fail until it does",
        ),
        "base_topic": os.environ.get("MQTT_BASE_TOPIC", "energy"),
        "client_id": os.environ.get("MQTT_CLIENT_ID", "eg4poll"),
    }
    influx_cfg = {
        "url": os.environ.get("INFLUX_URL", "http://influxdb:8086"),
        "token": await _read_secret_file(
            os.environ.get("INFLUX_TOKEN_FILE", "/influx-shared/influx-admin-token"),
            "Influx writes disabled until it does",
        ),
        "org": os.environ.get("INFLUX_ORG", "eg4poll"),
        "bucket": os.environ.get("INFLUX_BUCKET", "energy"),
    }

    # One lock, shared between the HTTP arm/disarm API (webapp.py) and the
    # command handler that actually enforces it (Runner._commands). See
    # app/armlock.py -- built here regardless of whether Runner construction
    # below succeeds, so /api/arm stays a consistent, working endpoint even
    # in degraded mode (there's just nothing for it to gate yet).
    lock = armlock.ArmLock()

    # A bad config must never lock the user out of the page that would let
    # them fix it -- so the web API is built and started regardless of
    # whether Runner construction below succeeds. If it doesn't, the
    # process stays up in a degraded, poll-nothing state: reachable at
    # /api/config to inspect and fix, at which point PUT /api/config exits
    # the process (see webapp.py) and `restart: unless-stopped` retries
    # Runner construction from scratch.
    runner = None
    startup_error = None
    try:
        runner = Runner(cfg["devices"], mqtt_cfg, cfg["site"], influx_cfg, lock)
    except SystemExit as e:
        startup_error = str(e)
        LOG.error("config invalid -- polling disabled until fixed via the Config page: %s", startup_error)

    web_app = webapp.build_app(devcfg_path, runner, startup_error, lock)
    await webapp.start(web_app, port=int(os.environ.get("WEBAPP_PORT", 8081)))

    if runner is None:
        await asyncio.Event().wait()
        return

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, runner.stop)

    await runner.run()
    LOG.info("shutting down")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
