"""
The network log an emOS device keeps from an outage: reading a copy
(em_netlast) and collecting it on connect (em_reconcile._collect_net_last).

The copy is written by emos/init/init.c (netlast_save) and read here, so the
format is pinned from both ends: emos/init/netcheck.c checks what the C writes,
and this checks the C still writes what this reads.
"""
import asyncio
import hashlib
import re
from pathlib import Path

import _sources
import em_events
import em_netlast as nl
import em_reconcile
import em_shell

INIT_C = (Path(__file__).resolve().parents[2] / "emos" / "init" / "init.c"
          ).read_text()

# A copy as init writes one: the summary, then the log's tail.
COPY = (
    "# emos-net-last why=watchdog-restart uptime=5123 "
    "wall=2026-01-15T03:04:05Z carrier=1 address=1 gateway=192.168.1.1 "
    "off_network=544 watchdog_steps=3 rx_packets=48211 tx_packets=39020\n"
    "[ 5000123] link watchdog: gateway 192.168.1.1 did not answer ARP, "
    "rx_packets 48200\n"
    "wlan0: CTRL-EVENT-DISCONNECTED bssid=a2:b4:c6:01:02:03 reason=4\n"
    "wlan0: Trying to associate with SSID 'Bob's WiFi'\n"
    "wlan0: Associated with a2:b4:c6:01:02:03\n"
    "udhcpc: lease of 192.168.1.136 obtained from 192.168.1.1, "
    "lease time 864000\n"
)


# ── reading a copy ──────────────────────────────────────────────────────────

def test_a_copy_becomes_one_message():
    msg = nl.summarise(COPY)
    assert msg.startswith(
        "emOS kept its network log: the link watchdog restarted WiFi 5123s "
        "after boot (2026-01-15 03:04:05 UTC), off the network 544s, "
        "3 watchdog step(s), carrier 1, address 1, rx/tx packets "
        "48211/39020. Last 5 line(s):\n")
    assert "CTRL-EVENT-DISCONNECTED" in msg
    assert msg.endswith("lease time 864000")


def test_no_network_identifier_survives():
    """An event reaches every dashboard and support bundle."""
    msg = nl.summarise(COPY)
    for leak in ("192.168.1", "a2:b4:c6", "Bob", "WiFi'"):
        assert leak not in msg, leak


def test_the_gateway_is_never_read():
    assert "gateway" not in nl.parse_summary(COPY.splitlines()[0])


def test_every_value_is_checked_against_its_shape():
    s = nl.parse_summary(
        "# emos-net-last why=carrier-lost uptime=12;reboot wall=yesterday "
        "carrier=2 address=1 off_network=-5 watchdog_steps=x rx_packets=1 "
        "tx_packets=99999999999999999999999")
    assert s["why"] == "carrier-lost"
    assert s["address"] == "1" and s["rx_packets"] == "1"
    for key in ("uptime", "wall", "carrier", "off_network", "watchdog_steps",
                "tx_packets"):
        assert s[key] == "?", key


def test_a_clock_nobody_set_is_left_out():
    msg = nl.summarise(COPY.replace("wall=2026-01-15T03:04:05Z", "wall=unset"))
    assert "5123s after boot, off the network" in msg


def test_a_reason_this_does_not_know_is_shown_as_written():
    msg = nl.summarise(COPY.replace("why=watchdog-restart", "why=new-reason"))
    assert msg.startswith("emOS kept its network log: new-reason 5123s")


def test_what_init_did_not_write_is_not_reported():
    for text in ("", None, "hello\n", "# emos-net-last \nline\n",
                 "# emos-net-last why=; uptime=1\n",
                 COPY.replace("# emos-net-last", "# something-else")):
        assert nl.summarise(text) is None, text


def test_the_message_is_bounded():
    text = COPY.splitlines()[0] + "\n" + "".join(
        f"wlan0: line {i} " + "x" * 300 + "\n" for i in range(500))
    msg = nl.summarise(text)
    assert f"Last {nl.TAIL_LINES} line(s)" in msg
    assert len(msg) < nl.MAX_CHARS + 400
    assert msg.rstrip().endswith("x")


# ── the contract with init.c ────────────────────────────────────────────────

def test_init_writes_the_summary_this_reads():
    assert '"# emos-net-last why=%s uptime=%ld wall=%s carrier=%d address=%d "' \
        in INIT_C
    assert '"gateway=%s off_network=%ld watchdog_steps=%d rx_packets=%llu "' \
        in INIT_C
    assert '"tx_packets=%llu\\n"' in INIT_C
    assert nl.LEAD == "# emos-net-last "
    assert f'#define NETLAST "{nl.NETLAST_PATH}"' in INIT_C


