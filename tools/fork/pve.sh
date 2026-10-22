#!/bin/sh
# pve.sh: run as root on the Proxmox host. Ships the fork's controller to the
# Home Assistant OS VM as a local app, and drives the migration to it.
#
# Usage:
#   pve.sh deploy [--full] [--dry-run] [--force]
#                                            build from this checkout, push it into the VM
#   pve.sh deploy --git URL [--ref REF] [--full] [--force]
#                                            clone and build inside the VM instead
#   pve.sh migrate [--recopy | --keep-fork-data] [--no-start] [--from SLUG]
#   pve.sh rollback [--no-start] [--from SLUG]
#   pve.sh status [--logs N]
#
# deploy installs or updates the app and leaves it stopped (Supervisor starts
# an updated app again only if it was running). Deploying the version already
# installed is a rebuild, which takes a running fork down for the length of
# the build, so it is refused while the fork runs unless --force is given.
# migrate, rollback and status run the haos_*.sh scripts that deploy left in
# the VM.
#
# Everything in the VM runs through `qm guest exec`, which prints JSON once
# the command ends; the exit code is read from that, never assumed. Files go
# in as base64 through --pass-stdin, at most 1 MiB per call, so the package
# is sent in chunks and its sha256 checked in the VM before anything uses it.
#
# EMF_VMID picks the VM (default 100). EMF_GUEST_WORK and EMF_CHUNK (bytes of
# base64 per call) are for the tests.
set -eu

VMID=${EMF_VMID:-100}
GUEST_WORK=${EMF_GUEST_WORK:-/mnt/data/echomuse_fork}
CHUNK=${EMF_CHUNK:-524288}
# The clone for `deploy --git` runs in this image (git and busybox).
ALPINE_GIT="alpine/git:2.47.2@sha256:062a01ad7a0eb17cff382bc5e26086b4d710e56dfdfdf001109a49b6d9bd378c"
INTERRUPT_NOTE="Interrupting this does not stop it in the VM; a second run refuses while the first is still going."

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

say() { printf '%s\n' "$*"; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    sed -n '/^# Usage:/,/^#$/{s/^# \{0,1\}//p}' "$0"
}

# qm prints {"exitcode", "exited", "out-data", "err-data"} when the command
# finished, or only {"pid"} when --timeout ran out first.
json_result() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c '
import json, sys
try:
    r = json.load(sys.stdin)
except ValueError:
    sys.stderr.write("qm guest exec did not return JSON\n")
    sys.exit(125)
if not r.get("exited"):
    sys.stderr.write("still running in the VM after the timeout (pid %s); check it with: qm guest exec-status VMID %s\n" % (r.get("pid"), r.get("pid")))
    sys.exit(124)
sys.stdout.write(r.get("out-data", ""))
sys.stderr.write(r.get("err-data", ""))
if r.get("out-truncated") or r.get("err-truncated"):
    sys.stderr.write("(output truncated by the guest agent)\n")
if "signal" in r and r.get("exitcode") is None:
    sys.exit(128 + int(r["signal"]))
sys.exit(int(r.get("exitcode", 1)))
'
    else
        perl -MJSON::PP -e '
