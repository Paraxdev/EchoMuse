"""
tools/fork/ run for real against a simulated Proxmox host and Home Assistant
OS (_fork_fakes.py): deploy, migrate, rollback, and the refusals that keep
the upstream app's data safe. These scripts act on the one database the
user's Echo depends on, through `qm guest exec`, and nothing else exercises
them before they do.

Under CI the migration and rollback also run with busybox's applets and
nothing else on PATH, which is what the Home Assistant OS host has.
"""

import functools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import _fork_fakes as ff

REPO = Path(__file__).resolve().parents[2]
FORK_TOOLS = REPO / "tools" / "fork"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None, reason="needs a POSIX sh")


def _schema_keys():
    return yaml.safe_load((REPO / "controller-ea" / "config.yaml").read_text(encoding="utf-8"))["schema"]


@pytest.fixture
def sim(tmp_path):
    return ff.Sim(tmp_path / "sim", _schema_keys())


@pytest.fixture(scope="module")
def _deployed_once(tmp_path_factory):
    s = ff.Sim(tmp_path_factory.mktemp("deployed") / "sim", _schema_keys())
    deploy(s)
    return s


@pytest.fixture
def deployed(_deployed_once, tmp_path):
    """A simulation after `pve.sh deploy`: the fork installed and stopped."""
    return _deployed_once.clone(tmp_path / "sim")


def run(sim, script, *args, env=None, check=True):
    p = subprocess.run(["sh", str(script), *args], env=env or sim.env(EMF_CHUNK="200000"),
                       capture_output=True, text=True, timeout=600)
    if check and p.returncode != 0:
        raise AssertionError(f"{script.name} {' '.join(args)} exited {p.returncode}\n"
                             f"--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}")
    return p


def pve(sim, *args, **kw):
    return run(sim, FORK_TOOLS / "pve.sh", *args, **kw)


@functools.lru_cache(maxsize=None)
def _mode():
    return tuple(ff.deploy_mode(REPO))


def deploy(sim, *args, **kw):
    return pve(sim, "deploy", *_mode(), *args, **kw)


def history(sim):
    return sim.load()["history"]


def upstream_tree(sim):
    return ff.tree(sim.data / ff.UPSTREAM)


def fork_tree(sim):
    t = ff.tree(sim.data / ff.FORK)
    t.pop(".echomuse_fork_migrated", None)
    return t


def backups(sim):
    d = sim.work / "backups"
    return sorted(d.iterdir()) if d.is_dir() else []


def test_deploy_installs_the_fork_stopped_and_leaves_upstream_running(sim):
    deploy(sim)

    fork = sim.app(ff.FORK)
    assert fork["installed"] and fork["state"] == "stopped"
    assert fork["boot"] == "manual", "a fresh install must not start at boot beside upstream"
    folder = sim.local / "echomuse_fork"
    assert f'version: "{fork["version"]}"' in (folder / "config.yaml").read_text()
    assert (folder / ".echomuse_fork_app").is_file()
    for script in ("haos_lib.sh", "haos_migrate.sh", "haos_rollback.sh", "haos_status.sh"):
        assert (sim.work / "tools" / script).is_file()

    up = sim.app(ff.UPSTREAM)
    assert (up["state"], up["boot"]) == ("started", "auto")
    assert ["install", ff.FORK] in history(sim)

    calls = [json.loads(line) for line in sim.qm_log.read_text().splitlines()]
    assert sum("--pass-stdin" in c for c in calls) >= 2, "the package should have gone in chunks"


def test_deploy_again_rebuilds_the_same_version_and_updates_a_new_one(sim):
    deploy(sim)
    deploy(sim)
    assert ["rebuild", ff.FORK] in history(sim)

    sim.update(lambda st: st["apps"][ff.FORK].update(version="2.26.0-old"))
    deploy(sim)
    assert ["update", ff.FORK] in history(sim)
    assert sim.app(ff.FORK)["version"] == sim.app(ff.FORK)["version_latest"]


def test_a_damaged_transfer_installs_nothing(sim, tmp_path):
    p = deploy(sim, env=sim.env(EMF_CHUNK="200000",
                                EMF_FAKE_QM_CORRUPT=str(tmp_path / "corrupted")), check=False)
    assert p.returncode != 0
    assert "damaged" in p.stderr
    assert not sim.app(ff.FORK).get("installed")
    assert not (sim.local / "echomuse_fork").exists()


