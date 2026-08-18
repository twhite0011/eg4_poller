"""Derived values -- everything the poller deliberately does not compute.

Ported from the Node-RED flow this project used to run (nodered/flows.json,
"01 transform" + "02 bank join", now removed): PV current correction,
battery/pack power, trapezoidal energy integration, cell math, and
capacity-weighted bank aggregation across packs. The numbers below (PV_SLOPE,
PV_INTERCEPT, the inverter-vs-BMS offset noted in bank_join) are fitted
against real hardware, not placeholders -- see the notes on each.

Node-RED's flow context (flow.get/flow.set), which carried state between
invocations, is DeriveState below: one instance, held by Runner, passed into
every poll.
"""

import time
from typing import Any


def _num(x) -> float | None:
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _r(x, n: int) -> float | None:
    """Round to n decimals, passing None through -- matches the r() helper
    in the original Node-RED function and its Math.round(x * 10**n) / 10**n
    idiom. (Python's round() uses round-half-to-even at exact .5 boundaries
    where JS rounds half-away-from-zero; real sensor floats essentially
    never land exactly on that boundary, so the difference is immaterial.)"""
    return None if x is None else round(x, n)


# PV current correction for the EG4 6000XP. This inverter has NO PV current
# register -- current is derived as P/V, and that derived value carries a
# fixed zero-point offset. Fitted against DC clamp readings, 11 points
# 0.3-4.5 A: R2 0.994, RMSE 0.107 A. Slope is within 3% of unity, so this is
# essentially pure offset (~0.63 A). Zero-crossing at raw 0.633 A, matching
# the observed first-light reading. Re-fit if the array changes; recheck
# seasonally (Hall offsets drift with temp).
PV_SLOPE = 1.0328
PV_INTERCEPT = -0.6537
# The MPPT drops out below ~120 V, so guard the divide.
PV_MIN_V = 20

# Energy integration. Trapezoidal between consecutive ticks. If the gap
# exceeds this, the poller was down and integrating across it would invent
# energy that never flowed -- skip the interval instead. 60 s = six missed
# 10 s ticks.
MAX_DT_S = 60

# Packs must share a tick within this window to be summed into the bank --
# stops a stalled device from quietly skewing the bank total.
MAX_SKEW_S = 2.0


class DeriveState:
    """Per-device state carried between polls. Two independent stores:
    _energy (integration accumulators, keyed by an integration key -- "pv"
    or a device name) and _last (each device's most recent derived fields,
    keyed by device name, used by bank_join to find and join packs)."""

    def __init__(self):
        self._energy: dict[str, dict[str, float]] = {}
        self._last: dict[str, dict[str, Any]] = {}

    def integrate(self, key: str, tick_ts: float, charge_w: float, discharge_w: float) -> dict:
        prev = self._energy.get(key)
        in_wh = out_wh = 0.0
        if prev and tick_ts > prev["ts"]:
            dt = tick_ts - prev["ts"]
            if 0 < dt <= MAX_DT_S:
                in_wh = ((prev["chg"] + charge_w) / 2) * dt / 3600
                out_wh = ((prev["dis"] + discharge_w) / 2) * dt / 3600
            # else: gap too large (or clock went backwards) -- skip rather
            # than invent energy that never flowed.

        tot_in = (prev["tot_in"] if prev else 0.0) + in_wh
        tot_out = (prev["tot_out"] if prev else 0.0) + out_wh
        self._energy[key] = {
            "ts": tick_ts, "chg": charge_w, "dis": discharge_w,
            "tot_in": tot_in, "tot_out": tot_out,
        }
        return {
            "energy_in_wh_delta": _r(in_wh, 5),
            "energy_out_wh_delta": _r(out_wh, 5),
            "energy_in_kwh": _r(tot_in / 1000, 5),
            "energy_out_kwh": _r(tot_out / 1000, 5),
            "energy_net_kwh": _r((tot_in - tot_out) / 1000, 5),
        }


