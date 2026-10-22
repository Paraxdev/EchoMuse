#!/bin/sh
# haos_migrate.sh: move the controller from the upstream EchoMuse app to the
# fork app, keeping the paired Echoes, Home Assistant's ESPHome entries and
# the app options. Runs as root on the Home Assistant OS host; from Proxmox,
# `pve.sh migrate`.
#
# The upstream app's data is COPIED and never modified, so going back is
# starting it again (haos_rollback.sh). In order:
#   1. stop the upstream app and turn off its start at boot
#   2. refuse to continue while any running container has its data mounted
#   3. stop the fork app; if Supervisor has not made its data folder yet,
#      start and stop it once
#   4. move the fork's current data folder to $WORK/backups/<time>/
#   5. cp -a the whole upstream data folder into the fork's (echomuse.db with
#      its -wal and -shm, tls/ with the CA every Echo verifies, oww_models/,
#      firmware/, recordings) and compare every file
#   6. copy the upstream options and sidebar setting, turn on the fork's
#      start at boot, start it
#
# Once a copy is done it is not repeated, because that would discard what
# the fork has written since. --recopy repeats it, after the same backup.
#
# Usage: haos_migrate.sh [--recopy | --keep-fork-data] [--no-start] [--from SLUG]
set -eu
# shellcheck source=tools/fork/haos_lib.sh
. "$(dirname -- "$0")/haos_lib.sh"

recopy=0
keep=0
start=1
while [ $# -gt 0 ]; do
    case $1 in
        --recopy) recopy=1 ;;
        --keep-fork-data) keep=1 ;;
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
[ "$recopy" = 0 ] || [ "$keep" = 0 ] || die "--recopy and --keep-fork-data contradict each other"

# What to do next if anything below fails, given how far it got.
fail_hint=""
on_exit() {
    rc=$?
    release_lock
    if [ "$rc" != 0 ] && [ -n "$fail_hint" ]; then
        printf '\n%s\n' "$fail_hint" >&2
    fi
}
trap on_exit EXIT

preflight
take_lock
src=$(data_dir "$UPSTREAM_SLUG")
dst=$(data_dir "$FORK_SLUG")

app_installed "$UPSTREAM_SLUG" \
    || die "$UPSTREAM_SLUG is not installed; give the upstream app's slug with --from"
app_installed "$FORK_SLUG" || die "$FORK_SLUG is not installed; run 'pve.sh deploy' first"
[ -f "$src/echomuse.db" ] || die "$src has no echomuse.db; is $UPSTREAM_SLUG the app the Echo uses?"
for f in ca.key ca.pem server.key server.pem; do
    [ -f "$src/tls/$f" ] || die "$src/tls/$f is missing; an Echo holding the CA could not verify a controller without all four"
done

copy=1
if [ -f "$dst/$MIGRATED" ] && [ "$recopy" = 0 ]; then
    copy=0
    say "The fork's data was already migrated:"
    sed 's/^/  /' "$dst/$MIGRATED"
    # The upstream app has run since (a rollback, most likely), so the two
    # databases have both moved on and only the owner can say which is right.
    newer=$(find "$src" -maxdepth 1 -name 'echomuse.db*' -newer "$dst/$MIGRATED" | head -n 1)
    if [ -n "$newer" ] && [ "$keep" = 0 ]; then
        die "$UPSTREAM_SLUG has written to its database since then ($newer). Run again with --recopy to take its current data (the fork's is backed up first), or with --keep-fork-data to start the fork on its own."
    fi
    say "Not copying again; --recopy would (after a backup)."
fi

step "1/6 Stop $UPSTREAM_SLUG and turn off its start at boot"
fail_hint="Nothing has been copied. 'sh tools/fork/pve.sh rollback' starts the upstream app again."
stop_app "$UPSTREAM_SLUG"
set_boot "$UPSTREAM_SLUG" manual

step "2/6 Check that no running container has $src mounted"
users=$(mounted_by "$src")
[ -z "$users" ] || die "still mounted by: $users. Refusing to copy a database that is in use."
say "None."