def test_deploy_will_not_replace_a_folder_it_did_not_write(sim):
    mine = sim.local / "echomuse_fork"
    mine.mkdir()
    (mine / "config.yaml").write_text('slug: "echomuse_fork"\nversion: "hand-made"\n')
    p = deploy(sim, check=False)
    assert p.returncode != 0
    assert "not written by these tools" in p.stderr
    assert (mine / "config.yaml").read_text().endswith('"hand-made"\n')
    assert not (sim.work / "tools").exists(), "nothing is installed before the refusal"


def test_migrate_copies_the_whole_data_folder_and_switches_the_apps(deployed):
    sim = deployed
    before = upstream_tree(sim)

    p = pve(sim, "migrate")

    up, fork = sim.app(ff.UPSTREAM), sim.app(ff.FORK)
    assert (up["state"], up["boot"]) == ("stopped", "manual")
    assert (fork["state"], fork["boot"]) == ("started", "auto")
    assert fork["options"] == ff.UPSTREAM_OPTIONS
    assert fork["ingress_panel"] is True
    assert fork["watchdog"] is True, "a fork without the upstream's watchdog stays down after a crash"

    assert upstream_tree(sim) == before, "the upstream data must only be read"
    assert fork_tree(sim) == before
    for name in ("echomuse.db", "echomuse.db-wal", "echomuse.db-shm", "tls/ca.key",
                 "tls/server.pem", "oww_models/mywake_20260101_120000.onnx", "firmware/server_b"):
        assert name in fork_tree(sim)

    [bak] = backups(sim)
    assert (bak / ff.FORK).is_dir(), "the fork's previous data folder is kept"

    hist = history(sim)
    assert hist.index(["stop", ff.UPSTREAM]) < hist.index(["start", ff.FORK])
    boots = [(h[1], h[2]["boot"]) for h in hist if h[0] == "options" and "boot" in h[2]]
    assert boots.index((ff.UPSTREAM, "manual")) < boots.index((ff.FORK, "auto"))
    assert "Check, in this order" in p.stdout
    assert not (sim.work / "lock").exists(), "a finished run releases its lock"


def test_migrate_refuses_while_anything_has_the_upstream_data_mounted(deployed):
    sim = deployed
    before = upstream_tree(sim)
    # Supervisor reports it stopped, but its container is still up.
    sim.update(lambda st: st.setdefault("sticky", []).append(ff.UPSTREAM))

    p = pve(sim, "migrate", check=False)

    assert p.returncode != 0
    assert "still mounted" in p.stderr
    assert "rollback" in p.stderr, "a failure says how to get back"
    assert not (sim.data / ff.FORK / ".echomuse_fork_migrated").exists()
    assert backups(sim) == []
    assert upstream_tree(sim) == before
    assert sim.app(ff.FORK)["state"] == "stopped"


def test_migrate_refuses_before_stopping_anything_when_the_fork_is_not_installed(sim):
    p = run(sim, FORK_TOOLS / "haos_migrate.sh", check=False)
    assert p.returncode != 0
    assert "not installed" in p.stderr
    assert sim.app(ff.UPSTREAM)["state"] == "started"


def test_migrate_twice_does_not_copy_over_what_the_fork_wrote(deployed):
    sim = deployed
    pve(sim, "migrate")
    (sim.data / ff.FORK / "written-by-the-fork").write_text("keep me")

    pve(sim, "migrate")

    assert (sim.data / ff.FORK / "written-by-the-fork").read_text() == "keep me"
    assert len(backups(sim)) == 1


def test_after_a_rollback_migrate_asks_whose_data_wins(deployed):
    sim = deployed
    pve(sim, "migrate")
    (sim.data / ff.FORK / "written-by-the-fork").write_text("fork")
    pve(sim, "rollback")

    up, fork = sim.app(ff.UPSTREAM), sim.app(ff.FORK)
    assert (up["state"], up["boot"]) == ("started", "auto")
    assert (fork["state"], fork["boot"]) == ("stopped", "manual")

    def upstream_writes():
        marker = (sim.data / ff.FORK / ".echomuse_fork_migrated").stat().st_mtime
        db = sim.data / ff.UPSTREAM / "echomuse.db"
        db.write_bytes(db.read_bytes() + b"new turn")
        os.utime(db, (marker + 60, marker + 60))

    upstream_writes()
    p = pve(sim, "migrate", check=False)
    assert p.returncode != 0
    assert "--recopy" in p.stderr and "--keep-fork-data" in p.stderr
    assert sim.app(ff.UPSTREAM)["state"] == "started", "nothing is stopped before the owner decides"

    pve(sim, "migrate", "--keep-fork-data")
    assert (sim.data / ff.FORK / "written-by-the-fork").read_text() == "fork"
    assert len(backups(sim)) == 1

    pve(sim, "rollback")
    upstream_writes()
    pve(sim, "migrate", "--recopy")
    assert fork_tree(sim) == upstream_tree(sim)
    assert len(backups(sim)) == 2
    assert (backups(sim)[-1] / ff.FORK / "written-by-the-fork").read_text() == "fork"


