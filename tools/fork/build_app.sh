#!/bin/sh
# build_app.sh: package this checkout's controller as a Home Assistant LOCAL
# app (slug echomuse_fork) for the Paraxdev fork.
#
# Two modes, because the folder written here is what Supervisor builds:
#
#   overlay (default)  FROM the published upstream image, pinned by digest,
#                      with the fork's Python, ESPHome package, device payloads,
#                      sounds and dashboard sources copied over it and the
#                      dashboard compiled again. About a minute on the Home
#                      Assistant host; esbuild is the only download.
#   --full             controller/Dockerfile itself, for a fork that changes
#                      what the image is built from. Several minutes and a few
#                      hundred MB of downloads on the Home Assistant host.
#
# The overlay is only right while the fork's image inputs (Dockerfile,
# requirements.txt, the two model rewrite tools) are the ones the base image
# was built from, so it refuses otherwise instead of shipping old wheels under
# new code. BASE_FINGERPRINT records those inputs at the base image's commit;
# `--fingerprint <git-ref>` recomputes it when the base moves.
#
# Options, schema, ports and ingress are controller-ea/config.yaml verbatim,
# so the upstream app's options are valid here unchanged.
#
# POSIX sh with sed, awk, grep, find, cp and sha256sum: it runs under busybox
# (alpine/git on the Home Assistant host) as well as on Proxmox.
#
# Usage: tools/fork/build_app.sh [--full] [--out DIR] [--version vX.Y.Z-...]
#        tools/fork/build_app.sh --fingerprint [GIT-REF]
set -eu

# The image the upstream EA app already runs, so Supervisor finds it locally.
# Multi-arch index digest, from
#   docker buildx imagetools inspect ghcr.io/wilbowes/echomuse-controller:2.27.0-ea.1
# Built from controller-ea-v2.27.0-ea.1 (50bbbc3).
BASE_IMAGE="ghcr.io/wilbowes/echomuse-controller:2.27.0-ea.1@sha256:52693f2230e0342cd1838eb00a5c27d7084160b2bfcad15b8bac117dbd0ab533"
BASE_REF="controller-ea-v2.27.0-ea.1"
BASE_FINGERPRINT="88dd10584bb8dca8905de333f13d6e87dd1fef8e3163098d288e0d92cc9115c7"

SLUG="echomuse_fork"
NAME="EchoMuse (Paraxdev fork)"
FORK_URL="https://github.com/Paraxdev/EchoMuse"
MARKER=".echomuse_fork_app"
FP_FILES="controller/Dockerfile controller/requirements.txt controller/tools/silero_typed.py controller/tools/oww_stream.py"

die() { printf 'build_app: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n 's/^# Usage: //p; s/^#        tools/tools/p' "$0"
}

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -- "$HERE/../.." && pwd)
CTRL="$ROOT/controller"
EA="$ROOT/controller-ea"

# Per-module `COPY x.py .` lines are left out: the overlay copies every *.py,
# so a new module, which always adds one, does not force the full build. CR is
# dropped so a checkout made with autocrlf still matches.
fingerprint() {
    for f in $FP_FILES; do
        if [ -n "$1" ]; then
            git -C "$ROOT" cat-file -e "$1:$f" 2>/dev/null || die "$f is not in $1"
        else
            [ -f "$ROOT/$f" ] || die "$f is missing"
        fi
    done
    for f in $FP_FILES; do
        printf '== %s\n' "$f"
        if [ -n "$1" ]; then
            git -C "$ROOT" show "$1:$f"
        else
            cat "$ROOT/$f"
        fi | tr -d '\r' | grep -v -E '^COPY[[:space:]]+[A-Za-z0-9_]+\.py[[:space:]]+\./?[[:space:]]*$' || true
    done | sha256sum | cut -d' ' -f1
}

ea_version() {
    sed -n 's/^version:[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' "$EA/config.yaml"
}

