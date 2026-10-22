# Running the Paraxdev fork on Home Assistant OS

These scripts run the fork's controller as a local app on the Home Assistant
OS VM (VM 100 on the Proxmox host `pve`), move the Echo and its Home Assistant
entities across from the upstream app, and move them back. Everything is
started from the Proxmox host as root; nothing needs a shell inside Home
Assistant.

The upstream app (`acc06409_controller-ea`, "EchoMuse (Early Access)") stays
installed throughout. Its data folder is only ever copied from, so going back
is starting it again.

## What is here

| File | Runs on | Does |
|---|---|---|
| `build_app.sh` | anywhere with `sh` | writes the local app folder from this checkout |
| `pve.sh` | Proxmox host, as root | builds, pushes the folder into the VM, installs; runs the three below |
| `haos_install.sh` | HAOS host, called by `pve.sh deploy` | puts the folder in place, then installs, updates or rebuilds the app |
| `haos_migrate.sh` | HAOS host, `pve.sh migrate` | upstream app to fork app |
| `haos_rollback.sh` | HAOS host, `pve.sh rollback` | fork app to upstream app |
| `haos_status.sh` | HAOS host, `pve.sh status` | read-only report |
| `haos_lib.sh` | | shared by the `haos_*` scripts |

`pve.sh` reaches the VM only through `qm guest exec`, which needs the QEMU
guest agent (Home Assistant OS has it). Files go in as base64 through
`--pass-stdin` in chunks under its 1 MiB limit, and the package's sha256 is
checked in the VM before anything uses it. In the VM the scripts drive
Supervisor from the `hassio_cli` container: the `ha` CLI for install, update,
start and stop, and the Supervisor API (curl and jq in that container) for
reading state and for the boot and options settings. Work files live in
`/mnt/data/echomuse_fork/` in the VM.

## The app

- **Name** "EchoMuse (Paraxdev fork)", slug `echomuse_fork`, which Supervisor
  installs as `local_echomuse_fork`. Sidebar entry "EchoMuse fork". Its data
  folder is `/mnt/data/supervisor/apps/data/local_echomuse_fork`.
- **Options, schema, ports and ingress** are `controller-ea/config.yaml`
  verbatim, including `esphome_port_base: 16101`, so the upstream app's
  options are valid for it as they are, and the migration copies them.
- **Start on boot is off** until the migration turns it on. Both apps bind
  the same host ports (8767, 8768, 8770, 16101, 17101), so they must never
  run together, and the scripts check for that rather than assume it.
- **Version** is stamped into the image as `EM_CONTROLLER_VERSION` and into
  the app's `version:`. With upstream's controller tags in the clone it reads
  like `v2.27.0-ea.1-3-ge2f2f4e-fork`; without them,
  `v2.27.0-ea.1-ge2f2f4e-fork` (the EA version this tree carries, then the
  commit). `controller/version.py` reads either as 2.27.0, and as a build that
  is offered Early Access firmware.

### Two ways to build it

**Overlay (the default).** `FROM` the image the upstream app already runs,
`ghcr.io/wilbowes/echomuse-controller:2.27.0-ea.1`, pinned by its index digest
(`sha256:52693f22...`), with the fork's Python, ESPHome package, device
payloads, sounds and dashboard sources copied over it and the dashboard
compiled again with the esbuild step from `controller/Dockerfile` (the
published image deletes esbuild, and `dashboard.js` is not in git). Supervisor
builds it in about a minute; esbuild is the only download.

It is only right while the fork builds its image from the same inputs as that
base: `controller/Dockerfile` (apart from its per-module `COPY x.py .` lines),
`requirements.txt`, and the two model rewrite tools. `build_app.sh` compares a
fingerprint of those with the base's and refuses the overlay when they
differ, and the image build compares `requirements.txt` with the base image's
own copy again.

**Full (`--full`).** `controller/Dockerfile` itself, for a fork that changes
what the image is built from. Several minutes on the VM and a few hundred MB
of downloads.

To move the overlay to a newer upstream image, set `BASE_IMAGE` (tag and
digest, from `docker buildx imagetools inspect ghcr.io/wilbowes/echomuse-controller:<tag>`),
`BASE_REF` and `BASE_FINGERPRINT` at the top of `build_app.sh`. The
fingerprint comes from the tag the image was released from:

```sh
sh tools/fork/build_app.sh --fingerprint controller-ea-v2.28.0-ea.1
```

## First time: deploy, migrate, verify

**0. Get the fork onto the Proxmox host.** Either a clone (the version then
names the commit):

```sh
apt install git        # if it is missing
git clone --branch fork/forkops https://github.com/Paraxdev/EchoMuse /root/EchoMuse
cd /root/EchoMuse
```

Or no clone at all: fetch `pve.sh` alone and let the VM clone and build, with
`deploy --git` in step 1.

```sh
curl -fsSLO https://raw.githubusercontent.com/Paraxdev/EchoMuse/refs/heads/fork/forkops/tools/fork/pve.sh
```

Both need the branch pushed to GitHub. From Windows, run any of the commands
below through `ssh pve '...'`.

