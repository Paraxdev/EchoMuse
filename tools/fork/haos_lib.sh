# shellcheck shell=sh disable=SC2034
# haos_lib.sh: shared by the haos_*.sh scripts, which run as root on the Home
# Assistant OS host (from Proxmox, through `qm guest exec`). Sourced, not run.
#
# Supervisor is driven from inside the hassio_cli container two ways: the ha
# CLI for actions (store reload, install, update, start, stop), and the REST
# API through curl and jq for reads, boot mode and options, which the CLI has
# no stable command for. preflight() checks both before anything changes.
#
# EMF_MNT relocates every path and EMF_POLL sets the poll interval; the tests
# use them to run these scripts against a scratch tree and a fake docker.
# The globals below are read by the scripts that source this (SC2034).

# Appended, so whatever started us (qemu-ga, a test) keeps its own order.
PATH=$PATH:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

EMF_MNT=${EMF_MNT:-/mnt/data}
EMF_POLL=${EMF_POLL:-2}
WORK="$EMF_MNT/echomuse_fork"
FORK_SLUG=local_echomuse_fork
UPSTREAM_SLUG=${EMF_UPSTREAM_SLUG:-acc06409_controller-ea}
MIGRATED=.echomuse_fork_migrated
CLI=hassio_cli
HA_GROUP=apps
API=/addons

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

# Home Assistant renamed add-ons to apps and the host paths moved with it;
# accept either spelling.
sv_dir() {
    for _d in "$EMF_MNT/supervisor/apps/$1" "$EMF_MNT/supervisor/addons/$1"; do
        if [ -d "$_d" ]; then
            printf '%s\n' "$_d"
            return 0
        fi
    done
    return 1
}

data_dir() {
    _root=$(sv_dir data) || die "no app data folder under $EMF_MNT/supervisor (apps/data or addons/data)"
    printf '%s/%s\n' "$_root" "$1"
}

# Run inside hassio_cli, where SUPERVISOR_TOKEN is set. Constant text; method,
# path and body arrive as arguments and stdin, never spliced into it.
# shellcheck disable=SC2016
CURL_GET='curl -sS -X "$1" -H "Authorization: Bearer $SUPERVISOR_TOKEN" -w "\n%{http_code}" "http://supervisor$2"'
# shellcheck disable=SC2016
CURL_BODY='curl -sS -X "$1" -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" --data-binary @- -w "\n%{http_code}" "http://supervisor$2"'

