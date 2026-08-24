"""The web-managed device/site config: schema, load/save, USB discovery.

Replaces the old model where config.yaml was a hand-edited file bind-mounted
in read-only and every USB device had to be declared in docker-compose.yml
up front. Now: the container can see any attached USB-serial adapter (see
docker-compose.yml's device_cgroup_rules), the Config web page lets you pick
one per device slot, and this module is what reads/writes the result.

Register maps (config/eg4_6000xp_registers.yaml) are NOT part of this --
those are protocol definitions, tracked and baked into the image, not user
config. Likewise MQTT/InfluxDB connection details stay in .env: they
describe how this container talks to its own bundled services, not
something that changes based on what hardware is plugged in.
"""

import copy
import os
import pathlib

import yaml

# Fixed per device type -- only one inverter model is supported today.
# Not user-editable: a wrong path here silently breaks decoding in a way
# nothing would catch until values come back wrong.
DEFAULT_REGISTER_MAP = "/config/eg4_6000xp_registers.yaml"

DEFAULT_POLL_INTERVAL = 10.0

# One entry per supported driver, with the fields InstanceDevice/JbdDevice/
# Eg4LlDevice actually read (see app/poller.py, app/jbd.py, app/eg4ll.py).
# The Config page uses this to know which fields to show for a given type.
TYPE_FIELDS = {
    "modbus": {"unit_id": 1, "baud": 19200, "timeout": 1.5, "read_holding": True},
    "jbd": {"baud": 9600, "timeout": 2.0, "read_cells": True},
    "eg4ll": {"unit_id": 2, "baud": 9600, "timeout": 2.0},
}

TEMPLATE = {
    # holidays: ISO dates ("2026-12-25"), for automations' "weekend_or_holiday"
    # day condition -- see app/automation.py. A fact only the site owner knows,
    # entered once, same reasoning as lat/lon: no external holiday API
    # call, which would have to guess a region/observance anyway.
    "site": {"lat": None, "lon": None, "tz": "UTC", "holidays": []},
    "devices": [],
    # Full-scale watts per flow, for sizing the dashboard's one-line diagram
    # (arc fill, flow-line animation speed). Display tuning only -- nothing
    # backend-side reads this; it exists here purely so it's editable on the
    # Config page instead of a separate hand-maintained file.
    "scale": {"pv": 4000, "grid": 8000, "load": 6000, "batt": 6000},
    # Rule-table automations for writable inverter settings -- see
    # app/automation.py for the schema and evaluation semantics.
    "automations": [],
}


def _new_device(name: str, dtype: str, port: str) -> dict:
    if dtype not in TYPE_FIELDS:
        raise ValueError(f"unknown device type {dtype!r}")
    d = {
        "name": name, "type": dtype, "enabled": True,
        "model": "", "role": "inverter" if dtype == "modbus" else "battery",
        "port": port,
        "poll_interval": DEFAULT_POLL_INTERVAL,
        "tags": {},
    }
    d.update(copy.deepcopy(TYPE_FIELDS[dtype]))
    if dtype == "modbus":
        d["register_map"] = DEFAULT_REGISTER_MAP
    return d


DEFAULT_TEMPLATE_PATH = "/config/config.example.yaml"


def load(path: str, template_path: str = DEFAULT_TEMPLATE_PATH) -> dict:
    """Read the live config, seeding it on first boot (empty volume) from
    the tracked template -- config/config.example.yaml, bind-mounted
    read-only at /config alongside the register maps. Falls back to the
    built-in TEMPLATE if that file is somehow missing too, rather than
    failing to start at all."""
    p = pathlib.Path(path)
    if not p.exists():
        try:
            with open(template_path) as f:
                seed = yaml.safe_load(f) or {}
        except OSError:
            seed = {}
        for k in TEMPLATE:
            seed.setdefault(k, copy.deepcopy(TEMPLATE[k]))
        save(path, seed)
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    # Fill in anything an older/partial file is missing rather than KeyError
    # deep in the web API.
    for k in TEMPLATE:
        data.setdefault(k, copy.deepcopy(TEMPLATE[k]))
    # site is a nested dict, so the top-level setdefault above only helps a
    # config missing the whole `site:` block -- one that already has it
    # (every config from before "holidays" existed) needs its own key
    # defaulted too, or automations would KeyError on a perfectly normal
    # upgrade rather than just seeing an empty holiday list.
    if isinstance(data.get("site"), dict):
        data["site"].setdefault("holidays", [])
    return data


def save(path: str, data: dict) -> None:
    """Write the live config. This file is now app-managed state, not a
    hand-edited one -- comments and formatting do not survive a save, which
    is an accepted tradeoff of that (there is nothing left here that a human
    was meant to hand-tune; the register maps, which are, live elsewhere and
    untouched)."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    tmp.replace(p)  # atomic on the same filesystem -- no half-written config


def discover_devices(configured: list[dict] | None = None) -> list[dict]:
    """USB-serial adapters currently visible under /dev/serial/by-id,
    each flagged with whichever configured device (if any) already claims
    it. by-id paths are what's stored in a device's `port:` -- stable
    across reboots (keyed to the adapter's own serial number), unlike
    /dev/ttyUSB0.
    """
    by_id = pathlib.Path("/dev/serial/by-id")
    assigned = {d.get("port"): d.get("name") for d in (configured or [])}

    if not by_id.is_dir():
        return []

    out = []
    for entry in sorted(by_id.iterdir()):
        path = str(entry)
        out.append({
            "port": path,
            "assigned_to": assigned.get(path),
        })
    return out


def add_device(config: dict, name: str, dtype: str, port: str) -> dict:
    """Returns the new device dict; caller is responsible for appending it
    and validating (duplicate names, duplicate ports) before saving --
    Runner.__init__ already does exactly that validation at startup, so it
    is not duplicated here."""
    return _new_device(name, dtype, port)
