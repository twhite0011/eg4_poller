"""
EG4 LL-S driver (Modbus RTU over RS485).

Register map derived from tuxntoast/eg4-ll (egll.py), verified field-by-field
against a live scan of a 16S LL-S at slave ID 2.

The upstream driver works in BYTE offsets into the raw Modbus response
(`[id][0x03][bytecount][data...]`), so:

    register = (byte_offset - 3) / 2

Odd byte offsets are a single byte inside a register -- temperatures and the
status/warning/protection flags are packed that way, which is why they need
explicit handling rather than a simple register table.

Three read blocks exist upstream (all FC3 holding-register reads):

    cell      addr 0    count 39    <- telemetry; this driver reads it
    config    addr 45   count 91    <- ALL ZEROS on the LL-S
    hardware  addr 105  count 23    <- ALL ZEROS on the LL-S

TESTED: on this LL-S both the config and hardware blocks read successfully
(no Modbus exception) but return all zeros. Upstream targets EG4-LL v1/v2;
the LL-S firmware does not populate them. Readable data ends at register 46.

Registers 45-46 are a u32 that increments ~1/second (observed +1910 over a
~32 minute gap) -- a clock or uptime counter. Not decoded here.

VERIFIED against a live pack:
    reg 0      voltage x0.01          5244 -> 52.44 V
    reg 1      current SIGNED x0.01   0xF739 -> -2247 -> -22.47 A
                 (x0.1 would be -224.7 A, impossible on a 100 A BMS)
    reg 2-17   16 cell voltages, mV   sum matched reg 0 to 4 mV
    reg 21     capacity remaining Ah  69 Ah, consistent with SOC 70%
    reg 24     SOC %                  70
    reg 31-32  u32 / 3600 / 1000      -> exactly 100.0 Ah
    reg 33-34  temps 1-4, signed bytes, two per register
    reg 36     cell count             16
"""

import asyncio
import logging
import time
from typing import Any

from pymodbus.client import AsyncModbusSerialClient

LOG = logging.getLogger("eg4poll.eg4ll")

# Read the whole telemetry block in one transaction.
# Upstream uses count=39 (registers 0-38). A full scan of this LL-S found
# live data through register 46, so we read 47 and expose the extras.
# Everything from 47 up reads zero; 720/1340/1920 return 0xFFFF (unmapped).
BLOCK_START = 0
BLOCK_COUNT = 47

# Bit meanings for the packed warning/protection/error fields are not
# published by EG4. Upstream keeps them as hex strings; so do we. A non-zero
# value is actionable even when the specific bit is unknown.


def _b(regs: list[int], byte_off: int) -> int:
    """Fetch one byte by its position in the raw packet's data area."""
    idx = byte_off - 3
    r, hi = divmod(idx, 2)
    if r >= len(regs):
        raise IndexError(f"byte {byte_off} past end of block")
    w = regs[r]
    return (w >> 8) & 0xFF if hi == 0 else w & 0xFF


def _s8(regs: list[int], byte_off: int) -> int:
    v = _b(regs, byte_off)
    return v - 0x100 if v >= 0x80 else v


def _hex(regs: list[int], start: int, end: int) -> str:
    return "".join(f"{_b(regs, o):02X}" for o in range(start, end))


