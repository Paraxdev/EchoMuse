#!/bin/sh
# haos_install.sh PKGDIR: put the app folder that pve.sh delivered into the
# local apps folder, then install, update or rebuild it through Supervisor.
# Runs as root on the Home Assistant OS host; pve.sh calls it.
#
# PKGDIR holds echomuse_fork/ (from build_app.sh) and tools/ (these scripts).
# It never touches the upstream app, never starts the fork, and copies no
# data: a first install stays stopped with boot off until haos_migrate.sh.
# Supervisor itself restarts the fork after an update if it was running.
set -eu
# shellcheck source=tools/fork/haos_lib.sh
. "$(dirname -- "$0")/haos_lib.sh"

[ $# -eq 1 ] || die "usage: haos_install.sh PKGDIR"
PKG=$1
APP_SRC="$PKG/echomuse_fork"
[ -f "$APP_SRC/config.yaml" ] && [ -f "$APP_SRC/.echomuse_fork_app" ] \
    || die "$APP_SRC is not an app folder written by build_app.sh"
[ -f "$PKG/tools/haos_lib.sh" ] || die "$PKG/tools has no helper scripts"
want=$(sed -n 's/^version:[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' "$APP_SRC/config.yaml")
[ -n "$want" ] || die "no version: in $APP_SRC/config.yaml"

preflight
local_root=$(sv_dir local) || die "no local apps folder under $EMF_MNT/supervisor (apps/local or addons/local)"
target="$local_root/echomuse_fork"

if [ -e "$target" ] && [ ! -f "$target/.echomuse_fork_app" ]; then
    die "$target exists but was not written by these tools; move it away first"
fi

step "Installing the helper scripts into $WORK/tools"
mkdir -p "$WORK"
rm -rf "$WORK/tools.new"
cp -R "$PKG/tools" "$WORK/tools.new"
rm -rf "$WORK/tools"
mv "$WORK/tools.new" "$WORK/tools"

# The previous folder goes outside the local apps folder: Supervisor reads
# every config.yaml below it as an app, and two would share one slug.
step "Replacing $target (the previous one is kept as $WORK/app.prev)"
if [ -e "$target" ]; then
    rm -rf "$WORK/app.prev"
    mv "$target" "$WORK/app.prev"
fi
if ! mv "$APP_SRC" "$target"; then
    [ -d "$WORK/app.prev" ] && mv "$WORK/app.prev" "$target"
    die "could not move the new app folder into $target"
fi
say "Version $want, $(sed -n 's/^mode=//p' "$target/.echomuse_fork_app") build."

step "Reloading the app store"
ha_cli store reload || ha_app reload || die "the store reload failed"
if ! ha_app info "$FORK_SLUG" >/dev/null 2>&1; then
    ha_cli supervisor logs 2>/dev/null | tail -n 40 >&2 || true
    die "Supervisor does not list $FORK_SLUG after the reload; it probably refused config.yaml (Supervisor log above)"
fi

if app_installed "$FORK_SLUG"; then
    have=$(app_field "$FORK_SLUG" version) || die "cannot read the installed version of $FORK_SLUG"
    if [ "$have" = "$want" ]; then
        action=rebuild
    else
        action=update
    fi
else
    action=install
fi

step "ha $HA_GROUP $action $FORK_SLUG: Supervisor builds the image now, which takes minutes"
if ! out=$(ha_app "$action" "$FORK_SLUG" 2>&1); then
    say "$out"
    # The CLI gives up on a long build before Supervisor does; anything else
    # is a real failure, and the Supervisor log says why.
    case $out in
        *[Tt]imeout* | *deadline*)
            warn "the CLI timed out; waiting for Supervisor to finish the build"
            ;;
        *)
            ha_cli supervisor logs 2>/dev/null | tail -n 60 >&2 || true
            die "ha $HA_GROUP $action $FORK_SLUG failed (Supervisor log above)"
            ;;
    esac
else
    [ -z "$out" ] || say "$out"
fi

step_s=$EMF_POLL
[ "$step_s" -gt 0 ] || step_s=1
left=$((1800 / step_s))
while :; do
    if app_installed "$FORK_SLUG" && [ "$(app_field "$FORK_SLUG" version)" = "$want" ]; then
        break
    fi
    if [ "$left" -le 0 ]; then
        ha_cli supervisor logs 2>/dev/null | tail -n 60 >&2 || true
        die "$FORK_SLUG is not at $want after 30 minutes (Supervisor log above)"
    fi
    left=$((left - 1))
    sleep "$EMF_POLL"
done

step "Done"
show_app "$FORK_SLUG"
show_app "$UPSTREAM_SLUG"
fork_data=$(data_dir "$FORK_SLUG")
if [ ! -f "$fork_data/$MIGRATED" ]; then
    say ""
    say "The fork has no migrated data yet. Next, on the Proxmox host:"
    say "  sh tools/fork/pve.sh migrate"
fi
