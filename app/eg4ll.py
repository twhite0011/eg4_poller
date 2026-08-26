"""
EG4 LL-S driver (Modbus RTU over RS485).

Register map derived from tuxntoast/eg4-ll (egll.py), verified field-by-field
against a live scan of a 16S LL-S at slave ID 2, and cross-checked against
EG4's own "EG4-LL Battery MODBUS Communication Protocol" doc (V01.06) for
registers 21-38 -- which turned out to disagree with an earlier byte-offset
scheme this driver used to use for status/warning/protection/error (see git
history): that scheme was inherited from upstream's convention for a
DIFFERENT, sub-byte-packed layout, and does not apply to these registers,
which the vendor doc shows as plain, unpacked USHORTs, one address per field.
Confirmed wrong against a live raw scan before removing it: register 25
itself read 0x0002 (a real, documented status code -- "Inactive/Discharging"),
while the old byte-offset code produced "0100" for the same poll, which is
not a valid status code at all.

Three read blocks exist upstream (all FC3 holding-register reads):

    cell      addr 0    count 39    <- telemetry; this driver reads it
    config    addr 45   count 91    <- ALL ZEROS on the LL-S
    hardware  addr 105  count 23    <- ALL ZEROS on the LL-S

TESTED: on this LL-S both the config and hardware blocks read successfully
(no Modbus exception) but return all zeros. Upstream targets EG4-LL v1/v2;
the LL-S firmware does not populate them. Readable data ends at register 46;
the vendor doc's own register table jumps straight from 38 to 105 (Model),
so 39-46 stay undocumented.

Registers 45-46 are a u32 that increments ~1/second (observed +1910 over a
~32 minute gap) -- a clock or uptime counter. Not decoded here.

VERIFIED against a live pack:
    reg 0      voltage x0.01          5244 -> 52.44 V
    reg 1      current SIGNED x0.01   0xF739 -> -2247 -> -22.47 A
                 (x0.1 would be -224.7 A, impossible on a 100 A BMS)
    reg 2-17   16 cell voltages, mV   sum matched reg 0 to 4 mV
    reg 21     capacity remaining Ah  69 Ah, consistent with SOC 70%
    reg 24     SOC %                  70
    reg 25     status                 0x0002, a real status code (per vendor doc)
    reg 31-32  u32 / 3600 / 1000      -> exactly 100.0 Ah
    reg 33-35  temps 1-6, signed bytes, two per register (per vendor doc)
    reg 36     cell count             16
    reg 37     designed capacity x0.1 1000 -> 100.0 Ah, matches reg 31-32
    reg 38     cell balance status    one bit per cell (per vendor doc)
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

# Bit meanings below are from the vendor's own EG4-LL MODBUS Communication
# Protocol doc (V01.06) -- registers 25/26/27/28/38 are plain, unpacked
# USHORT registers there, one address per field. The byte-offset scheme this
# driver used to read them with (`_b()`/`_hex()`, removed) was inherited from
# upstream's convention for a DIFFERENT, sub-byte-packed layout that does not
# apply here; it silently spliced together bytes from two adjacent registers
# instead. Confirmed wrong against a live raw scan: register 25 read 0x0002
# (a real, documented status code), while the old code's status_hex read
# "0100" for the same poll -- not a valid code at all, just two mismatched
# bytes that happened to look plausible.
STATUS_STATE = {0x0: "standby", 0x1: "charging", 0x2: "discharging",
                 0x4: "protect", 0x8: "charging_lmt"}
WARNING_BITS = {
    0x0001: "pack_ov", 0x0002: "cell_ov", 0x0004: "pack_uv", 0x0008: "cell_uv",
    0x0010: "charge_oc", 0x0020: "discharge_oc", 0x0040: "abnormal_ambient_temp",
    0x0080: "mos_overheating", 0x0100: "charge_ot", 0x0200: "discharge_ot",
    0x0400: "charge_ut", 0x0800: "discharge_ut", 0x1000: "low_capacity",
    0x2000: "float_stopped",
}
PROTECTION_BITS = {
    0x0001: "pack_ov", 0x0002: "cell_ov", 0x0004: "pack_uv", 0x0008: "cell_uv",
    0x0010: "charge_oc", 0x0020: "discharge_oc", 0x0040: "abnormal_ambient_temp",
    0x0080: "mos_overheating", 0x0100: "charge_ot", 0x0200: "discharge_ot",
    0x0400: "charge_ut", 0x0800: "discharge_ut", 0x1000: "low_capacity",
    0x2000: "discharge_sc",
}
ERROR_BITS = {
    0x0001: "voltage_error", 0x0002: "temperature_error",
    0x0004: "current_flow_error", 0x0010: "cell_unbalance",
}


def _s8_hi(regs: list[int], r: int) -> int:
    v = (regs[r] >> 8) & 0xFF
    return v - 0x100 if v >= 0x80 else v


def _s8_lo(regs: list[int], r: int) -> int:
    v = regs[r] & 0xFF
    return v - 0x100 if v >= 0x80 else v


def _bit_names(raw: int, bits: dict[int, str]) -> str:
    return ",".join(name for bit, name in bits.items() if raw & bit)


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
    # float, not the raw int register: cubix (app/jbd.py) reports this same
    # field with sub-Ah precision, and both feed InfluxDB's one "battery"
    # measurement -- a per-device type mismatch on a shared field name gets
    # every write from whichever type loses the race rejected outright.
    out["remaining_ah"] = float(u16(21))
    out["max_charge_current"] = u16(22)
    out["soh"] = u16(23)
    out["soc"] = u16(24)
    out["cycles"] = u32(29)
    # u32 in amp-seconds -> Ah. Verified: 360000000 -> exactly 100.0 Ah.
    out["nominal_ah"] = round(u32(31) / 3600 / 1000, 2)
    out["cell_count"] = min(u16(36), 16)
    # 0.1AH scale per the manual. Independent corroboration that register
    # addressing lines up with the doc: 1000 -> 100.0 Ah, matching nominal_ah
    # above (from a completely different register pair, 31-32).
    out["designed_capacity_ah"] = round(u16(37) / 10, 1)

    # Status/Warning/Protection/Error: plain unpacked registers 25-28, one
    # address per field (see the module-level bit tables above for sourcing).
    status_raw = u16(25)
    warning_raw = u16(26)
    protection_raw = u16(27)
    error_raw = u16(28)
    out["status_hex"] = f"{status_raw:04X}"
    out["status_active"] = bool(status_raw & 0x8000)
    out["status_state"] = STATUS_STATE.get(status_raw & 0x000F,
                                             f"unknown_0x{status_raw & 0xF:X}")
    out["warning_hex"] = f"{warning_raw:04X}"
    out["warning_active"] = _bit_names(warning_raw, WARNING_BITS)
    out["protection_hex"] = f"{protection_raw:04X}"
    out["protection_active"] = _bit_names(protection_raw, PROTECTION_BITS)
    out["error_hex"] = f"{error_raw:04X}"
    out["error_active"] = _bit_names(error_raw, ERROR_BITS)
    out["problem"] = bool(warning_raw or protection_raw or error_raw)

    # Cell Balance Status (register 38): one bit per cell, "Cell N Balanced".
    # Same shape as app/jbd.py's balance_bits/balancing/balancing_cells for
    # the Cubix packs -- same field names too, so both the Python derive.py
    # and the external node_red_flow's "01 transform" (which special-cases
    # "balancing_cells" by that exact name) already know how to handle it.
    bal_raw = u16(38)
    out["balance_bits"] = bal_raw
    out["balancing"] = bal_raw != 0
    out["balancing_cells"] = [i + 1 for i in range(out["cell_count"]) if bal_raw & (1 << i)]

    # Temps: signed bytes, one per sensor, packed two per register across
    # 33-35 (manual: "Temp 6Byte, 1byte/1Sensor") -- high byte first within
    # each register, matching how probes 1-2 were already read and confirmed
    # sensible before this fix. Probes 3-6 read 0 when the physical sensor
    # isn't wired -- a protocol quirk, flagged here rather than left for
    # downstream to guess at. Min/max are not computed; that is arithmetic
    # and belongs downstream.
    temps = [_s8_hi(regs, 33), _s8_lo(regs, 33),
             _s8_hi(regs, 34), _s8_lo(regs, 34),
             _s8_hi(regs, 35), _s8_lo(regs, 35)]
    # float, not raw int: cubix (app/jbd.py) reports this same field with
    # sub-degree precision, and derive.py flattens both into shared
    # temp_N_c/temp_min_c/temp_max_c InfluxDB fields -- see remaining_ah's
    # identical fix above for why a per-device type mismatch here is fatal
    # to every write from whichever type loses the race.
    out["temperatures_c"] = [float(t) for t in temps]
    out["temp_probes_active"] = 2 + sum(1 for t in temps[2:] if t != 0)

    # Cells start at byte 7 == register 2.
    mv = [u16(2 + i) for i in range(out["cell_count"])]
    out["cells_mv"] = mv

    # --- Registers 39-46: past the vendor doc's own register table (which
    # jumps from 38 straight to 105/Model), still undocumented. ---
    # 40/41/42/43 read 0x0007, 0x0FFF, 0x07FF, 0x000F -- all-ones masks with
    # 3/12/11/4 bits set. Almost certainly capability or alarm-enable
    # bitfields rather than telemetry. Exposed raw and unnamed rather than
    # guessed at.
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
        "remaining_ah", "nominal_ah", "designed_capacity_ah",
        "cycles", "max_charge_current",
        "temperature_mos_c", "temperatures_c", "temp_probes_active",
        "status_hex", "status_active", "status_state",
        "warning_hex", "warning_active",
        "protection_hex", "protection_active",
        "error_hex", "error_active", "problem",
        "balance_bits", "balancing", "balancing_cells",
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