# The form version.py parses: vX.Y.Z first, then anything after a hyphen. The
# nearest controller tag when the clone has one, which reads like upstream's
# own describe output; otherwise the EA version this tree carries plus the
# commit. "-fork" keeps a support bundle from passing for an upstream build.
stamp_version() {
    ea=$(ea_version)
    [ -n "$ea" ] || die "no version: in controller-ea/config.yaml"
    if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --verify -q HEAD >/dev/null 2>&1; then
        if d=$(git -C "$ROOT" describe --tags --match 'controller-ea-v*' --match 'controller-v*' --dirty 2>/dev/null); then
            d=${d#controller-ea-}
            d=${d#controller-}
            printf '%s-fork\n' "$d"
            return
        fi
        sha=$(git -C "$ROOT" rev-parse --short=7 HEAD)
        dirty=""
        git -C "$ROOT" diff --quiet HEAD -- 2>/dev/null || dirty="-dirty"
        printf 'v%s-g%s%s-fork\n' "$ea" "$sha" "$dirty"
        return
    fi
    printf 'v%s-local-fork\n' "$ea"
}

check_version() {
    case $1 in
        v[0-9]*) ;;
        *) die "version must start with v and a digit: $1" ;;
    esac
    case $1 in
        *[!A-Za-z0-9._-]*) die "version may hold only letters, digits, '.', '_' and '-' (it becomes a Docker tag): $1" ;;
    esac
    [ "${#1}" -le 120 ] || die "version is longer than 120 characters: $1"
    core=${1#v}
    core=${core%%-*}
    case $core in
        *[!0-9.]* | .* | *. | *..*) die "version must begin vX.Y.Z: $1" ;;
    esac
    [ "$(printf '%s' "$core" | tr -cd . | wc -c)" -eq 2 ] || die "version must begin vX.Y.Z: $1"
}

# controller-ea/config.yaml with the app's identity replaced. The file header,
# and the comments above `image:` and `version:`, describe pulling a
# published image, which this app does not do, so they go too.
gen_config() {
    awk -v ver="$2" -v name="$NAME" -v slug="$SLUG" -v url="$FORK_URL" '
    function flush(   i) { for (i = 1; i <= n; i++) print buf[i]; n = 0 }
    BEGIN {
        print "# GENERATED by tools/fork/build_app.sh from controller-ea/config.yaml;"
        print "# regenerate it rather than editing. Only the identity below differs:"
        print "# options, schema, ports and ingress are the EA app'"'"'s, so its options"
        print "# carry over unchanged."
        print "#"
        print "# No image: key, so Supervisor builds the Dockerfile in this folder."
        n = 0; started = 0
    }
    /^#/ { buf[++n] = $0; next }
    {
        if (!started) { n = 0; started = 1 }
        if ($0 ~ /^image:/) { n = 0; next }
        if ($0 ~ /^version:/) { n = 0 }
        flush()
    }
    /^name:/ { print "name: \"" name "\""; next }
    /^version:/ {
        print "# The fork commit this was built from; it is also EM_CONTROLLER_VERSION."
        print "version: \"" ver "\""
        next
    }
    /^slug:/ {
        print "slug: \"" slug "\""
        print "# Off at boot until haos_migrate.sh has stopped the upstream app and"
        print "# turned it on here: both bind the same host ports."
        print "boot: manual"
        next
    }
    /^description:/ {
        print "description: \"The Paraxdev fork of the EchoMuse controller, built on this machine from the fork. Never run it alongside another EchoMuse app: they use the same host ports. It starts with an empty /data; tools/fork/pve.sh migrate copies the upstream app'"'"'s data in.\""
        next
    }
    /^url:/ { print "url: \"" url "\""; next }
    /^panel_title:/ { print "panel_title: EchoMuse fork"; next }
    { print }
    END { flush() }
    ' "$1"
}

# Everything but the identity keys and comments must be the EA file's.
config_body() {
    grep -v -E '^(#|name:|version:|slug:|description:|url:|panel_title:|image:|boot:)' "$1" || true
}

