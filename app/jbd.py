"""
JBD / Jiabaida BMS driver (Eco-Worthy Cubix, RS485-1 set to HNJD).

Protocol notes -- verified against a real frame from the pack:

  Request:  DD A5 <cmd> 00 <chk_hi> <chk_lo> 77
  Response: DD <cmd> <status> <len> <data...> <chk_hi> <chk_lo> 77

  status 0x00 = OK, 0x80 = error
  checksum = (0x10000 - (len + sum(data))) & 0xFFFF, big-endian
  All multi-byte values are BIG-endian.

  cmd 0x03 = basic info  (voltage, current, capacity, SOC, temps, flags)
  cmd 0x04 = cell voltages (2 bytes per cell, mV)

IMPORTANT: 0x03 does NOT contain per-cell voltages. The trailing bytes are
NTC temperature sensors in 0.1 Kelvin. Getting this wrong yields plausible-
looking mV values that are actually temperatures.
"""

import asyncio
import logging
import time
from typing import Any

import serial_asyncio

LOG = logging.getLogger("eg4poll.jbd")

CMD_BASIC = 0x03
CMD_CELLS = 0x04

# Protection status bits (offset 16-17 of the 0x03 payload)
PROTECTION_BITS = [
    (0,  "cell_overvoltage"),
    (1,  "cell_undervoltage"),
    (2,  "pack_overvoltage"),
    (3,  "pack_undervoltage"),
    (4,  "charge_overtemp"),
    (5,  "charge_undertemp"),
    (6,  "discharge_overtemp"),
    (7,  "discharge_undertemp"),
    (8,  "charge_overcurrent"),
    (9,  "discharge_overcurrent"),
    (10, "short_circuit"),
    (11, "ic_front_end_error"),
    (12, "mosfet_lock"),
]


def checksum(length: int, data: bytes) -> int:
    return (0x10000 - (length + sum(data))) & 0xFFFF


def build_request(cmd: int) -> bytes:
    chk = checksum(cmd, b"")  # for a zero-length read, chk covers the cmd byte
    return bytes([0xDD, 0xA5, cmd, 0x00, (chk >> 8) & 0xFF, chk & 0xFF, 0x77])


def parse_response(buf: bytes, expect_cmd: int) -> bytes:
    """Validate framing and checksum; return the payload."""
    if len(buf) < 7:
        raise ValueError(f"short frame ({len(buf)} bytes)")
    if buf[0] != 0xDD:
        raise ValueError(f"bad start byte 0x{buf[0]:02X}")
    if buf[1] != expect_cmd:
        raise ValueError(f"cmd mismatch: got 0x{buf[1]:02X}, want 0x{expect_cmd:02X}")
    if buf[2] != 0x00:
        raise ValueError(f"BMS returned error status 0x{buf[2]:02X}")

    ln = buf[3]
    if len(buf) < ln + 7:
        raise ValueError(f"truncated: need {ln + 7} bytes, have {len(buf)}")

    data = buf[4:4 + ln]
    got = int.from_bytes(buf[4 + ln:6 + ln], "big")
    want = checksum(ln, data)
    if got != want:
        raise ValueError(f"checksum {got:04X} != {want:04X}")
    if buf[6 + ln] != 0x77:
        raise ValueError(f"bad terminator 0x{buf[6 + ln]:02X}")
    return data


def decode_basic(d: bytes) -> dict[str, Any]:
    """Decode a cmd 0x03 payload."""
    if len(d) < 23:
        raise ValueError(f"basic payload too short ({len(d)})")

    def u16(o: int) -> int:
        return int.from_bytes(d[o:o + 2], "big")

    def s16(o: int) -> int:
        return int.from_bytes(d[o:o + 2], "big", signed=True)

    out: dict[str, Any] = {}
    out["voltage"] = round(u16(0) / 100, 2)              # 10 mV units
    out["current"] = round(s16(2) / 100, 2)              # 10 mA, + = charge
    out["remaining_ah"] = round(u16(4) / 100, 2)
    out["nominal_ah"] = round(u16(6) / 100, 2)
    out["cycles"] = u16(8)

    pd = u16(10)
    out["production_date"] = f"{(pd >> 9) + 2000:04d}-{(pd >> 5) & 0x0F:02d}-{pd & 0x1F:02d}"

    bal = u16(12) | (u16(14) << 16)
    out["balance_bits"] = bal
    out["balancing"] = bal != 0
    out["balancing_cells"] = [i + 1 for i in range(32) if bal & (1 << i)]

    prot = u16(16)
    out["protection_bits"] = prot
    out["faults"] = [name for bit, name in PROTECTION_BITS if prot & (1 << bit)]
    out["problem"] = prot != 0

    out["sw_version"] = f"0x{d[18]:02X}"
    out["soc"] = d[19]                                    # RSOC %

    fet = d[20]
    out["fet_charge"] = bool(fet & 0x01)
    out["fet_discharge"] = bool(fet & 0x02)

    out["cell_count"] = d[21]
    ntc = d[22]
    out["ntc_count"] = ntc

    temps = []
    for i in range(ntc):
        o = 23 + i * 2
        if o + 2 > len(d):
            break
        # 0.1 Kelvin
        temps.append(round(u16(o) / 10 - 273.15, 2))
    out["temperatures_c"] = temps

    return out