local $/;
my $r = eval { decode_json(<STDIN>) };
if (!$r) { print STDERR "qm guest exec did not return JSON\n"; exit 125; }
if (!$r->{exited}) {
    my $p = $r->{pid} // "?";
    print STDERR "still running in the VM after the timeout (pid $p); check it with: qm guest exec-status VMID $p\n";
    exit 124;
}
print STDOUT $r->{"out-data"} // "";
print STDERR $r->{"err-data"} // "";
print STDERR "(output truncated by the guest agent)\n" if $r->{"out-truncated"} || $r->{"err-truncated"};
exit(defined $r->{exitcode} ? $r->{exitcode} : 128 + ($r->{signal} // 0));
'
    fi
}

# qga TIMEOUT INPUT CMD...: run CMD in the VM, print its output, return its
# exit code. INPUT is a file sent as stdin, or - for none.
qga() {
    _t=$1
    _in=$2
    shift 2
    if [ "$_in" = - ]; then
        _json=$(qm guest exec "$VMID" --timeout "$_t" -- "$@") || die "qm guest exec failed: $*"
    else
        _json=$(qm guest exec "$VMID" --timeout "$_t" --pass-stdin 1 -- "$@" <"$_in") \
            || die "qm guest exec --pass-stdin failed. If this Proxmox VE has no --pass-stdin, use 'pve.sh deploy --git URL' instead."
    fi
    printf '%s' "$_json" | json_result
}

check_vm() {
    command -v qm >/dev/null 2>&1 || die "no qm here; run this on the Proxmox host"
    command -v python3 >/dev/null 2>&1 || perl -MJSON::PP -e 1 2>/dev/null \
        || die "need python3, or perl with JSON::PP, to read qm's output"
    _st=$(qm status "$VMID" 2>&1) || die "qm status $VMID failed (this needs root on the Proxmox host): $_st"
    case $_st in
        *"status: running"*) ;;
        *) die "VM $VMID is not running ($_st)" ;;
    esac
    qm guest cmd "$VMID" ping >/dev/null 2>&1 \
        || die "the QEMU guest agent in VM $VMID does not answer (qm guest cmd $VMID ping)"
}

# Unpack in the VM, on the host's own busybox when it has every tool, or in a
# throwaway container from the hassio_cli image, which every Home Assistant
# OS has locally. $3 is the script that does the work.
# shellcheck disable=SC2016
GUEST_UNPACK='set -eu
if command -v base64 >/dev/null && command -v sha256sum >/dev/null && command -v gzip >/dev/null && command -v tar >/dev/null; then
    sh -c "$3" sh "$1" "$2"
else
    img=$(docker inspect --format "{{.Config.Image}}" hassio_cli)
    docker run --rm --network none -v "$1:$1" --entrypoint sh "$img" -c "$3" sh "$1" "$2"
fi'
# shellcheck disable=SC2016
UNPACK='set -eu
cd "$1"
base64 -d pkg.b64 >pkg.tgz
if ! echo "$2  pkg.tgz" | sha256sum -c - >/dev/null; then
    echo "the package arrived damaged (sha256 mismatch); nothing was installed" >&2
    exit 1
fi
rm -rf pkg
mkdir pkg
gzip -dc pkg.tgz | tar -xf - -C pkg
echo "Unpacked in the VM, sha256 verified."'

# deploy --git: clone and build in the VM. URL, ref and build mode arrive as
# arguments. The mode is a word, never an empty argument, so the positions
# cannot shift if one were dropped on the way through qm.
# shellcheck disable=SC2016
GUEST_CLONE='set -eu
rm -rf "$1/incoming"
mkdir -p "$1/incoming/pkg/tools"
docker run --rm -v "$1/incoming:/w" --entrypoint sh "$5" -c "$6" sh "$2" "$3" "$4"'
# shellcheck disable=SC2016
CLONE='set -eu
git clone --quiet --depth 1 --branch "$2" -- "$1" /w/src
case $3 in
    full) sh /w/src/tools/fork/build_app.sh --full --out /w/pkg/echomuse_fork ;;
    overlay) sh /w/src/tools/fork/build_app.sh --out /w/pkg/echomuse_fork ;;
    *) echo "unknown build mode: $3" >&2; exit 1 ;;
esac
cp /w/src/tools/fork/haos_*.sh /w/pkg/tools/
rm -rf /w/src'