**1. Build and install the app.** It is installed stopped, with start on
boot off, and the upstream app keeps running.

```sh
sh tools/fork/pve.sh deploy --dry-run   # optional: build and pack only, sends nothing
sh tools/fork/pve.sh deploy
# or, without a clone:
sh pve.sh deploy --git https://github.com/Paraxdev/EchoMuse --ref fork/forkops
```

The output arrives when Supervisor has finished building (a minute or two).
If the build fails, the end of the Supervisor log is printed.

**2. Migrate.** This is the step that changes what runs.

```sh
sh tools/fork/pve.sh migrate
```

In order (`pve.sh` prints the steps when the run ends, which can take a few
minutes; interrupting `pve.sh` does not stop the run in the VM, and a second
run refuses to start until the first has ended):

1. stops `acc06409_controller-ea` and turns off its start at boot;
2. refuses to go on while any running container still has its data folder
   mounted, whatever Supervisor reports;
3. stops the fork app; if Supervisor has not created its data folder yet,
   starts it once and stops it again;
4. **moves the fork app's current data folder** to
   `/mnt/data/echomuse_fork/backups/<time>/` (on a first migration that is the
   empty folder from the install);
5. copies the **whole** upstream data folder with `cp -a`: `echomuse.db`
   together with its `-wal` and `-shm` (the WAL holds the newest writes),
   `tls/` (the CA and server certificate the Echo verifies; without them it
   cannot connect at all), `oww_models/` (the custom wake word), `firmware/`
   and any recordings. Every file is then compared by checksum;
6. copies the upstream app's options and its sidebar and watchdog settings to
   the fork app, turns the fork's start at boot on, and starts it.

Because the database and certificates are copied, the Echo keeps its pairing
and its ESPHome ports (stored per device in the database), so Home Assistant's
existing ESPHome entries come back on their own.

Run it again and it does not copy again: that would throw away what the fork
has written since. `--recopy` does copy again, after the same backup. If the
upstream app has run since the last copy (after a rollback), it stops and asks
for `--recopy` (take the upstream data) or `--keep-fork-data` (keep what the
fork has; refused when nothing was ever migrated).

**3. Verify.** The migration ends with this checklist:

- Settings > Apps: "EchoMuse (Paraxdev fork)" started with Start on boot on;
  "EchoMuse (Early Access)" stopped with Start on boot off.
- The fork's log reads `Opening database: /data/echomuse.db`, then
  `Controller ready`, with no Traceback (`sh tools/fork/pve.sh status --logs 40`).
- The "EchoMuse fork" sidebar entry opens the dashboard and its header shows
  the fork version.
- Within a few minutes the Echo is connected and its Status tab Link row reads
  `wss (TLS)`. **If it shows as pending approval instead, the database did not
  come across: do not approve it; roll back.**
- Home Assistant's ESPHome entries for the Echo (voice on 16101, Bluetooth
  proxy on 17101) are available again, with the same entities.
- A wake word, a question and an answer work end to end.

**4. Going back.**

```sh
sh tools/fork/pve.sh rollback
```

Stops the fork and turns off its start at boot, turns on the upstream app's
and starts it. Nothing is deleted. Two things to know:

- Anything changed while on the fork (device settings, a new wake word model,
  history) exists only in the fork's data folder.
- If the Echo was paired again while on the fork (the Pair button, or its
  action button held for 5 seconds), it holds a token the upstream database
  has never seen, and the upstream controller refuses it. Remove it in the
  upstream dashboard and approve it again, or hold the action button for 5
  seconds and approve the pairing request.

## After a new commit

```sh
cd /root/EchoMuse && git pull
sh tools/fork/pve.sh deploy
```

The version names the new commit, so Supervisor runs an update: it builds the
new image and, if the fork app was running, restarts it on it. The data
folder is untouched. The same commit deployed twice is a rebuild. If
`build_app.sh` refuses the overlay because the image inputs changed, deploy
with `--full`.

`sh tools/fork/pve.sh status` shows both apps, their data folders, the
migration record and the backups. Backups are never deleted by these
scripts; remove old ones from `/mnt/data/echomuse_fork/backups/` yourself
(for example `qm guest exec 100 -- rm -rf /mnt/data/echomuse_fork/backups/<time>`).

**Do not uninstall the upstream app while you might roll back.** Uninstalling
an app deletes its data folder, and that folder is the rollback.

## Firmware built from the fork

The controller deploys a firmware binary uploaded through the dashboard, so
fork firmware needs no release:

