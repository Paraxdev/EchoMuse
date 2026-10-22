"""
Stand-ins for the parts of Proxmox and Home Assistant OS that tools/fork/
drives, so its scripts run for real against a scratch tree.

`docker` emulates what the haos_*.sh scripts ask of the HAOS host: the
hassio_cli container (the ha CLI, curl to the Supervisor API, jq), `docker ps`,
`docker inspect` and `docker run`. Supervisor is a JSON state file: apps with
state, boot, watchdog, version and options, and the containers that have a
data folder mounted. A local app is "built" by reading the version from its
folder's config.yaml, as Supervisor would. A `docker run` container is its
command run on this host with each mount's target rewritten to its source, on
the PATH in EMF_FAKE_CONTAINER_PATH. `qm` runs `qm guest exec` locally, on
the PATH in EMF_FAKE_GUEST_PATH when that is set, and answers in the JSON
that Proxmox prints. `git` (installed only by the tests that want it) clones
by copying this checkout and hands every other command to the real git.

jq is the real one when it is installed (CI has it) and otherwise a table of
the filters the scripts use, which fails on any it does not know.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

FAKE_DOCKER = r'''#!@PYTHON@
import json, os, re, shutil, subprocess, sys
from pathlib import Path

STATE = Path(os.environ["EMF_FAKE_STATE"])
s = json.loads(STATE.read_text())
apps = s["apps"]
MNT = Path(s["mnt"])
DATA = MNT / "supervisor" / "apps" / "data"
LOCAL = MNT / "supervisor" / "apps" / "local"


def save():
    STATE.write_text(json.dumps(s, indent=1))


def scan_local():
    cfg = LOCAL / "echomuse_fork" / "config.yaml"
    slug = "local_echomuse_fork"
    if not cfg.is_file():
        if not apps.get(slug, {}).get("installed"):
            apps.pop(slug, None)
        return
    text = cfg.read_text()
    ver = re.search(r'^version:\s*"(.*)"\s*$', text, re.M).group(1)
    boot = re.search(r"^boot:\s*(\S+)\s*$", text, re.M)
    app = apps.setdefault(slug, {"installed": False})
    app["version_latest"] = ver
    app["boot_default"] = boot.group(1) if boot else "auto"


def container_id(slug):
    return "c-" + slug


def start(slug):
    app = apps[slug]
    app["state"] = "started"
    (DATA / slug).mkdir(parents=True, exist_ok=True)
    s["containers"][container_id(slug)] = {"name": "/app_" + slug, "mounts": [str(DATA / slug)]}


def stop(slug):
    apps[slug]["state"] = "stopped"
    if slug not in s.get("sticky", []):
        s["containers"].pop(container_id(slug), None)


def ha(args):
    if args[:2] == ["store", "reload"]:
        scan_local()
        s["history"].append(["store", "reload"])
        save()
        return 0
    if args[:2] == ["supervisor", "logs"]:
        print("supervisor: fake log line")
        return 0
    group = args[0]
    if group not in s["ha_groups"]:
        print(f'Error: unknown command "{group}" for "ha"', file=sys.stderr)
        return 1
    if args[1:] == ["--help"]:
        print("usage")
        return 0
    action = args[1]
    if action == "reload":
        scan_local()
        save()
        return 0
    slug = args[2]
    app = apps.get(slug)
    if app is None:
        print(f"Error: app {slug} does not exist", file=sys.stderr)
        return 1
    if action in s.get("fail", []):
        print(f"Error: {action} failed (injected)", file=sys.stderr)
        return 1
    s["history"].append([action, slug])
    if action == "info":
        print(f"slug: {slug}\nstate: {app.get('state', 'unknown')}\nversion: {app.get('version')}")
        return 0
    if action == "logs":
        print("Opening database: /data/echomuse.db\nEchoMuse Controller ready")
        return 0
    if action == "install":
        if app.get("installed"):
            print("Error: already installed", file=sys.stderr)
            return 1
        app.update(installed=True, version=app["version_latest"], state="stopped",
                   boot=app.get("boot_default", "auto"), options={}, ingress_panel=False)
        (DATA / slug).mkdir(parents=True, exist_ok=True)
    elif not app.get("installed"):
        print(f"Error: {slug} is not installed", file=sys.stderr)
        return 1
    elif action == "update":
        if app["version"] == app.get("version_latest"):
            print("Error: no update available", file=sys.stderr)
            return 1
        app["version"] = app["version_latest"]
    elif action == "rebuild":
        if app["version"] != app.get("version_latest"):
            print("Error: Version changed, use Update instead Rebuild", file=sys.stderr)
            return 1
    elif action == "start":
        start(slug)
    elif action == "stop":
        stop(slug)
    else:
        print(f"Error: fake ha has no {action}", file=sys.stderr)
        return 1
    save()
    return 0


def api(method, path, body):
    for prefix in s["api_prefixes"]:
        if path == prefix or path.startswith(prefix + "/"):
            rest = path[len(prefix):]
            break
    else:
        return 404, {"result": "error", "message": "404: Not Found"}
    if rest == "" and method == "GET":
        return 200, {"result": "ok", "data": {"addons": [
            {"slug": k} for k, a in apps.items() if a.get("installed")]}}
    m = re.fullmatch(r"/([^/]+)/(info|options)", rest)
    if not m or m.group(1) not in apps:
        return 404, {"result": "error", "message": "App does not exist"}
    slug, what = m.groups()
    app = apps[slug]
    if what == "info" and method == "GET":
        inst = app.get("installed")
        return 200, {"result": "ok", "data": {
            "slug": slug,
            "state": app.get("state") if inst else "unknown",
            "boot": app.get("boot", app.get("boot_default")),
            "version": app.get("version") if inst else None,
            "version_latest": app.get("version_latest"),
            "options": app.get("options", {}),
            "ingress_panel": app.get("ingress_panel", False),
            "watchdog": app.get("watchdog", False),
        }}
    if what == "options" and method == "POST":
        if not app.get("installed"):
            return 400, {"result": "error", "message": "App is not installed"}
        data = json.loads(body)
        if set(data) - {"boot", "options", "ingress_panel", "watchdog"}:
            return 400, {"result": "error", "message": "extra keys not allowed"}
        if "boot" in data and data["boot"] not in ("auto", "manual"):
            return 400, {"result": "error", "message": "bad boot"}
        if "options" in data and s.get("schema") and set(data["options"]) - set(s["schema"]):
            return 400, {"result": "error", "message": "unknown option"}
        for k in ("boot", "options", "ingress_panel", "watchdog"):
            if k in data:
                app[k] = data[k]
        s["history"].append(["options", slug, data])
        save()
        return 200, {"result": "ok", "data": {}}
    return 405, {"result": "error", "message": "method"}


JQ = {
    '.data.addons[]?.slug, .data.apps[]?.slug':
        lambda d: [a["slug"] for a in (d["data"].get("addons") or [])]
                  + [a["slug"] for a in (d["data"].get("apps") or [])],
    '.data.options | tojson': lambda d: [json.dumps(d["data"]["options"])],
    '.data.options | to_entries | sort_by(.key) | tojson':
        lambda d: [json.dumps([{"key": k, "value": v} for k, v in sorted(d["data"]["options"].items())])],
    '{options: .data.options} + (.data | with_entries(select((.key == "ingress_panel" or .key == "watchdog") and (.value | type) == "boolean"))) | tojson':
        lambda d: [json.dumps({"options": d["data"]["options"],
                               **{k: d["data"][k] for k in ("ingress_panel", "watchdog")
                                  if isinstance(d["data"].get(k), bool)}})],
    '"state \\(.data.state), boot \\(.data.boot), version \\(.data.version)"':
        lambda d: ["state %s, boot %s, version %s" % tuple(
            "null" if d["data"].get(k) is None else d["data"][k] for k in ("state", "boot", "version"))],
}


def jq(flt, text):
    if shutil.which("jq") and not os.environ.get("EMF_FAKE_NO_JQ"):
        return subprocess.run(["jq", "-r", flt], input=text.encode()).returncode
    d = json.loads(text)
    m = re.fullmatch(r"\.data\.(\w+)", flt)
    if m:
        out = [d["data"].get(m.group(1))]
    elif flt in JQ:
        out = JQ[flt](d)
    else:
        print(f"fake jq: unknown filter {flt!r}", file=sys.stderr)
        return 5
    for v in out:
        if v is None:
            print("null")
        elif isinstance(v, bool):
            print("true" if v else "false")
        elif isinstance(v, (dict, list)):
            print(json.dumps(v, indent=2))
        else:
            print(v)
    return 0


def run(args):
    mounts, entry, i = [], None, 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--rm":
            i += 1
        elif args[i] in ("-v", "--network", "--entrypoint"):
            if args[i] == "-v":
                src, dst = args[i + 1].split(":", 1)
                mounts.append((src, dst))
            elif args[i] == "--entrypoint":
                entry = args[i + 1]
            i += 2
        else:
            print(f"fake docker: run option {args[i]} not emulated", file=sys.stderr)
            return 1
    image, rest = args[i], args[i + 1:]
    s["history"].append(["run", image])
    save()

    def host(x):
        for src, dst in mounts:
            if src != dst:
                x = x.replace(dst + "/", src + "/")
        return x
    env = dict(os.environ, PATH=os.environ.get("EMF_FAKE_CONTAINER_PATH", os.environ["PATH"]))
    return subprocess.run([entry] + [host(x) for x in rest], env=env).returncode


def main(argv):
    cmd = argv[0]
    if cmd == "run":
        return run(argv[1:])
    if cmd == "ps":
        for cid in s["containers"]:
            print(cid)
        return 0
    if cmd == "inspect":
        fmt = argv[argv.index("--format") + 1]
        targets = [a for a in argv[1:] if a not in ("--format", fmt)]
        for t in targets:
            if t == "hassio_cli":
                if fmt == "{{.State.Running}}":
                    print("true" if s.get("cli_running", True) else "false")
                elif fmt == "{{.Config.Image}}":
                    print("ghcr.io/home-assistant/amd64-hassio-cli:2026.10.0")
            elif t in s["containers"]:
                c = s["containers"][t]
                print(c["name"] + "|" + "".join(m + ";" for m in c["mounts"]))
            else:
                print(f"Error: No such object: {t}", file=sys.stderr)
                return 1
        return 0
    if cmd == "exec":
        rest = argv[1:]
        if rest and rest[0] == "-i":
            rest = rest[1:]
        if rest[0] != "hassio_cli":
            print("fake docker: exec into an unknown container", file=sys.stderr)
            return 1
        rest = rest[1:]
        if rest[0] == "ha":
            return ha(rest[1:])
        if rest[0] == "jq":
            return jq(rest[-1], sys.stdin.read())
        if rest[:2] == ["sh", "-c"]:
            script = rest[2]
            if "command -v curl" in script:
                return 0 if s.get("cli_ok", True) else 1
            if script.startswith("curl"):
                method, path = rest[4], rest[5]
                body = sys.stdin.read() if "--data-binary" in script else None
                code, resp = api(method, path, body)
                print(json.dumps(resp))
                print(code)
                return 0
        print(f"fake docker: exec not emulated: {rest!r}", file=sys.stderr)
        return 1
    print(f"fake docker: {cmd} not emulated", file=sys.stderr)
    return 1


sys.exit(main(sys.argv[1:]))
'''

FAKE_QM = r'''#!@PYTHON@
import json, os, subprocess, sys
from pathlib import Path

a = sys.argv[1:]
log = os.environ.get("EMF_FAKE_QM_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(a) + "\n")
if a[:1] == ["status"]:
    print("status: running")
    sys.exit(0)
if a[:2] == ["guest", "cmd"]:
    print("{}")
    sys.exit(0)
if a[:2] != ["guest", "exec"]:
    sys.exit(f"fake qm: {a!r} not emulated")
rest = a[3:]
stdin = False
while rest and rest[0] != "--":
    if rest[0] == "--timeout":
        rest = rest[2:]
    elif rest[0] == "--pass-stdin":
        stdin = rest[1] == "1"
        rest = rest[2:]
    else:
        sys.exit(f"fake qm: unknown option {rest[0]}")
cmd = rest[1:]
data = sys.stdin.buffer.read() if stdin else None
if data is not None and len(data) > 1024 * 1024:
    sys.stderr.write("'input-data' (STDIN) is limited to 1 MiB, aborting\n")
    sys.exit(255)
once = os.environ.get("EMF_FAKE_QM_CORRUPT")
if data and once and not Path(once).exists():
    Path(once).write_text("done")
    i = data.index(b"A")
    data = data[:i] + b"B" + data[i + 1:]
env = dict(os.environ)
if os.environ.get("EMF_FAKE_GUEST_PATH"):
    env["PATH"] = os.environ["EMF_FAKE_GUEST_PATH"]
p = subprocess.run(cmd, input=data, capture_output=True, env=env)
r = {"exitcode": p.returncode, "exited": 1}
if p.stdout:
    r["out-data"] = p.stdout.decode("utf-8", "replace")
if p.stderr:
    r["err-data"] = p.stderr.decode("utf-8", "replace")
print(json.dumps(r, indent=3, sort_keys=True))
'''

FAKE_GIT = r'''#!@PYTHON@
import json, os, shutil, sys
from pathlib import Path

a = sys.argv[1:]
if a[:1] == ["clone"]:
    i = a.index("--")
    url, dest = a[i + 1], Path(a[i + 2])
    log = os.environ.get("EMF_FAKE_GIT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(a) + "\n")
    repo = Path(os.environ["EMF_FAKE_REPO"])
    for part in ("tools/fork", "controller", "controller-ea"):
        shutil.copytree(repo / part, dest / part,
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "data", ".venv"))
    sys.exit(0)
real = os.environ.get("EMF_REAL_GIT")
if not real:
    sys.exit(1)
os.execv(real, [real] + a)
'''

UPSTREAM = "acc06409_controller-ea"
FORK = "local_echomuse_fork"

UPSTREAM_OPTIONS = {
    "server_host": "0.0.0.0", "server_port": 8767, "server_tls_port": 8770,
    "server_ip": "192.168.1.69", "mdns_name": "echomuse",
    "esphome_project_version": "2.0.0", "esphome_port_base": 16101,
    "oww_model": "hey_jarvis", "oww_threshold": 0.5, "require_device_tls": False,
    "device_approval": "strict", "debug": True, "log_levels": "echomuse.esphome=DEBUG",
    "extra_ca_cert": "", "tts_wait_timeout": 60,
}

UPSTREAM_FILES = {
    "echomuse.db": b"SQLite format 3\0" + os.urandom(4096),
    "echomuse.db-wal": os.urandom(8192),
    "echomuse.db-shm": os.urandom(1024),
    "options.json": json.dumps(UPSTREAM_OPTIONS).encode(),
    "tls/ca.key": b"ca key", "tls/ca.pem": b"ca pem",
    "tls/server.key": b"server key", "tls/server.pem": b"server pem",
    "oww_models/mywake_20260101_120000.onnx": os.urandom(2048),
    "firmware/server_b": os.urandom(4096),
    "recordings/a wake clip.wav": os.urandom(512),
}


def _write_exe(path: Path, text: str) -> None:
    path.write_text(text.replace("@PYTHON@", sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Sim:
    """A Proxmox host and a Home Assistant OS VM in a directory."""

    def __init__(self, root: Path, schema_keys):
        self.root = root
        self.mnt = root / "mnt" / "data"
        self.data = self.mnt / "supervisor" / "apps" / "data"
        self.local = self.mnt / "supervisor" / "apps" / "local"
        self.work = self.mnt / "echomuse_fork"
        self.bin = root / "fakebin"
        for d in (self.data, self.local, self.bin):
            d.mkdir(parents=True)
        self.state_path = root / "supervisor.json"
        self.qm_log = root / "qm.log"
        _write_exe(self.bin / "docker", FAKE_DOCKER)
        _write_exe(self.bin / "qm", FAKE_QM)
        up = self.data / UPSTREAM
        for rel, content in UPSTREAM_FILES.items():
            (up / rel).parent.mkdir(parents=True, exist_ok=True)
            (up / rel).write_bytes(content)
        self.save({
            "mnt": str(self.mnt),
            "api_prefixes": ["/addons"],
            "ha_groups": ["apps", "addons"],
            "schema": sorted(schema_keys),
            "apps": {UPSTREAM: {
                "installed": True, "state": "started", "boot": "auto",
                "version": "2.27.0-ea.1", "version_latest": "2.27.0-ea.1",
                "options": UPSTREAM_OPTIONS, "ingress_panel": True, "watchdog": True,
            }},
            "containers": {"c-" + UPSTREAM: {"name": "/app_" + UPSTREAM, "mounts": [str(up)]}},
            "history": [],
        })

    def clone(self, root: Path) -> "Sim":
        """A copy at another path, so one deploy can serve many tests."""
        shutil.copytree(self.root, root, symlinks=True)
        other = object.__new__(Sim)
        other.__dict__.update({k: Path(str(v).replace(str(self.root), str(root)))
                               for k, v in self.__dict__.items()})
        other.state_path.write_text(other.state_path.read_text().replace(
            json.dumps(str(self.root))[1:-1], json.dumps(str(root))[1:-1]))
        if other.qm_log.exists():
            other.qm_log.unlink()
        return other

    def load(self) -> dict:
        return json.loads(self.state_path.read_text())

    def save(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, indent=1))

    def update(self, fn) -> None:
        st = self.load()
        fn(st)
        self.save(st)

    def app(self, slug: str) -> dict:
        return self.load()["apps"].get(slug, {})

    def install_git(self, repo: Path) -> dict:
        """Put the stand-in git on this simulation's PATH; returns the env it
        reads. A clone copies `repo`, so nothing is fetched."""
        real = shutil.which("git") or ""
        _write_exe(self.bin / "git", FAKE_GIT)
        return {"EMF_FAKE_REPO": str(repo), "EMF_REAL_GIT": real,
                "EMF_FAKE_GIT_LOG": str(self.root / "git.log")}

    def env(self, path_dirs=None, **extra) -> dict:
        env = dict(os.environ)
        base = path_dirs if path_dirs is not None else env.get("PATH", "")
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{base}",
            "EMF_MNT": str(self.mnt),
            "EMF_GUEST_WORK": str(self.work),
            "EMF_POLL": "0",
            "EMF_FAKE_STATE": str(self.state_path),
            "EMF_FAKE_QM_LOG": str(self.qm_log),
        })
        env.update(extra)
        return env


def deploy_mode(repo: Path) -> list:
    """`pve.sh deploy`'s build flags for `repo`: none while its image inputs
    match the overlay's base, else --full. build_app.sh refuses the overlay
    once they move, which an upstream merge does routinely, and the tests
    that deploy are not about that."""
    build = repo / "tools" / "fork" / "build_app.sh"
    have = subprocess.run(["sh", str(build), "--fingerprint"],
                          capture_output=True, text=True, check=True).stdout.strip()
    pinned = re.search(r'^BASE_FINGERPRINT="([0-9a-f]{64})"$', build.read_text(), re.M).group(1)
    return [] if have == pinned else ["--full"]


def path_without(root: Path, *names: str) -> Path:
    """A directory linking every command on PATH except `names`, to stand in
    for a host that lacks them."""
    d = root / "path-without"
    d.mkdir()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            target = os.path.join(directory, entry)
            link = d / entry
            if entry in names or os.path.lexists(link):
                continue
            if os.path.isfile(target) and os.access(target, os.X_OK):
                link.symlink_to(target)
    return d


def tree(path: Path) -> dict:
    """{relative path: bytes} for every file below path."""
    return {str(p.relative_to(path)).replace(os.sep, "/"): p.read_bytes()
            for p in sorted(path.rglob("*")) if p.is_file()}


def busybox_path(root: Path):
    """A directory of busybox applets, or None without busybox. Ubuntu's
    busybox has no base64 applet; the scripts under test do not use it."""
    real = shutil.which("busybox")
    if real is None:
        return None
    d = root / "busybox-bin"
    d.mkdir()
    applets = subprocess.run([real, "--list"], capture_output=True, text=True).stdout.split()
    for name in applets:
        (d / name).symlink_to(real)
    return d