def transform(state: DeriveState, envelope: dict) -> dict | None:
    """Raw poller envelope -> derived fields, for one device.

    Returns None if the poll had nothing to derive from (poller.py already
    publishes the raw envelope regardless; this is skipped, not an error).
    Otherwise returns {"fields", "cell_fields", "measurement", "tags"} --
    ready for an Influx write and an energy/derived/<device> MQTT publish.
    """
    v = envelope.get("values")
    if not v:
        return None
    if not envelope.get("connected") or envelope.get("spans_ok") == 0:
        return None

    tags = dict(envelope.get("tags") or {})
    tick_ts = envelope.get("tick_ts") or time.time()

    fields: dict[str, Any] = {}
    cell_fields: dict[str, Any] = {}

    def add(k, val):
        if val is not None:
            fields[k] = val

    def add_cell(k, val):
        if val is not None:
            cell_fields[k] = val

    # Pass raw values through unchanged. Arrays are expanded; Influx has no
    # array type, so cells_mv becomes cell_01..cell_16.
    for k, val in v.items():
        if val is None:
            continue
        if isinstance(val, list):
            if k == "cells_mv":
                for i, mv in enumerate(val):
                    add_cell(f"cell_{i + 1:02d}_mv", mv)
            elif k == "temperatures_c":
                for i, t in enumerate(val):
                    add(f"temp_{i + 1}_c", t)
            elif k in ("balancing_cells", "faults"):
                add(f"{k}_count", len(val))
                if val:
                    add(f"{k}_list", ",".join(str(x) for x in val))
            continue
        add(k, val)

    # ---- Derived: inverter ----
    if tags.get("role") == "inverter":
        chg, dis, bv = _num(v.get("battery_charge_w")), _num(v.get("battery_discharge_w")), _num(v.get("battery_voltage"))

        if chg is not None and dis is not None:
            net = chg - dis
            add("battery_net_w", net)
            # DIAGNOSTIC ONLY. This inverter has no battery current register,
            # so this is net/volts, and it reads ~1.1 A low. Characterised
            # across 12.5-48.1 A: gap +1.116 / +1.202 / +1.033 A -- a fixed
            # offset, not a gain error. Same character as the PV channel;
            # the two likely share sense hardware. DELIBERATELY NOT
            # CORRECTED -- ground truth for battery power/current is
            # bank.current / bank.power_w (two independent BMS shunts).
            # Kept because this is the number the inverter itself acts on.
            if bv:
                add("battery_current_derived", _r(net / bv, 3))

        pv_total_raw = pv_total_corr = 0.0
        saw_pv = False
        for n in (1, 2):
            pv, pp = _num(v.get(f"pv{n}_voltage")), _num(v.get(f"pv{n}_power"))
            if pv is None or pp is None:
                continue
            saw_pv = True
            pv_total_raw += pp
            if pv < PV_MIN_V:
                add(f"pv{n}_current_corrected", 0)
                add(f"pv{n}_power_corrected", 0)
                continue
            raw_a = pp / pv
            corr_a = max(0.0, PV_SLOPE * raw_a + PV_INTERCEPT)
            add(f"pv{n}_current", _r(raw_a, 3))
            add(f"pv{n}_current_corrected", _r(corr_a, 3))
            cw = _r(corr_a * pv, 1)
            add(f"pv{n}_power_corrected", cw)
            pv_total_corr += cw
        if saw_pv:
            add("pv_total_w", pv_total_raw)
            add("pv_total_w_corrected", _r(pv_total_corr, 1))
            # PV only flows one way, so the "in" side carries it. Integrates
            # the CORRECTED figure -- the inverter's own pv1_today_kwh
            # accumulates the uncorrected one and overstates by ~20%.
            e = state.integrate("pv", tick_ts, pv_total_corr, 0)
            add("pv_energy_wh_delta", e["energy_in_wh_delta"])
            add("pv_energy_kwh", e["energy_in_kwh"])

    # ---- Derived: batteries (both JBD and EG4 LL-S) ----
    if tags.get("role") == "battery":
        volts, amps = _num(v.get("voltage")), _num(v.get("current"))
        if volts is not None and amps is not None:
            p = _r(volts * amps, 1)  # signed: + charging
            add("power_w", p)
            # Unsigned split -- exactly one is non-zero at a time, so each
            # can be integrated independently.
            add("charge_w", max(0.0, p))
            add("discharge_w", max(0.0, -p))
            add("charge_a", _r(max(0.0, amps), 3))
            add("discharge_a", _r(max(0.0, -amps), 3))

            e = state.integrate(envelope["device"], tick_ts, max(0.0, p), max(0.0, -p))
            for k, val in e.items():
                add(k, val)

        mv = [x for x in (v.get("cells_mv") or []) if x > 0]
        if mv:
            mx, mn = max(mv), min(mv)
            add("cell_max_mv", mx)
            add("cell_min_mv", mn)
            add("cell_delta_mv", mx - mn)
            add("cell_avg_mv", _r(sum(mv) / len(mv), 1))
            add("cell_max_index", mv.index(mx) + 1)
            add("cell_min_index", mv.index(mn) + 1)
            # Cross-check: cell sum vs the pack's own reported voltage. A gap
            # of more than ~0.05 V means a bad read or a miscounted cell.
            cell_sum = _r(sum(mv) / 1000, 3)
            add("cell_sum_v", cell_sum)
            if volts is not None:
                add("cell_sum_delta_v", _r(cell_sum - volts, 3))

        # The LL-S reports 0 for absent probes 3 and 4.
        temps_raw = v.get("temperatures_c") or []
        temps = [t for i, t in enumerate(temps_raw) if i < 2 or t != 0]
        if temps:
            add("temp_max_c", max(temps))
            add("temp_min_c", min(temps))

    # Stash for the cross-device join.
    state._last[envelope["device"]] = {"tick_ts": tick_ts, "tags": tags, "fields": fields}

    measurement = "inverter" if tags.get("role") == "inverter" else "battery"
    return {
        "fields": fields, "cell_fields": cell_fields,
        "measurement": measurement, "tags": tags, "tick_ts": tick_ts,
    }