copy_sources() {
    dest=$1
    mkdir -p "$dest/static"
    cp "$CTRL"/*.py "$dest/"
    cp "$CTRL/requirements.txt" "$dest/"
    cp -R "$CTRL/esphome" "$CTRL/device_payloads" "$CTRL/sounds" "$dest/"
    # vendor/ and dashboard.js are build outputs that a local run can leave
    # behind; the image makes its own.
    for f in "$CTRL"/static/*; do
        case ${f##*/} in
            vendor | dashboard.js) continue ;;
        esac
        cp -R "$f" "$dest/static/"
    done
}

write_overlay_dockerfile() {
    esbuild=$(grep -E '^RUN .*esbuild /app/static/dashboard\.jsx' "$CTRL/Dockerfile" || true)
    [ -n "$esbuild" ] && [ "$(printf '%s\n' "$esbuild" | wc -l)" -eq 1 ] \
        || die "controller/Dockerfile has no single esbuild RUN line to reuse; use --full"
    cat >"$1" <<EOF
# GENERATED by tools/fork/build_app.sh (overlay); regenerate it rather than
# editing.
#
# The fork's code over the published upstream image. Everything the image
# build downloads or computes (wheels, ffmpeg, vendored JS and fonts, the DTLN
# and wake-word runtime models) is the base image's; only code changes here.
FROM $BASE_IMAGE

# build_app.sh already refused this mode if the fork's requirements differ
# from the base commit's. This asks the image itself, in case the pin moved.
COPY app/requirements.txt /tmp/fork-requirements.txt
RUN if ! cmp -s /tmp/fork-requirements.txt /app/requirements.txt; then \\
      echo "requirements.txt differs from the base image; build with tools/fork/build_app.sh --full" >&2; \\
      exit 1; \\
    fi && rm /tmp/fork-requirements.txt

# Whatever the fork deleted must not survive from the base. static/vendor
# (React, xterm, fonts) and /app/models stay: they are not in git.
RUN rm -rf /app/*.py /app/esphome /app/device_payloads /app/sounds && \\
    find /app/static -maxdepth 1 -type f -delete
COPY app/ /app/

# controller/Dockerfile's own esbuild step, verbatim: the published image
# deletes esbuild after use, and dashboard.js is not in git.
$esbuild

ENV EM_CONTROLLER_VERSION="$VERSION"
LABEL org.opencontainers.image.source="$FORK_URL"
EOF
}

write_full_dockerfile() {
    sed "s|^ARG EM_CONTROLLER_VERSION=dev\$|ARG EM_CONTROLLER_VERSION=$VERSION|" "$CTRL/Dockerfile" >"$1"
    grep -q "^ARG EM_CONTROLLER_VERSION=$VERSION\$" "$1" \
        || die "controller/Dockerfile has no 'ARG EM_CONTROLLER_VERSION=dev' line to stamp"
}

write_docs() {
    cat >"$1" <<EOF
# EchoMuse (Paraxdev fork)

The EchoMuse controller built from the Paraxdev fork ($FORK_URL), as a
local app on this Home Assistant. Version: \`$VERSION\` ($MODE build).

- Never run it at the same time as another EchoMuse app. They use the same
  host ports (8767, 8768, 8770 and the satellite ports).
- It has its own /data. Run \`tools/fork/pve.sh migrate\` on the Proxmox host
  to copy the upstream app's database, certificates and models in, and
  \`tools/fork/pve.sh rollback\` to go back.
- The options are the Early Access app's; see the upstream documentation for
  what each one does.

Instructions: tools/fork/README.md in the fork.
EOF
}

MODE=overlay
OUT=""
VERSION=""
while [ $# -gt 0 ]; do
    case $1 in
        --full) MODE=full ;;
        --out)
            [ $# -ge 2 ] || die "--out needs a directory"
            OUT=$2
            shift
            ;;
        --version)
            [ $# -ge 2 ] || die "--version needs a value"
            VERSION=$2
            shift
            ;;
        --fingerprint)
            fingerprint "${2-}"
            exit 0
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
    shift
done

[ -f "$CTRL/Dockerfile" ] && [ -f "$EA/config.yaml" ] || die "not an EchoMuse checkout: $ROOT"
OUT=${OUT:-"$ROOT/dist/$SLUG"}
[ -n "$VERSION" ] || VERSION=$(stamp_version)
check_version "$VERSION"

if [ "$MODE" = overlay ]; then
    have=$(fingerprint "")
    if [ "$have" != "$BASE_FINGERPRINT" ]; then
        die "this checkout's image inputs ($FP_FILES) differ from $BASE_REF, which the overlay base was built from. Build with --full, or move BASE_IMAGE/BASE_REF/BASE_FINGERPRINT to a newer upstream image (fingerprint $have)."
    fi
fi

if [ -e "$OUT" ] && [ ! -f "$OUT/$MARKER" ]; then
    die "$OUT exists and was not written by this script; refusing to replace it"
fi
tmp="$OUT.new.$$"
mkdir -p "$(dirname -- "$OUT")"
mkdir "$tmp" || die "cannot create $tmp"
trap 'rm -rf "$tmp"' EXIT

if [ "$MODE" = overlay ]; then
    copy_sources "$tmp/app"
    write_overlay_dockerfile "$tmp/Dockerfile"
else
    copy_sources "$tmp"
    cp -R "$CTRL/tools" "$tmp/"
    write_full_dockerfile "$tmp/Dockerfile"
fi
find "$tmp" -type d -name __pycache__ -prune -exec rm -rf {} \;
find "$tmp" -type f -name '*.pyc' -exec rm -f {} \;

gen_config "$EA/config.yaml" "${VERSION#v}" >"$tmp/config.yaml"
mkdir -p "$tmp/translations"
cp "$EA/translations/en.yaml" "$tmp/translations/en.yaml"
cp "$EA/icon.png" "$EA/logo.png" "$tmp/"
write_docs "$tmp/DOCS.md"

[ "$(grep -c "^slug: \"$SLUG\"\$" "$tmp/config.yaml")" -eq 1 ] || die "config.yaml rewrite lost the slug"
[ "$(grep -c "^version: \"${VERSION#v}\"\$" "$tmp/config.yaml")" -eq 1 ] || die "config.yaml rewrite lost the version"
! grep -q '^image:' "$tmp/config.yaml" || die "config.yaml still has an image: key"
config_body "$EA/config.yaml" >"$tmp/.ea_body"
config_body "$tmp/config.yaml" >"$tmp/.app_body"
cmp -s "$tmp/.ea_body" "$tmp/.app_body" || die "config.yaml differs from controller-ea/config.yaml beyond the identity keys"
rm -f "$tmp/.ea_body" "$tmp/.app_body"
# Supervisor reads every config.json/yaml/yml below the local apps folder as
# an app of its own.
stray=$(find "$tmp" -mindepth 2 -type f \( -name config.json -o -name config.yaml -o -name config.yml \) | head -n 1)
[ -z "$stray" ] || die "a second app config would be found by Supervisor: $stray"

commit=unknown
if command -v git >/dev/null 2>&1; then
    commit=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
fi
{
    echo "mode=$MODE"
    echo "version=$VERSION"
    echo "commit=$commit"
    [ "$MODE" = overlay ] && echo "base=$BASE_IMAGE"
    echo "built=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$tmp/$MARKER"

rm -rf "$OUT"
mv "$tmp" "$OUT"
trap - EXIT

echo "App folder written to $OUT"
echo "  slug:    $SLUG (installed as local_$SLUG)"
echo "  version: $VERSION"
echo "  mode:    $MODE"
[ "$MODE" = overlay ] && echo "  base:    $BASE_IMAGE"
exit 0