# sv_call METHOD PATH [JSON]: the response body on success (HTTP 200).
sv_call() {
    if [ $# -ge 3 ]; then
        _r=$(printf '%s' "$3" | docker exec -i "$CLI" sh -c "$CURL_BODY" sh "$1" "$API$2") || return 1
    else
        _r=$(docker exec "$CLI" sh -c "$CURL_GET" sh "$1" "$API$2") || return 1
    fi
    _code=$(printf '%s\n' "$_r" | tail -n 1)
    _body=$(printf '%s\n' "$_r" | sed '$d')
    if [ "$_code" != 200 ]; then
        printf 'Supervisor API %s %s answered HTTP %s: %s\n' "$1" "$API$2" "$_code" "$_body" >&2
        return 1
    fi
    printf '%s\n' "$_body"
}

jqr() { docker exec -i "$CLI" jq -r "$1"; }

ha_cli() { docker exec "$CLI" ha "$@"; }
ha_app() { docker exec "$CLI" ha "$HA_GROUP" "$@"; }

app_field() {
    _j=$(sv_call GET "/$1/info") || return 1
    printf '%s\n' "$_j" | jqr ".data.$2"
}

app_installed() {
    _j=$(sv_call GET "") || die "cannot list the installed apps"
    printf '%s\n' "$_j" | jqr '.data.addons[]?.slug, .data.apps[]?.slug' | grep -qx "$1"
}

preflight() {
    if [ "$EMF_MNT" = /mnt/data ] && [ "$(id -u)" != 0 ]; then
        die "run this as root on the Home Assistant OS host"
    fi
    command -v docker >/dev/null 2>&1 || die "no docker here; this runs on the Home Assistant OS host"
    [ "$(docker inspect --format '{{.State.Running}}' "$CLI" 2>/dev/null)" = true ] \
        || die "the $CLI container is not running"
    docker exec "$CLI" sh -c 'command -v curl >/dev/null && command -v jq >/dev/null && [ -n "${SUPERVISOR_TOKEN:-}" ]' \
        || die "$CLI has no curl, jq or SUPERVISOR_TOKEN, which these scripts need to reach Supervisor"
    if docker exec "$CLI" ha apps --help >/dev/null 2>&1; then
        HA_GROUP=apps
    else
        HA_GROUP=addons
    fi
    API=/addons
    if ! sv_call GET "" >/dev/null 2>&1; then
        API=/apps
        sv_call GET "" >/dev/null || die "the Supervisor API answers neither /addons nor /apps"
    fi
}

# wait_state SLUG STATE SECONDS
wait_state() {
    _step=$EMF_POLL
    [ "$_step" -gt 0 ] || _step=1
    _left=$(($3 / _step))
    while :; do
        _s=$(app_field "$1" state 2>/dev/null) || _s=unknown
        [ "$_s" = "$2" ] && return 0
        if [ "$_left" -le 0 ]; then
            warn "$1 is '$_s' after ${3}s, expected '$2'"
            return 1
        fi
        _left=$((_left - 1))
        sleep "$EMF_POLL"
    done
}

stop_app() {
    _s=$(app_field "$1" state) || die "cannot read the state of $1"
    case $_s in
        started | startup)
            say "Stopping $1 (it is $_s)."
            ha_app stop "$1" || die "ha $HA_GROUP stop $1 failed"
            wait_state "$1" stopped 120 || die "$1 did not stop"
            ;;
        *) say "$1 is $_s; nothing to stop." ;;
    esac
}

start_app() {
    say "Starting $1."
    ha_app start "$1" || die "ha $HA_GROUP start $1 failed; see: docker exec $CLI ha $HA_GROUP logs $1"
    wait_state "$1" started 180 || die "$1 did not reach 'started'; see: docker exec $CLI ha $HA_GROUP logs $1"
}

set_boot() {
    sv_call POST "/$1/options" "{\"boot\":\"$2\"}" >/dev/null || die "could not set boot to $2 on $1"
    _b=$(app_field "$1" boot) || die "cannot read the boot mode of $1"
    [ "$_b" = "$2" ] || die "$1 boot mode is '$_b' after setting it to '$2'"
    say "$1 boot mode: $2"
}

# Running containers with this host directory mounted, whatever Supervisor
# named them. This, not the reported state, is what makes a copy unsafe.
mounted_by() {
    for _id in $(docker ps -q --no-trunc); do
        docker inspect --format '{{.Name}}|{{range .Mounts}}{{.Source}};{{end}}' "$_id" 2>/dev/null || true
    done | while IFS='|' read -r _name _mounts; do
        case ";$_mounts" in
            *";$1;"*) printf '%s\n' "${_name#/}" ;;
        esac
    done
}

# One line per file, path last, sorted: equal output means equal trees.
hash_tree() {
    if command -v sha256sum >/dev/null 2>&1; then
        _h=sha256sum
    elif command -v md5sum >/dev/null 2>&1; then
        _h=md5sum
    else
        _h="wc -c"
    fi
    # shellcheck disable=SC2086
    (cd "$1" && find . -type f -exec $_h {} +) | sort -k2
}

free_kb() { df -kP "$1" | awk 'NR == 2 { print $4 }'; }
used_kb() { du -sk "$1" | awk '{ print $1 }'; }

show_app() {
    if ! app_installed "$1"; then
        say "  $1: not installed"
        return 0
    fi
    _j=$(sv_call GET "/$1/info") || return 0
    say "  $1: $(printf '%s\n' "$_j" | jqr '"state \(.data.state), boot \(.data.boot), version \(.data.version)"')"
}
