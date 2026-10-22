#!/bin/sh
# haos_status.sh: read-only report on both EchoMuse apps, their data folders
# and the fork's app folder. Runs as root on the Home Assistant OS host; from
# Proxmox, `pve.sh status`.
#
# Usage: haos_status.sh [--logs N] [--from SLUG]
set -eu
# shellcheck source=tools/fork/haos_lib.sh
. "$(dirname -- "$0")/haos_lib.sh"

logs=0
while [ $# -gt 0 ]; do
    case $1 in
        --logs)
            [ $# -ge 2 ] || die "--logs needs a line count"
            case $2 in
                '' | *[!0-9]*) die "--logs needs a line count" ;;
            esac
            logs=$2
            shift
            ;;
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
say "Apps (ha CLI group '$HA_GROUP', API $API):"
show_app "$UPSTREAM_SLUG"
show_app "$FORK_SLUG"

say ""
say "Data folders:"
for slug in "$UPSTREAM_SLUG" "$FORK_SLUG"; do
    d=$(data_dir "$slug")
    if [ -d "$d" ]; then
        say "  $d: $(($(used_kb "$d") / 1024)) MB, mounted by: $(mounted_by "$d" | tr '\n' ' ')"
    else
        say "  $d: absent"
    fi
done
fork_data=$(data_dir "$FORK_SLUG")
if [ -f "$fork_data/$MIGRATED" ]; then
    say "  migrated: $(tr '\n' ' ' <"$fork_data/$MIGRATED")"
fi

local_root=$(sv_dir local) || local_root=""
if [ -n "$local_root" ] && [ -f "$local_root/echomuse_fork/.echomuse_fork_app" ]; then
    say ""
    say "App folder $local_root/echomuse_fork:"
    sed 's/^/  /' "$local_root/echomuse_fork/.echomuse_fork_app"
fi
if [ -d "$WORK/backups" ]; then
    say ""
    say "Backups in $WORK/backups: $(find "$WORK/backups" -mindepth 1 -maxdepth 1 -exec basename {} \; | sort | tr '\n' ' ')"
fi

if [ "$logs" -gt 0 ]; then
    say ""
    say "Last $logs lines of $FORK_SLUG's log:"
    ha_app logs "$FORK_SLUG" 2>/dev/null | tail -n "$logs" || true
fi
