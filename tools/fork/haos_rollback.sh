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
trap on_exit EXIT
take_lock
app_installed "$UPSTREAM_SLUG" \
    || die "$UPSTREAM_SLUG is not installed; give the upstream app's slug with --from"

# pve.sh prints the hints on the Proxmox host, so the commands in them are in
# the form that runs there; in the VM itself, drop 'qm guest exec <vmid> --'.
ha_by_hand="qm guest exec <vmid> -- docker exec $CLI ha $HA_GROUP"

step "1/3 Stop $FORK_SLUG and turn off its start at boot"
fail_hint="The rollback stopped part way and nothing was deleted, but nothing may be running now: $FORK_SLUG can be stopped with Start on boot still on, and $UPSTREAM_SLUG stopped with it off. Run 'sh tools/fork/pve.sh rollback' again, or start ONE of them by hand, never both (they bind the same ports): Settings > Apps > the app > Start, with Start on boot on for that one only, or on the Proxmox host (<vmid> is the Home Assistant VM, 100 by default)
  $ha_by_hand start $UPSTREAM_SLUG    (the upstream app)
  $ha_by_hand start $FORK_SLUG    (the fork)"
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
fail_hint="$FORK_SLUG is stopped with Start on boot off, and $UPSTREAM_SLUG has Start on boot on but did not start. Start it with Settings > Apps > the upstream app > Start, or on the Proxmox host (<vmid> is the Home Assistant VM, 100 by default)
  $ha_by_hand start $UPSTREAM_SLUG
and read why it failed with
  $ha_by_hand logs $UPSTREAM_SLUG"
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
      boot on, and "EchoMuse (Paraxdev fork)" is stopped with Start on boot off
      (Supervisor may show it as failed: the controller does not exit cleanly
      when it is stopped).
  [ ] The Echo reconnects within a few minutes and its Status tab Link row
      reads "wss (TLS)".
  [ ] Home Assistant's ESPHome entries for the Echo are available again.
  [ ] An Echo on fork firmware keeps the fork's Patience (default 2) until it
      restarts, because the upstream controller never sends owwPatience. If
      the Echo detects its own wake word (Wake word set to On this Echo, or
      shadow), restart the Echo now; next time, set Patience to 1 in the fork
      dashboard before rolling back.

If the Echo was paired again while on the fork (the Pair button, or its
action button held for 5 seconds), it now holds a token the upstream database
has never seen, and the upstream controller refuses it: remove it in the
dashboard and approve it again, or hold its action button for 5 seconds and
approve the pairing request.

Settings changed on the fork since the migration exist only in the fork's
data. A later 'pve.sh migrate' asks whether to copy the upstream data again
(--recopy) or keep the fork's (--keep-fork-data).
EOF
