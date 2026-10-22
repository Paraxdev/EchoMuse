"""
The fork's packaging, tools/fork/build_app.sh.

The app folder must be the EA app with a new identity and nothing else, or
the upstream app's options stop being valid for it; its overlay build must
start from a pinned image and compile the dashboard the way the image build
does; and it must refuse the overlay once the fork's image inputs have moved,
rather than run new code on old wheels.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import version

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "tools" / "fork" / "build_app.sh"

needs_sh = pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                              reason="needs a POSIX sh")

IDENTITY = {"name", "version", "slug", "description", "url", "panel_title", "image", "boot"}


def build(out, *args, root=REPO, check=True):
    p = subprocess.run(["sh", str(root / "tools" / "fork" / "build_app.sh"), "--out", str(out), *args],
                       capture_output=True, text=True, timeout=300)
    if check and p.returncode != 0:
        raise AssertionError(f"build_app.sh failed\n{p.stdout}\n{p.stderr}")
    return p


def base_fingerprint():
    return re.search(r'^BASE_FINGERPRINT="([0-9a-f]{64})"$', BUILD.read_text(), re.M).group(1)


def tree_fingerprint(root=REPO):
    return subprocess.run(["sh", str(root / "tools" / "fork" / "build_app.sh"), "--fingerprint"],
                          capture_output=True, text=True, check=True).stdout.strip()


@needs_sh
@pytest.mark.parametrize("mode", ["overlay", "full"])
def test_the_app_is_the_ea_app_with_a_new_identity(tmp_path, mode):
    if mode == "overlay" and tree_fingerprint() != base_fingerprint():
        pytest.skip("the tree's image inputs have moved past the overlay base "
                    "(test_the_overlay_refuses_once_the_image_inputs_move covers this state)")
    out = tmp_path / "echomuse_fork"
    build(out, *(["--full"] if mode == "full" else []))

    ea = yaml.safe_load((REPO / "controller-ea" / "config.yaml").read_text(encoding="utf-8"))
    app = yaml.safe_load((out / "config.yaml").read_text(encoding="utf-8"))

    assert {k: v for k, v in app.items() if k not in IDENTITY} == \
        {k: v for k, v in ea.items() if k not in IDENTITY}
    assert "image" not in app, "without image: Supervisor builds the folder"
    assert app["slug"] == "echomuse_fork"
    assert app["boot"] == "manual", "must not start at boot beside the upstream app before migrating"
    assert app["options"]["esphome_port_base"] == ea["options"]["esphome_port_base"]
    assert (out / "translations" / "en.yaml").read_bytes() == \
        (REPO / "controller-ea" / "translations" / "en.yaml").read_bytes()

    dockerfile = (out / "Dockerfile").read_text()
    stamped = re.search(r'^(?:ENV|ARG) EM_CONTROLLER_VERSION="?([^"\s]+)"?$', dockerfile, re.M).group(1)
    assert stamped == "v" + app["version"]
    assert version.parse(stamped) is not None, "version.py must be able to read it"
    assert version.offers_ea_firmware(stamped)
    assert "-fork" in stamped

    marker = (out / ".echomuse_fork_app").read_text()
    assert f"mode={mode}" in marker
    # Supervisor reads every config.json/yaml/yml below /addons as an app.
    configs = [p for p in out.rglob("config.*") if p.suffix in (".json", ".yaml", ".yml")]
    assert configs == [out / "config.yaml"]


@needs_sh
def test_the_overlay_starts_from_a_pinned_image_and_compiles_the_dashboard_as_the_image_does(tmp_path):
    if tree_fingerprint() != base_fingerprint():
        pytest.skip("the tree's image inputs have moved past the overlay base")
    out = tmp_path / "echomuse_fork"
    build(out)
    dockerfile = (out / "Dockerfile").read_text()

    [base] = re.findall(r"^FROM (\S+)$", dockerfile, re.M)
    assert re.fullmatch(r"ghcr\.io/wilbowes/echomuse-controller:[0-9][^@]*@sha256:[0-9a-f]{64}", base)

    [esbuild] = [ln for ln in (REPO / "controller" / "Dockerfile").read_text().splitlines()
                 if ln.startswith("RUN ") and "esbuild /app/static/dashboard.jsx" in ln]
    assert esbuild in dockerfile.splitlines(), "the esbuild step must be controller/Dockerfile's own"

    app = out / "app"
    for name in ("em_controller.py", "em_start.py", "version.py", "requirements.txt",
                 "esphome/frame_protocol.py", "device_payloads/start_server.sh",
                 "sounds/timer_finished.flac", "static/dashboard.jsx", "static/index.html"):
        assert (app / name).is_file(), name
    for gone in ("static/dashboard.js", "static/vendor", "tests", "CLAUDE.md"):
        assert not (app / gone).exists(), gone
    assert {p.name for p in app.glob("*.py")} == {p.name for p in (REPO / "controller").glob("*.py")}


def _copy_tree(dst):
    shutil.copytree(REPO / "tools" / "fork", dst / "tools" / "fork")
    shutil.copytree(REPO / "controller-ea", dst / "controller-ea")
    shutil.copytree(REPO / "controller", dst / "controller",
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))


@needs_sh
def test_the_overlay_refuses_once_the_image_inputs_move(tmp_path):
    root = tmp_path / "repo"
    _copy_tree(root)
    with open(root / "controller" / "requirements.txt", "a", encoding="utf-8") as f:
        f.write("some-new-wheel==1.0\n")

    p = build(tmp_path / "out", root=root, check=False)
    assert p.returncode != 0
    assert "--full" in p.stderr
    assert not (tmp_path / "out").exists()

    build(tmp_path / "out", "--full", root=root)
    assert "some-new-wheel" in (tmp_path / "out" / "requirements.txt").read_text()


@needs_sh
def test_a_new_module_does_not_force_the_full_build(tmp_path):
    root = tmp_path / "repo"
    _copy_tree(root)
    if tree_fingerprint(root) != base_fingerprint():
        pytest.skip("the tree's image inputs have moved past the overlay base")
    (root / "controller" / "em_new_thing.py").write_text("X = 1\n")
    df = root / "controller" / "Dockerfile"
    df.write_text(df.read_text().replace("COPY version.py .\n", "COPY version.py .\nCOPY em_new_thing.py .\n"))

    build(tmp_path / "out", root=root)
    assert (tmp_path / "out" / "app" / "em_new_thing.py").is_file()


@needs_sh
def test_the_builder_refuses_to_replace_a_folder_it_did_not_write(tmp_path):
    out = tmp_path / "somebody_elses"
    out.mkdir()
    (out / "keep").write_text("x")
    p = build(out, "--full", check=False)
    assert p.returncode != 0
    assert (out / "keep").read_text() == "x"


@needs_sh
@pytest.mark.parametrize("bad", ["2.27.0", "v2.27", "v1.2.3;rm -rf /", "v1.2.3 x", "v1.2.3+meta"])
def test_the_version_must_be_one_version_py_and_docker_both_accept(tmp_path, bad):
    p = build(tmp_path / "out", "--full", "--version", bad, check=False)
    assert p.returncode != 0, bad