def test_keep_fork_data_refuses_when_nothing_was_migrated(deployed):
    """Keeping the fork's data means keeping what a migration copied; with
    no migration it would start the fork on an empty database, with a CA no
    Echo holds."""
    sim = deployed
    p = pve(sim, "migrate", "--keep-fork-data", check=False)
    assert p.returncode != 0
    assert "no migration record" in p.stderr
    assert sim.app(ff.UPSTREAM)["state"] == "started"
    assert backups(sim) == []


def _dead_pid():
    p = subprocess.Popen(["sh", "-c", "exit 0"])
    p.wait()
    return p.pid


def test_a_second_run_waits_for_the_first_to_end(deployed):
    """pve.sh shows a run's output only at its end and interrupting it
    leaves the run going in the VM, so a second run must not act beside it."""
    sim = deployed
    lock = sim.work / "lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")

    for script in ("migrate", "rollback"):
        p = pve(sim, script, check=False)
        assert p.returncode != 0, script
        assert "still running" in p.stderr, script
    assert sim.app(ff.UPSTREAM)["state"] == "started"
    assert sim.app(ff.FORK)["state"] == "stopped"
    assert backups(sim) == []
    assert (lock / "pid").read_text().strip() == str(os.getpid()), "another run's lock is left alone"

    (lock / "pid").write_text(f"{_dead_pid()}\n")
    p = pve(sim, "migrate")
    assert "no longer running" in p.stderr
    assert sim.app(ff.FORK)["state"] == "started"
    assert not lock.exists()


def test_deploy_from_git_clones_and_builds_inside_the_vm(sim):
    url, ref = "https://github.com/Paraxdev/EchoMuse", "fork/forkops"
    env = sim.env(EMF_CHUNK="200000", **sim.install_git(REPO))

    deploy(sim, "--git", url, "--ref", ref, env=env)

    [clone] = [json.loads(line) for line in (sim.root / "git.log").read_text().splitlines()]
    assert clone[-2:] == [url, str(sim.work / "incoming" / "src")]
    assert clone[clone.index("--branch") + 1] == ref
    assert any(h[0] == "run" and h[1].startswith("alpine/git:") and "@sha256:" in h[1]
               for h in history(sim))
    marker = (sim.local / "echomuse_fork" / ".echomuse_fork_app").read_text()
    assert ("mode=full" if _mode() else "mode=overlay") in marker
    fork = sim.app(ff.FORK)
    assert fork["installed"] and fork["state"] == "stopped" and fork["boot"] == "manual"
    assert (sim.work / "tools" / "haos_migrate.sh").is_file()
    assert not (sim.work / "incoming" / "src").exists(), "the clone is removed after the build"
    calls = [json.loads(line) for line in sim.qm_log.read_text().splitlines()]
    assert not any("--pass-stdin" in c for c in calls), "nothing is sent from this host"


def test_deploy_unpacks_in_a_container_when_the_vm_has_no_base64(sim, tmp_path):
    guest = ff.path_without(tmp_path, "base64")
    env = sim.env(EMF_CHUNK="200000",
                  EMF_FAKE_GUEST_PATH=f"{sim.bin}{os.pathsep}{guest}",
                  EMF_FAKE_CONTAINER_PATH=os.environ["PATH"])

    deploy(sim, env=env)

    assert any(h[0] == "run" and "hassio-cli" in h[1] for h in history(sim))
    assert sim.app(ff.FORK)["installed"]


def test_rollback_changes_neither_data_folder(deployed):
    sim = deployed
    pve(sim, "migrate")
    up, fork = upstream_tree(sim), ff.tree(sim.data / ff.FORK)

    pve(sim, "rollback")

    assert upstream_tree(sim) == up
    assert ff.tree(sim.data / ff.FORK) == fork