def test_every_reason_init_gives_is_one_this_names():
    events = set(re.findall(r'netlast_event\(&nl, "([a-z-]+)"\)', INIT_C))
    steps = re.search(r"wd_step_name\[\] = \{([^}]*)\}", INIT_C).group(1)
    for step in re.findall(r'"([a-z]+)"', steps):
        if step != "none":
            events.add(f"watchdog-{step}")
    assert len(events) == 5, events
    assert events == set(nl._WHY)


# ── collecting it on connect ────────────────────────────────────────────────

class Device:
    """The device side of the shell plane: files, and every command run."""

    def __init__(self, files=None, silent=False, cut_read=False):
        self.files = dict(files or {})
        self.silent = silent
        self.cut_read = cut_read
        self.commands = []
        self.events = []

    async def run(self, live, cmd, timeout=30.0):
        self.commands.append(cmd)
        if self.silent:
            return ""
        ok = em_shell.OK_MARK + "\n"
        if cmd.startswith("busybox md5sum "):
            out = ""
            body = self.files.get(nl.NETLAST_PATH)
            if body is not None:
                out += (f"{hashlib.md5(body.encode()).hexdigest()}  "
                        f"{nl.NETLAST_PATH}\n")
            return out + self.files.get(nl.SEEN_PATH, "") + ok
        if cmd.startswith(f"cat {nl.NETLAST_PATH};"):
            return self.files[nl.NETLAST_PATH] + ("" if self.cut_read else ok)
        m = re.fullmatch(r"echo ([0-9a-f]{32}) > (\S+)", cmd)
        if m:
            self.files[m.group(2)] = m.group(1) + "\n"
        return ""


def collect(monkeypatch, device):
    async def push_log(device_id, level, source, message):
        device.events.append((device_id, level, source, message))

    async def no_wait(_):
        return None

    monkeypatch.setattr(em_shell, "run", device.run)
    monkeypatch.setattr(em_events, "push_log", push_log)
    monkeypatch.setattr(em_reconcile.asyncio, "sleep", no_wait)
    asyncio.run(em_reconcile._collect_net_last(object(), "dev1"))


def test_a_new_copy_is_reported_once(monkeypatch):
    device = Device({nl.NETLAST_PATH: COPY})
    collect(monkeypatch, device)
    collect(monkeypatch, device)
    assert len(device.events) == 1
    _, level, source, message = device.events[0]
    assert (level, source) == ("warning", "network")
    assert message == nl.summarise(COPY)
    assert device.files[nl.SEEN_PATH].strip() == \
        hashlib.md5(COPY.encode()).hexdigest()


def test_the_marker_is_written_after_the_read(monkeypatch):
    device = Device({nl.NETLAST_PATH: COPY})
    collect(monkeypatch, device)
    kinds = [c.split()[0] + " " + c.split()[1] for c in device.commands]
    assert kinds == ["busybox md5sum", f"cat {nl.NETLAST_PATH};",
                     f"echo {hashlib.md5(COPY.encode()).hexdigest()}"]


def test_a_later_copy_is_reported_too(monkeypatch):
    device = Device({nl.NETLAST_PATH: COPY})
    collect(monkeypatch, device)
    device.files[nl.NETLAST_PATH] = COPY.replace("uptime=5123", "uptime=9000")
    collect(monkeypatch, device)
    assert len(device.events) == 2


def test_no_copy_reads_nothing(monkeypatch):
    device = Device()
    collect(monkeypatch, device)
    assert len(device.commands) == 1 and not device.events
    assert nl.SEEN_PATH not in device.files


def test_a_silent_shell_changes_nothing(monkeypatch):
    device = Device({nl.NETLAST_PATH: COPY}, silent=True)
    collect(monkeypatch, device)
    assert len(device.commands) == 1 and not device.events


def test_a_read_cut_short_is_retried_next_connect(monkeypatch):
    device = Device({nl.NETLAST_PATH: COPY}, cut_read=True)
    collect(monkeypatch, device)
    assert not device.events and nl.SEEN_PATH not in device.files


def test_a_copy_that_is_not_inits_is_marked_and_not_reported(monkeypatch):
    device = Device({nl.NETLAST_PATH: "garbage\n"})
    collect(monkeypatch, device)
    collect(monkeypatch, device)
    assert not device.events
    assert sum(c.startswith("cat ") for c in device.commands) == 1


def test_it_runs_on_connect_after_the_trial_and_before_the_debounce():
    """
    An image on trial has TRIAL_SECS to be confirmed, so the trial check goes
    first; and a device back from an outage is the one reconnecting inside the
    debounce window, so this goes before it, for emOS devices only.
    """
    fn = _sources.code_only(_sources.def_source("em_reconcile.on_connect"))
    at = fn.index("_collect_net_last(")
    assert fn.index("status_on_connect(") < at < fn.index("_due(")
    assert "not live.android_userspace" in fn[:at]
