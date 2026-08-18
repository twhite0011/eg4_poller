#!/usr/bin/env python3
"""Refuse to commit anything site-specific.

The point is not to catch today's leak -- that is already fixed -- but the
next one. Every time a real hostname or a coordinate gets typed into a
tracked file while debugging, this is what notices before it reaches a public
remote.

Run by deploy.sh before every commit. Also useful as a git pre-commit hook:

    ln -s ../../tools/check_secrets.py .git/hooks/pre-commit

Patterns are deliberately broad. A false positive costs ten seconds; a real
coordinate on a public remote is not retractable, because anyone can have
cloned it before you noticed.
"""
import re, subprocess, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Files that are SUPPOSED to hold real values. All gitignored.
ALLOWED = {".env"}

CHECKS = [
    # A home address to four decimals is the most exposing thing here, and
    # the least obvious once it is buried in a config block.
    (r"\b3[0-9]\.\d{3,}\s*,?\s*-1[01][0-9]\.\d{3,}", "latitude/longitude pair"),
    (r"lat\w*\s*[:=]\s*-?\d+\.\d{3,}",               "hard-coded latitude"),
    (r"lon\w*\s*[:=]\s*-?\d+\.\d{3,}",               "hard-coded longitude"),

    # Private IP literals. A LAN address is not a secret, but it is site
    # data and it is what people paste in while debugging.
    (r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b",
     "private IP address"),

    # Hardware identifiers.
    (r"usb-FTDI_[A-Z0-9_]+_[A-Z0-9]{6,}-if\d\d", "FTDI serial number"),

    # Absolute paths naming a person.
    (r"/(?:home|Users)/(?!nobody|user\b)[a-z][a-z0-9_-]{2,}/", "personal home path"),

    # Credentials that should be in .env.
    (r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*[\"'][^\"'{$][^\"']{6,}",
     "literal credential"),
    (r"\bghp_[A-Za-z0-9]{30,}", "GitHub token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
]

# Hostnames need a function, not a bare regex.
#
# Three approaches fail:
#   - stopping at the second label matches "fonts.googleapis" in a CDN URL
#   - a fixed private-TLD list misses .whitehouse, .box, .attlocal
#   - matching any dotted token flags every self.name and Math.max in the repo
#
# So: only look where a hostname can actually appear -- after a scheme, after
# an @, after -h, as a bare host:port, or as an entire quoted string -- then
# judge the last label. That is every form this project uses and none of the
# forms code uses.
HOST = r"[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+"
POSITIONS = [
    re.compile(r"https?://(" + HOST + r")", re.I),          # scheme
    re.compile(r"(?:ssh\s+\S*?|\B)@(" + HOST + r")\b", re.I),  # user@host
    re.compile(r"-h\s+(" + HOST + r")\b", re.I),            # mosquitto -h
    re.compile(r"\b(" + HOST + r"):\d{2,5}\b", re.I),       # host:port
    # A quoted string ONLY when the key names a host. Bare quoted dotted
    # tokens are ambiguous -- logging.getLogger("eg4poll.jbd") and every
    # `bank.current` in a markdown code span look identical to a hostname.
    re.compile(r"(?:host|broker|server|url|addr|endpoint|hostname)"
               r"\s*[:=]\s*['\"](" + HOST + r")['\"]", re.I),
    re.compile(r"(?:^|\s)[A-Z_]*HOST[A-Z_]*\s*=\s*['\"]?(" + HOST + r")"),  # FOO_HOST=
]

PUBLIC_TLDS = {
    "com", "org", "net", "io", "dev", "gov", "edu", "co", "uk", "ca", "de",
    "fr", "jp", "au", "us", "info", "app", "sh", "ai", "cloud", "run", "xyz",
}
FILE_EXT = {
    "json", "yaml", "yml", "html", "htm", "js", "py", "md", "conf", "txt",
    "css", "png", "svg", "log", "sh", "toml", "ini", "lock", "min", "example",
    "gz", "tar", "zip", "csv", "pdf", "jsx", "ts", "cjs", "mjs", "env", "flux",
}


def hostname_hit(line):
    for rx in POSITIONS:
        for m in rx.finditer(line):
            host = m.group(1).rstrip(".")
            last = host.rsplit(".", 1)[1].lower()
            if last in PUBLIC_TLDS or last in FILE_EXT:
                continue
            if len(last) < 3 or not last.isalpha():
                continue
            if host.lower().endswith("example.lan"):
                continue
            return host
    return None


def tracked():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        return [l for l in out.split("\n") if l]
    except Exception:
        return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                if p.is_file() and ".git" not in p.parts]

hits = []
for rel in tracked():
    if rel in ALLOWED or rel.endswith((".png", ".jpg", ".pdf")):
        continue
    p = ROOT / rel
    try:
        text = p.read_text(errors="ignore")
    except Exception:
        continue
    for n, line in enumerate(text.split("\n"), 1):
        # Placeholders are the whole point; do not flag them.
        if re.search(r"example\.(lan|com|org)|X{3,}|REPLACE_WITH|changeme|"
                     r"youruser|yourorg|myorg|<your", line):
            continue
        h = hostname_hit(line)
        if h:
            hits.append((rel, n, "internal hostname", h))
            continue
        for pat, why in CHECKS:
            m = re.search(pat, line)
            if m:
                hits.append((rel, n, why, m.group(0)[:56]))
                break

if hits:
    print("  BLOCKED — site-specific data in tracked files:")
    for rel, n, why, frag in hits:
        print(f"    {rel}:{n}  {why}")
        print(f"        {frag}")
    print("\n  Move it to .env -- gitignored, and the one place site-specific")
    print("  values belong. The Config page (dashboard/config.html) is where")
    print("  device/site settings that used to live in tracked files go now.")
    sys.exit(1)

print(f"  OK — {len(tracked())} tracked files, nothing site-specific")