if [ "$copy" = 1 ]; then
    step "3/6 Stop $FORK_SLUG and make sure its data folder exists"
    stop_app "$FORK_SLUG"
    if [ ! -d "$dst" ]; then
        say "$dst does not exist yet, so $FORK_SLUG is started once for Supervisor to create it and stopped again. The certificates it generates meanwhile are replaced below."
        start_app "$FORK_SLUG"
        stop_app "$FORK_SLUG"
        [ -d "$dst" ] || die "$dst still does not exist"
    fi
    users=$(mounted_by "$dst")
    [ -z "$users" ] || die "$dst is still mounted by: $users"

    need=$(used_kb "$src")
    free=$(free_kb "$EMF_MNT")
    [ "$free" -gt $((need + need / 10 + 102400)) ] \
        || die "not enough space on $EMF_MNT: the copy needs about $((need / 1024)) MB plus a margin, $((free / 1024)) MB is free"

    ts=$(date +%Y%m%d-%H%M%S)
    bak="$WORK/backups/$ts"
    step "4/6 Move the fork's current data folder to $bak"
    [ ! -e "$bak/$FORK_SLUG" ] || die "$bak/$FORK_SLUG already exists; run again in a second"
    mkdir -p "$bak"
    fail_hint="The fork's previous data folder is $bak/$FORK_SLUG. The upstream data is untouched; 'sh tools/fork/pve.sh rollback' starts the upstream app again."
    mv "$dst" "$bak/$FORK_SLUG"
    mkdir "$dst"
    say "Backed up."

    step "5/6 Copy $src into $dst"
    cp -a "$src/." "$dst/"
    hash_tree "$src" >"$bak/upstream.sums"
    hash_tree "$dst" >"$bak/fork.sums"
    if [ "$(cat "$bak/upstream.sums")" != "$(cat "$bak/fork.sums")" ]; then
        die "the copy does not match the original (see $bak/upstream.sums and $bak/fork.sums)"
    fi
    {
        echo "from=$UPSTREAM_SLUG"
        echo "at=$ts"
        echo "backup=$bak/$FORK_SLUG"
    } >"$dst/$MIGRATED"
    fail_hint="The copy is complete and verified. Retry with 'sh tools/fork/pve.sh migrate' (it does not copy again), or go back with 'sh tools/fork/pve.sh rollback'."
    say "Copied and compared $(wc -l <"$bak/fork.sums") files, $((need / 1024)) MB."

    step "6/6 Carry the options over, turn on the fork's start at boot, start it"
    # Same schema on both apps, so the upstream options are valid as they
    # are. Supervisor stores them per app; options.json in the copied folder
    # is rewritten from the store at the next start.
    up_info=$(sv_call GET "/$UPSTREAM_SLUG/info") || die "cannot read $UPSTREAM_SLUG's options"
    body=$(printf '%s\n' "$up_info" | jqr '{options: .data.options} + (if (.data.ingress_panel | type) == "boolean" then {ingress_panel: .data.ingress_panel} else {} end) | tojson')
    if sv_call POST "/$FORK_SLUG/options" "$body" >/dev/null; then
        a=$(printf '%s\n' "$up_info" | jqr '.data.options | to_entries | sort_by(.key) | tojson')
        b=$(app_field "$FORK_SLUG" 'options | to_entries | sort_by(.key) | tojson') || b=""
        if [ -n "$a" ] && [ "$a" = "$b" ]; then
            say "Options copied."
        else
            warn "the fork's options do not read back the same; compare them in Settings > Apps"
        fi
    else
        warn "could not copy the options. Enter these in the fork app's Configuration tab:"
        printf '%s\n' "$up_info" | jqr '.data.options' >&2
    fi
else
    step "6/6 Turn on the fork's start at boot and start it"
    fail_hint="Nothing was copied. Retry with 'sh tools/fork/pve.sh migrate', or go back with 'sh tools/fork/pve.sh rollback'."
fi

set_boot "$FORK_SLUG" auto
if [ "$start" = 1 ]; then
    if [ "$(app_field "$FORK_SLUG" state)" = started ]; then
        say "$FORK_SLUG is already running."
    else
        start_app "$FORK_SLUG"
    fi
    sleep "$EMF_POLL"
    say ""
    say "Last lines of the fork's log:"
    ha_app logs "$FORK_SLUG" 2>/dev/null | tail -n 15 || true
else
    say "Not started (--no-start). It starts at the next boot, or: docker exec $CLI ha $HA_GROUP start $FORK_SLUG"
fi

version=$(app_field "$FORK_SLUG" version) || version=unknown
cat <<EOF

Check, in this order:
  [ ] Settings > Apps: "EchoMuse (Paraxdev fork)" is started with Start on boot on,
      and the upstream app ($UPSTREAM_SLUG) is stopped with Start on boot off.
  [ ] The fork's log reads "Opening database: /data/echomuse.db", then
      "Controller ready", and has no Traceback.
  [ ] The "EchoMuse fork" sidebar entry opens the dashboard, and its header
      shows v$version.
  [ ] Within a few minutes the Echo is connected, and its Status tab Link row
      reads "wss (TLS)". If it shows as pending approval instead, the database
      did not come across: do not approve it, roll back.
  [ ] Home Assistant's ESPHome entries for the Echo (voice on its stored port,
      16101 here, Bluetooth proxy 17101) are available again, same entities.
  [ ] A wake word, a question and an answer work end to end.

Going back: sh tools/fork/pve.sh rollback (the upstream data was not changed).
EOF
