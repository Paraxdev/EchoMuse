#!/bin/sh
# haos_rollback.sh: go back from the fork app to the upstream one. Stops the
# fork and turns off its start at boot, then turns on the upstream app's and
# starts it. Runs as root on the Home Assistant OS host; from Proxmox,
# `pve.sh rollback`. Deletes nothing.
#
# The migration only copied from the upstream data folder, so the upstream
# app comes back exactly as it was stopped. What the fork wrote since stays
# in the fork's own folder.
#
# Usage: haos_rollback.sh [--no-start] [--from SLUG]
set -eu
# shellcheck source=tools/fork/haos_lib.sh
. "$(dirname -- "$0")/haos_lib.sh"

start=1
while [ $# -gt 0 ]; do
    case $1 in
        --no-start) start=0 ;;
        --from)
            [ $# -ge 2 ] || die "--from needs the upstream app's slug"
            UPSTREAM_SLUG=$2
            shift
            ;;
        -h | --help)
            sed -n 's/^# Usage: //p' "$0"
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

preflight
app_installed "$UPSTREAM_SLUG" \
    || die "$UPSTREAM_SLUG is not installed; give the upstream app's slug with --from"

step "1/3 Stop $FORK_SLUG and turn off its start at boot"
if app_installed "$FORK_SLUG"; then
    stop_app "$FORK_SLUG"
    set_boot "$FORK_SLUG" manual
else
    say "$FORK_SLUG is not installed."
fi
# Both bind the same host ports, so the upstream app must not start while
# anything still runs on the fork's data.
fork_data=$(data_dir "$FORK_SLUG")
users=$(mounted_by "$fork_data")
[ -z "$users" ] || die "$fork_data is still mounted by: $users; not starting $UPSTREAM_SLUG beside it"

step "2/3 Turn on $UPSTREAM_SLUG's start at boot"
set_boot "$UPSTREAM_SLUG" auto

step "3/3 Start $UPSTREAM_SLUG"
if [ "$start" = 0 ]; then
    say "Not started (--no-start). It starts at the next boot, or: docker exec $CLI ha $HA_GROUP start $UPSTREAM_SLUG"
elif [ "$(app_field "$UPSTREAM_SLUG" state)" = started ]; then
    say "$UPSTREAM_SLUG is already running."
else
    start_app "$UPSTREAM_SLUG"
fi

cat <<EOF

Check:
  [ ] Settings > Apps: the upstream app ($UPSTREAM_SLUG) is started with Start on
      boot on, and "EchoMuse (Paraxdev fork)" is stopped with Start on boot off.
  [ ] The Echo reconnects within a few minutes and its Status tab Link row
      reads "wss (TLS)".
  [ ] Home Assistant's ESPHome entries for the Echo are available again.

If the Echo was paired again while on the fork (the Pair button, or its
action button held for 5 seconds), it now holds a token the upstream database
has never seen, and the upstream controller refuses it: remove it in the
dashboard and approve it again, or hold its action button for 5 seconds and
approve the pairing request.

Settings changed on the fork since the migration exist only in the fork's
data. A later 'pve.sh migrate' asks whether to copy the upstream data again
(--recopy) or keep the fork's (--keep-fork-data).
EOF