1. Build it with the pinned compiler (see `device/CLAUDE.md`):

   ```sh
   git submodule update --init
   docker build -t echomuse-compiler device/compiler/
   git fetch https://github.com/wilbowes/EchoMuse 'refs/tags/v*:refs/tags/v*'
   cd device && ./compile.sh                 # writes device/build/server
   ```

   The tag fetch is for the version string. `compile.sh` stamps
   `git describe --match 'v*'`, which on the fork today is a build past the
   EA tag, such as `v2.19.0-ea.1-7-g9465e4a`. The Echo reports that version
   once it runs the build, and the Updates tab orders it after
   `v2.19.0-ea.1`, so that release is not offered over it (a newer upstream
   release is, and installing it replaces the fork build). Without the tags
   the binary carries a bare commit id, which cannot be ordered, so the
   Updates tab offers upstream's newest firmware as an update over the fork
   build, and an update of all devices would replace it.

   Until the Echo reports its version, the controller labels the update
   `local-<time>`: it reads a version out of the binary only in the forms
   `v2.18.1-66-g9465e4a` (a build past a GA tag) and `YYYYMMDD-HHMM-dev` (a
   dirty tree), not a build past an EA tag. The label is cosmetic, and the
   install goes ahead. It also means the dashboard cannot warn that the Echo
   already runs that build. Keep the tags local: pushing them to the fork
   would start a release workflow for every one of them once Actions is
   enabled.

2. Upload it: dashboard > the Echo > **Updates** > **Local Build** > Choose
   file > Deploy. That is `POST /api/releases/upload` (admin, multipart field
   `binary`), which returns an `upload_token` and the version read from the
   binary, then `POST /api/devices/<id>/update` with that token. It goes to the
   device's other slot. If the new binary exits within seconds of starting
   three times in a row, the device switches back to the old slot by itself;
   one that starts and then misbehaves is not caught that way, and the
   Updates tab's rollback is the way back.

Keep the Go module path `github.com/wilbowes/EchoMuse`: the dashboard refuses a
file that does not contain it, and `compile.sh` sets the version through it.
On a lossy WiFi link the transfer can fail with a keepalive timeout; the
running firmware is untouched (the copy is checked by md5 before the switch),
so retry when the link is good.

## Why the release source stays `wilbowes/EchoMuse`

Settings > System > Release source (`github_repo`) is one setting behind three
feeds: firmware updates (`v*` releases with a `server` asset), emOS updates
(`emos-v*` releases), and the controller update notice (`controller-v*`
tags). A GitHub fork copies none of those, and this fork has published none,
so pointing the setting at `Paraxdev/EchoMuse` today would empty all three:
no firmware offered, no emOS update, no notice. Leave it on
`wilbowes/EchoMuse` until the fork publishes its own `v*` and `emos-v*`
releases in upstream's tag shapes, and use Local Build for fork firmware until
then.

## Publishing the fork's own image (later)

The workflows lowercase the owner now (Docker refuses `ghcr.io/Paraxdev/...`),
so with Actions enabled the fork can publish `ghcr.io/paraxdev/echomuse-controller`:

1. Enable Actions on Paraxdev/EchoMuse.
2. A push to `main` that touches `controller/` runs "Controller Build (main)",
   which publishes `:sha-<commit>`. An annotated `controller-ea-vX.Y.Z-ea.N`
   tag, pushed once that build is green, promotes it to `:X.Y.Z-ea.N`. The
   release refuses a tag that differs from `controller-ea/config.yaml`'s
   `version:`, so set that first, from `controller/`, with
   `python tools/sync_channels.py --set-version X.Y.Z-ea.N`, and pick numbers
   upstream has not used.
3. Make the GHCR package public; a new package is private, and Supervisor
   cannot pull a private one.

Only then change what the add-on repository points at, because until the
image exists an install from the fork's repository would fail to pull:

- `controller/config.yaml`: `image: "ghcr.io/paraxdev/echomuse-controller"` and
  `url: "https://github.com/Paraxdev/EchoMuse"`, then
  `python controller/tools/sync_channels.py` to regenerate
  `controller-ea/config.yaml` (never edit that by hand; `test_channels.py`
  requires both to name the same image);
- `repository.yaml`: a name, url and maintainer of the fork's own, so the
  store does not show two repositories called "EchoMuse";
- `controller/Dockerfile`: the `org.opencontainers.image.source` label.

An app installed from the fork's repository gets a slug of its own
(`8769bfe0_controller-ea`, from the repository URL) and so its own data
folder: moving to it is another migration, and these scripts would need
their fork slug changed for it.

## Flashing over USB

The provisioning wizard cannot reach an Echo plugged into the Home Assistant
or Proxmox host; see "My Echo is plugged into the Home Assistant or Proxmox
host" in `docs/faq.md`.

## When something fails

- `qm guest exec --pass-stdin failed`: this Proxmox VE has no `--pass-stdin`
  (or the agent refused the input). Use `deploy --git URL --ref BRANCH`.
- `still running in the VM after the timeout (pid N)`: the command goes on in
  the VM; `qm guest exec-status 100 N` shows how it ended.
- `another tools/fork script (pid N) is still running in this VM`: an earlier
  deploy, migrate or rollback is still going, usually one whose `pve.sh` was
  interrupted. N is the process `qm guest exec` started, so
  `qm guest exec-status 100 N` shows whether it has ended and what it printed.
  Run the command again once it has. A lock whose process has gone is removed
  by the next run.
- `hassio_cli has no curl, jq or SUPERVISOR_TOKEN`: the scripts need those in
  that container to reach Supervisor, and stop before changing anything.
- A migration that stops part way prints what state it left and the command
  that goes back. The upstream data folder is never written.
- Supervisor's own log: `qm guest exec 100 -- docker exec hassio_cli ha supervisor logs`.