def bank_join(state: DeriveState) -> dict | None:
    """Aggregate every battery pack whose tick_ts is within MAX_SKEW_S of
    the newest. Called once per tick, after every device's transform()."""
    packs = [p for p in state._last.values() if p["tags"].get("role") == "battery"]
    if len(packs) < 2:
        return None

    newest = max(p["tick_ts"] for p in packs)
    fresh = [p for p in packs if newest - p["tick_ts"] <= MAX_SKEW_S]
    if len(fresh) < 2:
        return None

    def total(f):
        return sum(p["fields"].get(f, 0) for p in fresh)

    def have(f):
        return all(isinstance(p["fields"].get(f), (int, float)) and not isinstance(p["fields"].get(f), bool)
                   for p in fresh)

    fields: dict[str, Any] = {"pack_count": len(fresh)}

    # Bank current from the BMS shunts -- two direct measurements, versus
    # the inverter's derived P/V.
    if have("current"):
        fields["current"] = round(total("current"), 3)
    if have("power_w"):
        p = round(total("power_w"), 1)
        fields["power_w"] = p
        fields["charge_w"] = max(0.0, p)
        fields["discharge_w"] = max(0.0, -p)
    # Bank energy is the SUM of the per-pack deltas, not a re-integration of
    # bank power -- keeps the bank total consistent with the packs by
    # construction, rather than letting them drift apart.
    if have("energy_in_wh_delta") and have("energy_out_wh_delta"):
        fields["energy_in_wh_delta"] = round(total("energy_in_wh_delta"), 5)
        fields["energy_out_wh_delta"] = round(total("energy_out_wh_delta"), 5)
    if have("energy_in_kwh") and have("energy_out_kwh"):
        fields["energy_in_kwh"] = round(total("energy_in_kwh"), 5)
        fields["energy_out_kwh"] = round(total("energy_out_kwh"), 5)
        fields["energy_net_kwh"] = round(fields["energy_in_kwh"] - fields["energy_out_kwh"], 5)
    if have("remaining_ah"):
        fields["remaining_ah"] = round(total("remaining_ah"), 2)
    if have("nominal_ah"):
        fields["nominal_ah"] = round(total("nominal_ah"), 2)

    # Bank SOC weighted by pack capacity -- a plain average is wrong the
    # moment the packs differ in size.
    if have("soc") and have("nominal_ah"):
        cap = total("nominal_ah")
        if cap > 0:
            weighted = sum(p["fields"]["soc"] * p["fields"]["nominal_ah"] for p in fresh)
            fields["soc"] = round(weighted / cap, 2)

    # SOC spread between packs: the inter-brand counter drift, measured. The
    # packs are voltage-locked, so a persistent gap is calibration, not state.
    if have("soc"):
        socs = [p["fields"]["soc"] for p in fresh]
        fields["soc_spread"] = max(socs) - min(socs)

    # Load sharing. A ratio drifting from 1.0 over time points at unequal
    # cable or terminal resistance -- the pack carrying more does more cycling.
    if have("current"):
        amps = [abs(p["fields"]["current"]) for p in fresh]
        tot = sum(amps)
        if tot > 1:  # ignore near-idle, where percentages are meaningless
            for p, a in zip(fresh, amps):
                fields[f"share_{p['tags'].get('device')}_pct"] = round((a / tot) * 1000) / 10
            fields["share_imbalance_pct"] = round((max(amps) - min(amps)) / tot * 1000) / 10

    # Worst cell across the whole bank, and the widest spread within any pack.
    maxes = [p["fields"]["cell_max_mv"] for p in fresh if isinstance(p["fields"].get("cell_max_mv"), (int, float))]
    mins = [p["fields"]["cell_min_mv"] for p in fresh if isinstance(p["fields"].get("cell_min_mv"), (int, float))]
    if maxes and mins:
        fields["cell_max_mv"] = max(maxes)
        fields["cell_min_mv"] = min(mins)
        fields["cell_delta_mv"] = max(maxes) - min(mins)
    deltas = [p["fields"]["cell_delta_mv"] for p in fresh if isinstance(p["fields"].get("cell_delta_mv"), (int, float))]
    if deltas:
        fields["worst_pack_delta_mv"] = max(deltas)

    temps = [p["fields"]["temp_max_c"] for p in fresh if isinstance(p["fields"].get("temp_max_c"), (int, float))]
    if temps:
        fields["temp_max_c"] = max(temps)

    # Inverter cross-check. RESOLVED: fixed offset of ~1.12 A, not a gain
    # error (see the note in transform()). Left uncorrected by choice; the
    # BMS shunts are ground truth. Kept as a drift monitor: if the gap moves
    # away from ~1.12 A, a sensor has changed and is worth investigating.
    #
    # Found by role rather than a hardcoded device name -- device names are
    # user-configurable (see devconfig.py), so "the inverter" has to be
    # looked up, not assumed to be called "inverter_1".
    inv = next((p for p in state._last.values() if p["tags"].get("role") == "inverter"), None)
    if inv and abs(inv["tick_ts"] - newest) <= MAX_SKEW_S:
        d = inv["fields"].get("battery_current_derived")
        if isinstance(d, (int, float)) and isinstance(fields.get("current"), (int, float)):
            fields["inverter_current_derived"] = d
            fields["inverter_vs_bms_a"] = round(d - fields["current"], 3)

    return {
        "fields": fields,
        "measurement": "bank",
        "tags": {"role": "bank", "location": fresh[0]["tags"].get("location", "")},
        "tick_ts": newest,
    }
