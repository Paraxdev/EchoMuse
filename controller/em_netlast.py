"""
Read the network log an emOS device kept from an outage.

emOS init keeps its network log on tmpfs, and pulling the plug is how people
recover an Echo that has dropped off the network, so an outage nobody watched
used to leave nothing to read. Since the link watchdog (emos/init/init.c,
netlast_save), a link loss or a watchdog step copies the log's last 32KB to
/data/emos/net.last behind a one-line summary. This turns one copy into a
device log event; em_reconcile collects it on connect, once per copy.

Pure, so it is testable without a device. The summary's format is init's,
pinned from this side by tests/test_netlast.py and from the C side by
emos/init/netcheck.c.
"""
from __future__ import annotations

import re

import em_support

NETLAST_PATH = "/data/emos/net.last"
SEEN_PATH = "/data/emos/net.last.seen"

LEAD = "# emos-net-last "
TAIL_LINES = 40
MAX_CHARS = 6000

_PAIR = re.compile(r"([a-z_]+)=(\S+)")

# What each summary value may be. Anything else is shown as "?": the file is
# written by the device, and an event reaches every dashboard and support
# bundle, so nothing from it is passed through unchecked.
_SHAPES = {
    "why": re.compile(r"[a-z][a-z-]{0,40}"),
    "uptime": re.compile(r"\d{1,12}"),
    "wall": re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ|unset"),
    "carrier": re.compile(r"[01]"),
    "address": re.compile(r"[01]"),
    "off_network": re.compile(r"\d{1,12}"),
    "watchdog_steps": re.compile(r"\d{1,6}"),
    "rx_packets": re.compile(r"\d{1,20}"),
    "tx_packets": re.compile(r"\d{1,20}"),
}

_WHY = {
    "carrier-lost": "the link lost carrier",
    "address-lost": "the link lost its address",
    "watchdog-reassociate": "the link watchdog reassociated",
    "watchdog-reconnect": "the link watchdog reconnected",
    "watchdog-restart": "the link watchdog restarted WiFi",
}


def parse_summary(line: str) -> dict[str, str]:
    """
    The summary line's values, each checked against its shape. Empty for a
    line that is not init's summary. The gateway is left out: it is a network
    address, which no event carries.
    """
    if not line.startswith(LEAD):
        return {}
    found = dict(_PAIR.findall(line[len(LEAD):]))
    out = {}
    for key, shape in _SHAPES.items():
        value = found.get(key, "?")
        out[key] = value if shape.fullmatch(value) else "?"
    return out


def summarise(text: str) -> str | None:
    """
    A log message for one copy, or None when it is not one init wrote. The
    log lines are redacted as the provisioning diagnostics' net_log is
    (em_support), since the supplicant names networks.
    """
    lines = (text or "").splitlines()
    if not lines:
        return None
    s = parse_summary(lines[0])
    if not s or s["why"] == "?":
        return None
    why = _WHY.get(s["why"], s["why"])
    when = f"{s['uptime']}s after boot"
    if s["wall"] not in ("?", "unset"):
        when += f" ({s['wall'].replace('T', ' ').rstrip('Z')} UTC)"
    tail = em_support._probe_net_log("\n".join(lines[1:]))[-TAIL_LINES:]
    body = "\n".join(tail)
    if len(body) > MAX_CHARS:
        body = "…(truncated)\n" + body[-MAX_CHARS:]
    return (f"emOS kept its network log: {why} {when}, off the network "
            f"{s['off_network']}s, {s['watchdog_steps']} watchdog step(s), "
            f"carrier {s['carrier']}, address {s['address']}, "
            f"rx/tx packets {s['rx_packets']}/{s['tx_packets']}. "
            f"Last {len(tail)} line(s):\n{body}")