cmd_deploy() {
    full=""
    force=""
    git_url=""
    git_ref=""
    dry=0
    while [ $# -gt 0 ]; do
        case $1 in
            --full) full=--full ;;
            --force) force=--force ;;
            --dry-run) dry=1 ;;
            --git)
                [ $# -ge 2 ] || die "--git needs a repository URL"
                git_url=$2
                shift
                ;;
            --ref)
                [ $# -ge 2 ] || die "--ref needs a branch or tag"
                git_ref=$2
                shift
                ;;
            *) die "unknown deploy argument: $1" ;;
        esac
        shift
    done

    if [ -n "$git_url" ]; then
        case $git_url in
            https://*) ;;
            *) die "--git takes an https:// URL" ;;
        esac
        case $git_url in
            *[!A-Za-z0-9._~:/@%+-]*) die "unexpected characters in the URL: $git_url" ;;
        esac
        git_ref=${git_ref:-main}
        case $git_ref in
            -* | *[!A-Za-z0-9._/-]*) die "unexpected characters in the ref: $git_ref" ;;
        esac
        [ "$dry" = 0 ] || die "--dry-run builds this checkout; it does not apply to --git"
        mode=overlay
        [ -z "$full" ] || mode=full
        check_vm
        say "Cloning $git_url ($git_ref) and building it ($mode) inside VM $VMID"
        qga 1200 - sh -c "$GUEST_CLONE" sh "$GUEST_WORK" "$git_url" "$git_ref" "$mode" "$ALPINE_GIT" "$CLONE"
    else
        for t in tar gzip base64 split sha256sum mktemp; do
            command -v "$t" >/dev/null 2>&1 || die "$t is missing on this host"
        done
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/pkg/tools"
        # shellcheck disable=SC2086
        sh "$HERE/build_app.sh" $full --out "$tmp/pkg/echomuse_fork"
        cp "$HERE"/haos_*.sh "$tmp/pkg/tools/"
        tar -C "$tmp/pkg" -cf - echomuse_fork tools | gzip -9 >"$tmp/pkg.tgz"
        sum=$(sha256sum "$tmp/pkg.tgz" | cut -d' ' -f1)
        base64 "$tmp/pkg.tgz" >"$tmp/pkg.b64"
        split -b "$CHUNK" "$tmp/pkg.b64" "$tmp/chunk."
        n=$(find "$tmp" -maxdepth 1 -name 'chunk.*' | wc -l)
        say "Package: $(wc -c <"$tmp/pkg.tgz") bytes gzipped, $n chunk(s) of base64, sha256 $sum"
        if [ "$dry" = 1 ]; then
            say "Dry run: nothing was sent. The app folder was:"
            find "$tmp/pkg" -maxdepth 2 | sed "s|^$tmp/pkg/|  |" | sort
            return 0
        fi
        check_vm
        # shellcheck disable=SC2016
        qga 60 - sh -c 'rm -rf "$1" && mkdir -p "$1"' sh "$GUEST_WORK/incoming"
        i=0
        for c in "$tmp"/chunk.*; do
            i=$((i + 1))
            say "Sending chunk $i of $n"
            # shellcheck disable=SC2016
            qga 120 "$c" sh -c 'cat >>"$1"' sh "$GUEST_WORK/incoming/pkg.b64"
        done
        qga 300 - sh -c "$GUEST_UNPACK" sh "$GUEST_WORK/incoming" "$sum" "$UNPACK"
    fi

    say "Installing in VM $VMID (Supervisor builds the image; the output arrives when it is done)"
    say "$INTERRUPT_NOTE"
    qga 3600 - sh "$GUEST_WORK/incoming/pkg/tools/haos_install.sh" "$GUEST_WORK/incoming/pkg" ${force:+"$force"}
}

# migrate, rollback, status: the copies deploy installed in the VM.
cmd_guest() {
    script=$1
    shift
    check_vm
    # shellcheck disable=SC2016
    qga 30 - sh -c 'test -f "$1"' sh "$GUEST_WORK/tools/$script" \
        || die "$GUEST_WORK/tools/$script is not in the VM; run 'pve.sh deploy' first"
    say "Running $script in VM $VMID; its output arrives when it ends, which can take minutes."
    [ "$script" = haos_status.sh ] || say "$INTERRUPT_NOTE"
    qga 3600 - sh "$GUEST_WORK/tools/$script" "$@"
}

[ $# -ge 1 ] || {
    usage
    exit 2
}
cmd=$1
shift
case $cmd in
    deploy) cmd_deploy "$@" ;;
    migrate) cmd_guest haos_migrate.sh "$@" ;;
    rollback) cmd_guest haos_rollback.sh "$@" ;;
    status) cmd_guest haos_status.sh "$@" ;;
    -h | --help | help) usage ;;
    *)
        usage >&2
        exit 2
        ;;
esac