def decode_block(regs: list[int]) -> dict[str, Any]:
    if len(regs) < BLOCK_COUNT:
        raise ValueError(f"short block: {len(regs)} of {BLOCK_COUNT}")

    def u16(r: int) -> int:
        return regs[r]

    def s16(r: int) -> int:
        return regs[r] - 0x10000 if regs[r] >= 0x8000 else regs[r]

    def u32(r: int) -> int:
        return (regs[r] << 16) | regs[r + 1]

    out: dict[str, Any] = {}
    out["voltage"] = round(u16(0) / 100, 2)
    out["current"] = round(s16(1) / 100, 2)          # + charge, - discharge
    out["temperature_mos_c"] = s16(18)
    out["remaining_ah"] = u16(21)
    out["max_charge_current"] = u16(22)
    out["soh"] = u16(23)
    out["soc"] = u16(24)
    out["cycles"] = u32(29)
    # u32 in amp-seconds -> Ah. Verified: 360000000 -> exactly 100.0 Ah.
    out["nominal_ah"] = round(u32(31) / 3600 / 1000, 2)
    out["cell_count"] = min(u16(36), 16)

    # Packed single-byte fields.
    out["heater_hex"] = _hex(regs, 53, 54)
    out["status_hex"] = _hex(regs, 54, 56)
    out["warning_hex"] = _hex(regs, 55, 57)
    out["protection_hex"] = _hex(regs, 57, 59)
    out["error_hex"] = _hex(regs, 59, 61)
    out["problem"] = any(
        int(out[k], 16) != 0
        for k in ("warning_hex", "protection_hex", "error_hex")
    )

    # Temps: signed bytes, two per register at 33 and 34.
    # Probes 3 and 4 read 0 when absent -- that is a protocol quirk, so it is
    # flagged here rather than left for Node-RED to guess at. Min/max are not
    # computed; that is arithmetic and belongs downstream.
    temps = [_s8(regs, o) for o in (69, 70, 71, 72)]
    out["temperatures_c"] = temps
    out["temp_probes_active"] = 2 + sum(1 for t in temps[2:] if t != 0)

    # Cells start at byte 7 == register 2.
    mv = [u16(2 + i) for i in range(out["cell_count"])]
    out["cells_mv"] = mv

    # --- Registers 39-46: past the upstream block, undocumented ---
    # 40/41/42/43 read 0x0007, 0x0FFF, 0x07FF, 0x000F -- all-ones masks with
    # 3/12/11/4 bits set. Almost certainly capability or alarm-enable
    # bitfields rather than telemetry, which also hints that the warning and
    # protection words carry 12 and 11 meaningful bits.
    # Exposed raw and unnamed rather than guessed at.
    if len(regs) > 46:
        for r in range(39, 47):
            out[f"raw_reg_{r}"] = regs[r]
        # 45-46 is a u32 that advances ~1/second. Reads as a Unix timestamp
        # roughly 75 days behind wall clock, so an unsynced RTC rather than
        # live time. Useful as a monotonic uptime/sequence marker: a value
        # that stops advancing means the BMS has stalled.
        out["bms_clock"] = (regs[45] << 16) | regs[46]

    return out


class Eg4LlDevice:
    """Polls an EG4 LL-S over Modbus RTU. Same interface as the other devices."""

    KEYS = [
        "voltage", "current", "soc", "soh",
        "remaining_ah", "nominal_ah", "cycles", "max_charge_current",
        "temperature_mos_c", "temperatures_c", "temp_probes_active",
        "heater_hex", "status_hex", "warning_hex", "protection_hex",
        "error_hex", "problem",
        "cell_count", "cells_mv",
        "bms_clock",
    ] + [f"raw_reg_{r}" for r in range(39, 47)]

    def __init__(self, cfg: dict):
        self.name: str = cfg["name"]
        self.port: str = cfg["port"]
        self.baud: int = cfg.get("baud", 9600)
        self.unit: int = cfg.get("unit_id", 2)   # matches the pack's DIP switches
        self.timeout: float = cfg.get("timeout", 2.0)
        self.client: AsyncModbusSerialClient | None = None
        self._lock = asyncio.Lock()
        self._fail_streak = 0

    async def connect(self) -> bool:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = AsyncModbusSerialClient(
            port=self.port, baudrate=self.baud,
            bytesize=8, parity="N", stopbits=1, timeout=self.timeout,
        )
        ok = await self.client.connect()
        LOG.info("%s: %s on %s @ %d (slave %d)",
                 self.name, "connected" if ok else "connect FAILED",
                 self.port, self.baud, self.unit)
        return ok

    async def poll(self, tick_ts: float) -> dict:
        values: dict[str, Any] = {k: None for k in self.KEYS}
        ok = 0

        async with self._lock:
            if self.client is None or not self.client.connected:
                if not await self.connect():
                    return self._envelope(tick_ts, values, 0, 1, False)
            try:
                rr = await self.client.read_holding_registers(
                    address=BLOCK_START, count=BLOCK_COUNT, slave=self.unit
                )
                if rr.isError():
                    LOG.debug("%s: modbus error: %s", self.name, rr)
                else:
                    values.update(decode_block(list(rr.registers)))
                    ok = 1
            except Exception as e:
                LOG.debug("%s: read failed: %s", self.name, e)

        if ok == 0:
            self._fail_streak += 1
            if self._fail_streak >= 3:
                LOG.warning("%s: %d failed polls, reconnecting",
                            self.name, self._fail_streak)
                self.client = None
                self._fail_streak = 0
        else:
            self._fail_streak = 0

        return self._envelope(tick_ts, values, ok, 1, True)

    def _envelope(self, tick_ts, values, ok, total, connected) -> dict:
        return {
            "device": self.name,
            "tick_ts": round(tick_ts, 3),
            "read_ts": round(time.time(), 3),
            "connected": connected,
            "spans_ok": ok,
            "spans_total": total,
            "values": values,
        }