def test_migrate_starts_the_fork_once_when_supervisor_made_no_data_folder(deployed):
    sim = deployed
    shutil.rmtree(sim.data / ff.FORK)

    pve(sim, "migrate")

    hist = history(sim)
    first_start = hist.index(["start", ff.FORK])
    assert ["stop", ff.FORK] in hist[first_start:]
    assert fork_tree(sim) == upstream_tree(sim)


def test_status_reports_both_apps(deployed):
    sim = deployed
    pve(sim, "migrate")
    p = pve(sim, "status", "--logs", "5")
    assert f"{ff.FORK}: state started, boot auto" in p.stdout
    assert f"{ff.UPSTREAM}: state stopped, boot manual" in p.stdout
    assert "migrated: from=" in p.stdout
    assert "Opening database" in p.stdout


def test_the_renamed_api_and_the_old_cli_group_both_work(sim):
    sim.update(lambda st: st.update(api_prefixes=["/apps"], ha_groups=["addons"]))
    deploy(sim)
    pve(sim, "migrate")
    assert sim.app(ff.FORK)["state"] == "started"


def test_dry_run_sends_nothing(sim):
    p = deploy(sim, "--dry-run")
    assert "nothing was sent" in p.stdout
    assert not sim.qm_log.exists()


def _busybox(tmp_path):
    bb = ff.busybox_path(tmp_path)
    if bb is None:
        if os.environ.get("CI"):
            raise RuntimeError("busybox is required under CI for the Home Assistant OS scripts")
        pytest.skip("needs busybox on the host")
    return bb


def test_deploy_under_busybox(sim, tmp_path):
    """The VM side of a deploy (unpack, install) on busybox alone. With no
    base64 applet, as on Ubuntu's busybox, the unpack runs in the container."""
    bb = _busybox(tmp_path)
    env = sim.env(EMF_CHUNK="200000",
                  EMF_FAKE_GUEST_PATH=f"{sim.bin}{os.pathsep}{bb}",
                  EMF_FAKE_CONTAINER_PATH=os.environ["PATH"])
    deploy(sim, env=env)
    assert sim.app(ff.FORK)["installed"]
    assert (sim.local / "echomuse_fork" / ".echomuse_fork_app").is_file()


def test_migrate_and_rollback_under_busybox(deployed, tmp_path):
    bb = _busybox(tmp_path)
    sim = deployed
    tools = sim.work / "tools"
    env = sim.env(path_dirs=str(bb))

    run(sim, tools / "haos_migrate.sh", env=env)
    assert fork_tree(sim) == upstream_tree(sim)
    assert sim.app(ff.FORK)["state"] == "started"

    run(sim, tools / "haos_rollback.sh", env=env)
    assert sim.app(ff.UPSTREAM)["state"] == "started"
    run(sim, tools / "haos_status.sh", env=env)


def _snippet(lang):
    src = (FORK_TOOLS / "pve.sh").read_text()
    m = re.search(rf"{lang} (?:-MJSON::PP )?-[ce] '\n(.*?)\n'", src, re.S)
    return m.group(1)


QM_OUTPUTS = [
    (json.dumps({"exitcode": 0, "exited": 1, "out-data": "hello\n"}), 0, "hello\n"),
    (json.dumps({"exitcode": 3, "exited": 1, "out-data": "x", "err-data": "boom\n"}), 3, "x"),
    (json.dumps({"exitcode": 0, "exited": 1}), 0, ""),
    (json.dumps({"pid": 4242}), 124, ""),
    ("timeout reached, returning pid", 125, ""),
]


@pytest.mark.parametrize("raw,code,out", QM_OUTPUTS)
def test_pve_reads_the_guest_exit_code_from_qm_output(raw, code, out):
    """qm guest exec exits 0 whatever the command did; pve.sh must not."""
    p = subprocess.run([sys.executable, "-c", _snippet("python3")], input=raw,
                       capture_output=True, text=True)
    assert (p.returncode, p.stdout) == (code, out)
    if shutil.which("perl") and subprocess.run(["perl", "-MJSON::PP", "-e", "1"]).returncode == 0:
        p = subprocess.run(["perl", "-MJSON::PP", "-e", _snippet("perl")], input=raw,
                           capture_output=True, text=True)
        assert (p.returncode, p.stdout) == (code, out)


def test_the_fork_scripts_parse_as_posix_sh():
    for script in sorted(FORK_TOOLS.glob("*.sh")):
        p = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
        assert p.returncode == 0, f"{script.name}: {p.stderr}"
