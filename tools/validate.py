#!/usr/bin/env python3
"""Pre-deploy checks that do not need hardware.

Catches the class of mistake that has actually bitten this project: a
register in the wrong address space, a writable register with no range, a
settings-page key that does not exist in the map.
"""
import re, sys, pathlib, yaml

root = pathlib.Path(__file__).resolve().parent.parent
fail = []

rm = yaml.safe_load((root / "config/eg4_6000xp_registers.yaml").read_text())

# duplicate addresses within a space (packed sub-fields excepted)
for space in ("input", "holding"):
    seen = {}
    for r in rm[space]:
        seen.setdefault(r["addr"], []).append(r)
    for addr, rs in seen.items():
        if len(rs) == 1:
            continue
        sigs = {(r.get("bitmask"), r.get("shift")) for r in rs}
        if len(sigs) != len(rs):
            fail.append(f"{space} addr {addr} duplicated: "
                        + ", ".join(r["key"] for r in rs))

# writable registers need bounds, unless they are times
for r in rm["holding"]:
    if r.get("writable") and r.get("type") != "time":
        if r.get("min") is None or r.get("max") is None:
            fail.append(f"holding {r['key']} is writable with no min/max")

# every key the settings page references must exist and be writable
html = (root / "dashboard/solar_settings.html").read_text()
ui = set(re.findall(r'\["(set_[a-z0-9_]+)"', html))
keys = {r["key"] for r in rm["holding"]}
writable = {r["key"] for r in rm["holding"] if r.get("writable")}
for k in ui - keys:
    fail.append(f"settings page references unknown register {k}")
for k in ui - writable - (ui - keys):
    fail.append(f"settings page offers {k} but the map says read-only")

# The device/site config template must stay a TEMPLATE. It seeds the
# writable volume on first boot (see app/devconfig.py) and is the only
# device/site config that ever gets committed -- the real, live config
# lives on a Docker volume, edited only through the validated web API,
# never on the host and never in this repo. So this file must never contain
# a real device: `port` is a USB adapter's own serial number
# (tools/check_secrets.py already flags that pattern elsewhere), and a real
# device here would be exactly that, just missed by the regex because it is
# sitting in a file check_secrets.py doesn't expect to hold one.
tmpl = yaml.safe_load((root / "config/config.example.yaml").read_text())
if tmpl.get("devices") != []:
    fail.append("config.example.yaml: devices must be empty -- a real device's port "
                 "is a USB adapter serial number and must never be committed")
if not isinstance(tmpl.get("site"), dict) or "tz" not in tmpl["site"]:
    fail.append("config.example.yaml: site block missing or incomplete")
elif tmpl["site"].get("lat") is not None or tmpl["site"].get("lon") is not None:
    fail.append("config.example.yaml: site.lat/lon must be blank (null) in the tracked template")

if fail:
    print("  FAILED:")
    for f in fail:
        print("    -", f)
    sys.exit(1)
print(f"  OK — {len(rm['input'])} input, {len(rm['holding'])} holding, "
      f"{len(writable)} writable registers")