def decode_cells(d: bytes) -> dict[str, Any]:
    """Decode a cmd 0x04 payload: 2 bytes per cell, millivolts."""
    n = len(d) // 2
    mv = [int.from_bytes(d[i * 2:i * 2 + 2], "big") for i in range(n)]
    out: dict[str, Any] = {
        "cells_mv": mv,
        "cell_count_reported": n,
    }
    return out


class JbdDevice:
    """Polls a JBD-family BMS over RS485 and returns a fixed-shape dict."""

    # Fixed key set -- every poll publishes all of these, null on failure.
    KEYS = [
        "voltage", "current", "soc", "remaining_ah", "nominal_ah",
        "cycles", "production_date", "sw_version",
        "balance_bits", "balancing", "balancing_cells",
        "protection_bits", "faults", "problem",
        "fet_charge", "fet_discharge",
        "cell_count", "ntc_count", "temperatures_c",
        "cells_mv", "cell_count_reported",
    ]

    def __init__(self, cfg: dict):
        self.name: str = cfg["name"]
        self.port: str = cfg["port"]
        self.baud: int = cfg.get("baud", 9600)
        self.timeout: float = cfg.get("timeout", 2.0)
        self.read_cells: bool = cfg.get("read_cells", True)
        self._lock = asyncio.Lock()
        self._reader = None
        self._writer = None
        self._fail_streak = 0

    async def connect(self) -> bool:
        await self.close()
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self.port, baudrate=self.baud,
                bytesize=8, parity="N", stopbits=1,
            )
            LOG.info("%s: connected on %s @ %d", self.name, self.port, self.baud)
            return True
        except Exception as e:
            LOG.warning("%s: connect failed: %s", self.name, e)
            self._reader = self._writer = None
            return False

    async def close(self):
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    async def _transact(self, cmd: int) -> bytes:
        """Send a read request and return the validated payload."""
        # Drain anything stale before writing -- a partial previous response
        # would otherwise be parsed as the head of this one.
        try:
            while True:
                stale = await asyncio.wait_for(self._reader.read(256), timeout=0.02)
                if not stale:
                    break
        except asyncio.TimeoutError:
            pass

        self._writer.write(build_request(cmd))
        await self._writer.drain()

        # Read the 4-byte header first so we know the payload length,
        # rather than guessing at a fixed read size.
        head = await asyncio.wait_for(self._reader.readexactly(4), timeout=self.timeout)
        ln = head[3]
        rest = await asyncio.wait_for(self._reader.readexactly(ln + 3), timeout=self.timeout)
        return parse_response(head + rest, cmd)

    async def poll(self, tick_ts: float) -> dict:
        values: dict[str, Any] = {k: None for k in self.KEYS}
        ok = 0
        total = 2 if self.read_cells else 1

        async with self._lock:
            if self._writer is None:
                if not await self.connect():
                    return self._envelope(tick_ts, values, 0, total, False)

            try:
                values.update(decode_basic(await self._transact(CMD_BASIC)))
                ok += 1
            except Exception as e:
                LOG.debug("%s: basic read failed: %s", self.name, e)

            if self.read_cells:
                try:
                    values.update(decode_cells(await self._transact(CMD_CELLS)))
                    ok += 1
                except Exception as e:
                    LOG.debug("%s: cell read failed: %s", self.name, e)

        if ok == 0:
            self._fail_streak += 1
            if self._fail_streak >= 3:
                LOG.warning("%s: %d failed polls, reconnecting", self.name, self._fail_streak)
                await self.close()
                self._fail_streak = 0
        else:
            self._fail_streak = 0

        return self._envelope(tick_ts, values, ok, total, True)

    def _envelope(self, tick_ts, values, ok, total, connected) -> dict:
        # No derived values here: cell min/max/delta, power, and the cell-sum
        # cross-check are all computed in Node-RED. Bitfield decoding stays,
        # since that is protocol parsing rather than arithmetic.
        return {
            "device": self.name,
            "tick_ts": round(tick_ts, 3),
            "read_ts": round(time.time(), 3),
            "connected": connected,
            "spans_ok": ok,
            "spans_total": total,
            "values": values,
        }
