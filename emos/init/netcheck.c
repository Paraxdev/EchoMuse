/* Prove init.c's network supervision does what an Echo off the network needs.
 *
 * Everything here is silent when wrong, which is the criterion for a check:
 *
 *   - a link watchdog that acts too soon or too often cuts a working Echo off
 *     every few minutes, and one that never acts leaves it off the network for
 *     days;
 *   - a default gateway read wrongly never answers, so the watchdog acts on a
 *     healthy link;
 *   - a probe exit code read wrongly turns "could not ask" into "no answer";
 *   - a probe or wpa_cli call that never returns ends supervision for the
 *     boot, since the loop waits on both;
 *   - the udhcpc script is the whole of what a lease does to wlan0: deconfig
 *     ignored keeps a lapsed address (#871), a route left behind keeps an old
 *     router in front;
 *   - -a handed to a udhcpc that does not know it ends DHCP before it starts;
 *   - the evidence copy is the only record of an outage that survives the
 *     power cycle, and every copy costs flash.
 *
 * init.c is included whole, as the other checks do, so this drives the real
 * functions. The udhcpc script and the arping probe are run for real, through
 * sh (and mksh, the device's shell, where installed) and through init's own
 * spawn(), against stub busyboxes.
 *
 *   cc -O2 -o netcheck netcheck.c && ./netcheck
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define LEDDIR  "/tmp/emos-netcheck.led"
#define NETLOG  "/tmp/emos-netcheck.log"
#define NETLAST "/tmp/emos-netcheck.last"
#define ROUTES  "/tmp/emos-netcheck.route"
#define main    init_main_unused

#include "init.c"

#undef main

#define T "/tmp/emos-netcheck."

static int failures;

static void check(int ok, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    printf(ok ? "ok    " : "FAIL  ");
    vprintf(fmt, ap);
    printf("\n");
    va_end(ap);
    if (!ok)
        failures++;
}

static void put(const char *path, const char *body)
{
    FILE *f = fopen(path, "w");
    if (!f) {
        printf("FAIL  could not write %s\n", path);
        failures++;
        return;
    }
    fwrite(body, 1, strlen(body), f);
    fclose(f);
}

/* The whole file, NUL-terminated; NULL when it cannot be read. */
static char *slurp(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        return NULL;
    size_t cap = 4096, n = 0;
    char *b = malloc(cap);
    size_t r;
    while ((r = fread(b + n, 1, cap - n - 1, f)) > 0) {
        n += r;
        if (cap - n < 2) {
            cap *= 2;
            b = realloc(b, cap);
        }
    }
    fclose(f);
    b[n] = 0;
    if (len)
        *len = n;
    return b;
}

/* ── The watchdog's ladder ──────────────────────────────────────────────── */

struct act { long t; int a; };

/* A turn every 5s, as net_main runs; `obs` returns -1 for a turn with nothing
 * observed, which is most turns while the link is up (one probe in six). */
static int run(struct linkwd *w, long from, long to, int (*obs)(long),
               struct act *out, int max)
{
    int n = 0;
    for (long t = from; t <= to; t += 5) {
        int o = obs(t);
        if (o < 0)
            continue;
        int a = linkwd_step(w, t, o);
        if (a != WD_NONE && n < max) {
            out[n].t = t;
            out[n].a = a;
            n++;
        }
    }
    return n;
}

static int probe_turn(long t) { return t % WD_PROBE_S == 0; }

static int healthy(long t)   { return probe_turn(t) ? LINK_OK : -1; }
static int silent_all(long t) { return probe_turn(t) ? LINK_SILENT : -1; }
static int nocarrier(long t) { (void)t; return LINK_NOCARRIER; }
static int noaddr(long t)    { (void)t; return LINK_NOADDR; }
static int unknown_all(long t) { return probe_turn(t) ? LINK_UNKNOWN : -1; }

static int half_unknown(long t)
{
    if (!probe_turn(t))
        return -1;
    return (t / WD_PROBE_S) % 2 ? LINK_UNKNOWN : LINK_SILENT;
}

static int short_outage(long t)
{
    if (!probe_turn(t))
        return -1;
    return t <= 150 ? LINK_SILENT : LINK_OK;
}

static int one_answer(long t)
{
    if (!probe_turn(t))
        return -1;
    return t == 210 ? LINK_OK : LINK_SILENT;
}

static long settle_until;
static int settles(long t)
{
    if (!probe_turn(t))
        return -1;
    if (t <= 540)
        return LINK_SILENT;
    return t <= settle_until ? LINK_OK : LINK_SILENT;
}

static int flapping(long t)
{
    if (!probe_turn(t))
        return -1;
    return t % 240 == 0 ? LINK_OK : LINK_SILENT;
}

/* Every kind of bad observation is one outage: the step chosen is the one
 * the CURRENT observation calls for. */
static int mixed(long t)
{
    if (t < 60)  return probe_turn(t) ? LINK_SILENT : -1;
    if (t < 100) return LINK_NOCARRIER;
    if (t < 130) return LINK_NOADDR;
    return probe_turn(t) ? LINK_SILENT : -1;
}

static void expect(const char *label, const struct act *got, int n,
                   const struct act *want, int wn)
{
    int ok = n == wn;
    for (int i = 0; ok && i < n; i++)
        ok = got[i].t == want[i].t && got[i].a == want[i].a;
    if (ok) {
        check(1, "%s (%d step%s)", label, n, n == 1 ? "" : "s");
        return;
    }
    check(0, "%s", label);
    for (int i = 0; i < n || i < wn; i++)
        printf("        #%d got %-11s at %6ld, want %-11s at %6ld\n", i,
               i < n ? wd_step_name[got[i].a] : "-", i < n ? got[i].t : -1L,
               i < wn ? wd_step_name[want[i].a] : "-",
               i < wn ? want[i].t : -1L);
}

/* A long random walk over every observation, against the rules themselves
 * rather than a schedule: never act on an answer or on not knowing, never
 * before a whole grace of bad observations since the last answer, unknown or
 * step, never inside the gap, and always restart without carrier. */
static void random_walk(void)
{
    struct linkwd w;
    linkwd_init(&w);
    unsigned long long seed = 0x9e3779b97f4a7c15ULL;
    int state = LINK_OK, acts = 0, bad = 0;
    long run_start = -1, last_act = -1;
    int steps_before = 0;
    for (long t = 0; t < 7L * 24 * 3600; t += 5) {
        seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
        unsigned r = (unsigned)(seed >> 33);
        if (r % 1000 < 4)           /* a change of state now and then */
            state = (int)((r >> 10) % 5);
        int o = state;
        if ((o == LINK_OK || o == LINK_SILENT || o == LINK_UNKNOWN)
            && !probe_turn(t))
            continue;
        if (o == LINK_OK || o == LINK_UNKNOWN)
            run_start = -1;
        else if (run_start < 0)
            run_start = t;
        steps_before = w.steps;
        int a = linkwd_step(&w, t, o);
        if (a == WD_NONE)
            continue;
        acts++;
        if (o == LINK_OK || o == LINK_UNKNOWN)
            bad++;
        if (run_start < 0 || t - run_start < WD_GRACE_S)
            bad++;
        if (last_act >= 0 && t - last_act < linkwd_gap(steps_before))
            bad++;
        if (o == LINK_NOCARRIER && a != WD_RESTART)
            bad++;
        last_act = t;
        run_start = t;             /* each step waits a whole grace again */
    }
    check(bad == 0 && acts > 20,
          "a week of random links: %d steps, %d broke a rule", acts, bad);
}

static void watchdog(void)
{
    struct linkwd w;
    struct act a[64];
    int n;

    linkwd_init(&w);
    n = run(&w, 0, 3600, healthy, a, 64);
    check(n == 0, "an hour of answers does nothing");

    linkwd_init(&w);
    n = run(&w, 0, 4 * 3600, silent_all, a, 64);
    {
        static const struct act want[] = {
            { 180, WD_REASSOC }, { 360, WD_RECONNECT }, { 540, WD_RESTART },
            { 720, WD_RESTART }, { 1080, WD_RESTART }, { 1800, WD_RESTART },
            { 3240, WD_RESTART }, { 6120, WD_RESTART }, { 9720, WD_RESTART },
            { 13320, WD_RESTART },
        };
        expect("a silent gateway: reassociate at 3 min, reconnect, restart, "
               "then the gap doubles to an hour", a, n, want, 10);
    }

    linkwd_init(&w);
    n = run(&w, 0, 1200, nocarrier, a, 64);
    {
        static const struct act want[] = {
            { 180, WD_RESTART }, { 360, WD_RESTART }, { 540, WD_RESTART },
            { 720, WD_RESTART }, { 1080, WD_RESTART },
        };
        expect("no carrier: the loop already reassociates, so restart first",
               a, n, want, 5);
    }

    linkwd_init(&w);
    n = run(&w, 0, 600, noaddr, a, 64);
    {
        static const struct act want[] = {
            { 180, WD_REASSOC }, { 360, WD_RECONNECT }, { 540, WD_RESTART },
        };
        expect("carrier with no address climbs the same ladder", a, n, want, 3);
    }

    linkwd_init(&w);
    n = run(&w, 0, 300, mixed, a, 64);
    {
        static const struct act want[] = { { 180, WD_REASSOC } };
        expect("silent, no carrier, no address: one outage, timed from its "
               "start", a, n, want, 1);
    }

    linkwd_init(&w);
    n = run(&w, 0, 4 * 3600, unknown_all, a, 64);
    check(n == 0, "hours of not being able to ask do nothing");

    linkwd_init(&w);
    n = run(&w, 0, 4 * 3600, half_unknown, a, 64);
    check(n == 0, "silence broken up by unknowns never adds up to an outage");

    linkwd_init(&w);
    n = run(&w, 0, 3600, short_outage, a, 64);
    check(n == 0 && w.off_since < 0,
          "an outage shorter than the grace does nothing and is forgotten");

    linkwd_init(&w);
    n = run(&w, 0, 595, one_answer, a, 64);
    {
        static const struct act want[] = {
            { 180, WD_REASSOC }, { 420, WD_RECONNECT },
        };
        expect("one answer restarts the grace but not the ladder", a, n,
               want, 2);
    }

    settle_until = 540 + WD_SETTLE_S + 30;    /* answers 570..1170 */
    linkwd_init(&w);
    n = run(&w, 0, 1500, settles, a, 64);
    {
        static const struct act want[] = {
            { 180, WD_REASSOC }, { 360, WD_RECONNECT }, { 540, WD_RESTART },
            { 1380, WD_REASSOC },
        };
        expect("ten minutes of answers reset the ladder", a, n, want, 4);
    }

    settle_until = 540 + WD_SETTLE_S;         /* answers 570..1140 */
    linkwd_init(&w);
    n = run(&w, 0, 1500, settles, a, 64);
    {
        static const struct act want[] = {
            { 180, WD_REASSOC }, { 360, WD_RECONNECT }, { 540, WD_RESTART },
            { 1350, WD_RESTART },
        };
        expect("less than ten minutes of answers does not", a, n, want, 4);
    }

    linkwd_init(&w);
    n = run(&w, 0, 6 * 3600, flapping, a, 64);
    {
        int spaced = 1;
        for (int i = 1; i < n; i++)
            if (a[i].t - a[i - 1].t < WD_GRACE_S)
                spaced = 0;
        check(spaced && n > 0 && n <= 16 && a[n - 1].a == WD_RESTART,
              "a link that answers once every 4 min still backs off "
              "(%d steps in 6 h)", n);
    }

    {
        static const struct { int steps; long gap; } g[] = {
            { 0, 180 }, { 1, 180 }, { 3, 180 }, { 4, 360 }, { 5, 720 },
            { 6, 1440 }, { 7, 2880 }, { 8, 3600 }, { 50, 3600 },
            { 1000000, 3600 },
        };
        int ok = 1;
        for (unsigned i = 0; i < sizeof g / sizeof g[0]; i++)
            if (linkwd_gap(g[i].steps) != g[i].gap) {
                printf("        gap after %d steps is %ld, want %ld\n",
                       g[i].steps, linkwd_gap(g[i].steps), g[i].gap);
                ok = 0;
            }
        check(ok, "the gap between steps: 3 min, doubling from the fourth, "
                  "an hour at most");
    }

    random_walk();
}

/* ── The default gateway ────────────────────────────────────────────────── */

#define HDR "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask" \
            "\t\tMTU\tWindow\tIRTT                                        \n"
#define RT(i, d, g, f, m) i "\t" d "\t" g "\t" f "\t0\t0\t0\t" m \
            "\t0\t0\t0                                                     \n"

static void gw_case(const char *label, const char *table, const char *iface,
                    const char *want)
{
    char got[32] = "unchanged";
    if (table)
        put(ROUTES, table);
    else
        unlink(ROUTES);
    int rc = default_gateway(ROUTES, iface, got, sizeof got);
    if (want)
        check(rc == 0 && strcmp(got, want) == 0, "%-48s -> %s", label,
              rc == 0 ? got : "none");
    else
        check(rc != 0 && strcmp(got, "unchanged") == 0, "%-48s -> none%s",
              label, rc == 0 ? " (but found one)" : "");
}

static void gateway(void)
{
    /* As emOS's table reads behind a home router, padding and all. */
    gw_case("the default route behind a home router",
            HDR RT("wlan0", "00000000", "0101A8C0", "0003", "00000000")
                RT("wlan0", "0001A8C0", "00000000", "0001", "00FFFFFF"),
            "wlan0", "192.168.1.1");
    gw_case("listed after the subnet route",
            HDR RT("wlan0", "0001A8C0", "00000000", "0001", "00FFFFFF")
                RT("wlan0", "00000000", "0101A8C0", "0003", "00000000"),
            "wlan0", "192.168.1.1");
    gw_case("10.0.0.1: the first octet is the low byte",
            HDR RT("wlan0", "00000000", "0100000A", "0003", "00000000"),
            "wlan0", "10.0.0.1");
    gw_case("172.16.254.1",
            HDR RT("wlan0", "00000000", "01FE10AC", "0003", "00000000"),
            "wlan0", "172.16.254.1");
    gw_case("two default routes: the first",
            HDR RT("wlan0", "00000000", "0100000A", "0003", "00000000")
                RT("wlan0", "00000000", "0200000A", "0003", "00000000"),
            "wlan0", "10.0.0.1");
    gw_case("no default route at all",
            HDR RT("wlan0", "0001A8C0", "00000000", "0001", "00FFFFFF"),
            "wlan0", NULL);
    gw_case("a default route on another interface",
            HDR RT("eth0", "00000000", "0100000A", "0003", "00000000"),
            "wlan0", NULL);
    gw_case("... which counts when any interface will do",
            HDR RT("eth0", "00000000", "0100000A", "0003", "00000000"),
            NULL, "10.0.0.1");
    gw_case("an interface whose name starts the same",
            HDR RT("wlan01", "00000000", "0100000A", "0003", "00000000"),
            "wlan0", NULL);
    gw_case("0.0.0.0/1 is not the default route",
            HDR RT("wlan0", "00000000", "0100000A", "0003", "00000080"),
            "wlan0", NULL);
    gw_case("a default route with no gateway (RTF_GATEWAY clear)",
            HDR RT("wlan0", "00000000", "00000000", "0001", "00000000"),
            "wlan0", NULL);
    gw_case("a gateway route that is not up (RTF_UP clear)",
            HDR RT("wlan0", "00000000", "0100000A", "0002", "00000000"),
            "wlan0", NULL);
    gw_case("a line cut short",
            HDR "wlan0\t00000000\t0100000A\t0003\n", "wlan0", NULL);
    gw_case("the header alone", HDR, "wlan0", NULL);
    gw_case("an empty table", "", "wlan0", NULL);
    gw_case("no table to read", NULL, "wlan0", NULL);
    unlink(ROUTES);
}

/* ── The gateway probe, through spawn() ─────────────────────────────────── */

static void probe_case(const char *label, const char *bb, const char *code,
                       int want)
{
    put(T "code", code);
    unlink(T "args");
    int got = gateway_answers(bb, "192.168.1.1");
    check(got == want, "%-44s -> %d", label, got);
}

static void probe(void)
{
    const char *bb = T "bb";
    put(bb,
        "#!/bin/sh\n"
        "echo \"$@\" > " T "args\n"
        "read code < " T "code\n"
        "[ \"$code\" = kill ] && kill -9 $$\n"
        "[ \"$code\" = hang ] && read x < " T "fifo\n"
        "exit $code\n");
    chmod(bb, 0755);
    unlink(T "fifo");
    mkfifo(T "fifo", 0600);

    probe_case("arping heard a reply", bb, "0\n", 1);
    char *args = slurp(T "args", NULL);
    check(args && strcmp(args, "arping -q -f -c 3 -w 3 -I wlan0 "
                               "192.168.1.1\n") == 0,
          "it asks wlan0, three requests in three seconds, first reply wins");
    free(args);
    probe_case("arping heard nothing", bb, "1\n", 0);
    probe_case("arping failed on its own (2)", bb, "2\n", -1);
    probe_case("an exit nobody documented", bb, "3\n", -1);
    probe_case("no arping applet (127)", bb, "127\n", -2);
    probe_case("killed by a signal", bb, "kill\n", -1);
    probe_case("no busybox at all", T "no-such-busybox", "0\n", -2);
    long t0 = mono_ms();
    probe_case("an arping that never returns is stopped", bb, "hang\n", -1);
    long took = mono_ms() - t0;
    check(took >= WD_ARPING_MAX_S * 1000L - 100
          && took < WD_ARPING_MAX_S * 1000L + 3000,
          "... at its limit, %d s (%ld ms)", WD_ARPING_MAX_S, took);
    unlink(bb);
    unlink(T "code");
    unlink(T "args");
    unlink(T "fifo");
}

/* ── The udhcpc script, run by a real shell ─────────────────────────────── */

#define UBB T "ubb"

/* Runs the script for `event`; returns busybox's calls, one per line, and
 * leaves what the script printed in `said`. */
static char *dhcp_event(const char *sh, const char *event, const char *env,
                        int dels, int *status, char **said)
{
    char cmd[512], d[16];
    snprintf(d, sizeof d, "%d\n", dels);
    put(T "dels", d);
    unlink(T "calls");
    put(T "calls", "");
    snprintf(cmd, sizeof cmd, "env -i %s %s " T "script %s > " T "said 2>&1",
             env, sh, event);
    int st = system(cmd);
    *status = WIFEXITED(st) ? WEXITSTATUS(st) : -1;
    *said = slurp(T "said", NULL);
    return slurp(T "calls", NULL);
}

static void dhcp_case(const char *sh, const char *label, const char *event,
                      const char *env, int dels, const char *want_calls,
                      const char *want_said)
{
    int st;
    char *said = NULL;
    char *calls = dhcp_event(sh, event, env, dels, &st, &said);
    int ok = calls && st == 0 && strcmp(calls, want_calls) == 0
          && said && (want_said ? strstr(said, want_said) != NULL
                                : said[0] == 0);
    check(ok, "%-5s %s", sh, label);
    if (!ok)
        printf("        status %d\n        calls:\n%s        said: %s\n", st,
               calls ? calls : "(none)\n", said ? said : "(none)");
    free(calls);
    free(said);
}

static void dhcp_script(const char *sh)
{
    static const char *const lease =
        "interface=wlan0 ip=192.168.1.136 subnet=255.255.255.0 ";
    char env[256];

    dhcp_case(sh, "deconfig clears the address and says so", "deconfig",
              "interface=wlan0", 0,
              "ifconfig wlan0 0.0.0.0\n", "udhcpc.sh: deconfig");

    snprintf(env, sizeof env, "%srouter=192.168.1.1", lease);
    dhcp_case(sh, "bound: address, old default route out, new one in",
              "bound", env, 1,
              "ifconfig wlan0 192.168.1.136 netmask 255.255.255.0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route add default gw 192.168.1.1 dev wlan0\n", NULL);
    dhcp_case(sh, "renew does the same", "renew", env, 0,
              "ifconfig wlan0 192.168.1.136 netmask 255.255.255.0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route add default gw 192.168.1.1 dev wlan0\n", NULL);
    dhcp_case(sh, "the route delete loop is bounded", "bound", env, 100,
              "ifconfig wlan0 192.168.1.136 netmask 255.255.255.0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route add default gw 192.168.1.1 dev wlan0\n", NULL);

    snprintf(env, sizeof env, "%srouter='10.0.0.1 10.0.0.2'", lease);
    dhcp_case(sh, "two routers: the first that takes", "bound", env, 0,
              "ifconfig wlan0 192.168.1.136 netmask 255.255.255.0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route add default gw 10.0.0.1 dev wlan0\n", NULL);
    snprintf(env, sizeof env,
             "%srouter='10.0.0.1 10.0.0.2' FAIL_ADD=10.0.0.1", lease);
    dhcp_case(sh, "two routers, the first refused: the second", "bound", env,
              0,
              "ifconfig wlan0 192.168.1.136 netmask 255.255.255.0\n"
              "route del default gw 0.0.0.0 dev wlan0\n"
              "route add default gw 10.0.0.1 dev wlan0\n"
              "route add default gw 10.0.0.2 dev wlan0\n", NULL);

    dhcp_case(sh, "no router: the address only, routes untouched", "bound",
              lease, 1,
              "ifconfig wlan0 192.168.1.136 netmask 255.255.255.0\n", NULL);
    dhcp_case(sh, "leasefail is said and touches nothing", "leasefail",
              "interface=wlan0", 0, "", "udhcpc.sh: leasefail");
    dhcp_case(sh, "nak is said with the server's message", "nak",
              "interface=wlan0 message='address not available'", 0, "",
              "udhcpc.sh: nak: address not available");
    dhcp_case(sh, "an event nobody handles touches nothing", "foo",
              "interface=wlan0", 0, "", NULL);
}

static void dhcp(void)
{
    put(UBB,
        "#!/bin/sh\n"
        "echo \"$*\" >> " T "calls\n"
        "case \"$1 $2\" in\n"
        "'route del')\n"
        "  read n < " T "dels\n"
        "  [ \"$n\" -gt 0 ] || exit 1\n"
        "  echo $((n - 1)) > " T "dels\n"
        "  ;;\n"
        "'route add')\n"
        "  case \" $FAIL_ADD \" in *\" $5 \"*) exit 1 ;; esac\n"
        "  ;;\n"
        "esac\n"
        "exit 0\n");
    chmod(UBB, 0755);

    char text[1024];
    int k = udhcpc_script_text(text, sizeof text, UBB);
    check(k > 0, "the udhcpc script fits its buffer (%d bytes)", k);
    put(T "script", text);

    char longbb[80];
    memset(longbb, 'b', 64);
    longbb[0] = '/';
    longbb[64] = 0;
    check(udhcpc_script_text(text, sizeof text, longbb) > 0,
          "... with a 64-character busybox path");
    check(udhcpc_script_text(text, 100, UBB) < 0,
          "a buffer too small is refused, not truncated");

    check(system("sh -n " T "script") == 0, "sh   parses the script");
    dhcp_script("sh");
    if (system("command -v mksh >/dev/null 2>&1") == 0) {
        check(system("mksh -n " T "script") == 0, "mksh parses the script");
        dhcp_script("mksh");
    } else {
        printf("skip  mksh is not installed (the device's /system/bin/sh)\n");
    }

    const char *files[] = { UBB, T "script", T "calls", T "dels", T "said" };
    for (unsigned i = 0; i < sizeof files / sizeof files[0]; i++)
        unlink(files[i]);
}

/* ── Which udhcpc may be given -a ───────────────────────────────────────── */

#define SBIN T "sbin"

static void ours_case(const char *label, int want)
{
    int got = udhcpc_is_ours(SBIN "/udhcpc", SBIN "/busybox");
    check(got == want, "%-50s -> %s", label, got ? "-a" : "no -a");
}

static void ours(void)
{
    mkdir(SBIN, 0755);
    put(SBIN "/busybox", "ours\n");
    put(SBIN "/other", "theirs\n");

    unlink(SBIN "/udhcpc");
    symlink("busybox", SBIN "/udhcpc");
    ours_case("udhcpc -> busybox, as the packer writes it", 1);
    unlink(SBIN "/udhcpc");
    symlink(SBIN "/busybox", SBIN "/udhcpc");
    ours_case("udhcpc -> /sbin/busybox, as the applet stage does", 1);
    unlink(SBIN "/udhcpc");
    symlink("link", SBIN "/udhcpc");
    symlink("busybox", SBIN "/link");
    ours_case("through a second link", 1);
    unlink(SBIN "/link");
    unlink(SBIN "/udhcpc");
    symlink("other", SBIN "/udhcpc");
    ours_case("udhcpc -> another busybox", 0);
    unlink(SBIN "/udhcpc");
    put(SBIN "/udhcpc", "ours\n");
    ours_case("a copy, not a link: nothing says it is ours", 0);
    unlink(SBIN "/udhcpc");
    symlink("gone", SBIN "/udhcpc");
    ours_case("a dangling link", 0);
    unlink(SBIN "/udhcpc");
    ours_case("no udhcpc", 0);
    symlink("busybox", SBIN "/udhcpc");
    unlink(SBIN "/busybox");
    ours_case("no busybox of ours", 0);

    unlink(SBIN "/udhcpc");
    unlink(SBIN "/other");
    rmdir(SBIN);
}

/* ── The evidence copy ──────────────────────────────────────────────────── */

static void limiter(void)
{
    struct netlast n;
    char why[48];
    netlast_init(&n);

    check(!netlast_due(&n, 0, why, sizeof why), "nothing happened, nothing "
          "written");
    netlast_event(&n, "carrier-lost");
    check(netlast_due(&n, 100, why, sizeof why)
          && strcmp(why, "carrier-lost") == 0,
          "the first event of a boot is written at once");
    check(!netlast_due(&n, 100, why, sizeof why), "... once");

    netlast_event(&n, "watchdog-reassociate");
    netlast_event(&n, "watchdog-reconnect");
    int early = netlast_due(&n, 100 + NETLAST_GAP_S - 1, why, sizeof why);
    check(!early, "the next waits out the gap");
    check(netlast_due(&n, 100 + NETLAST_GAP_S, why, sizeof why)
          && strcmp(why, "watchdog-reconnect") == 0,
          "... and is then written, as the latest event");

    netlast_event(&n, "watchdog-restart");
    check(!netlast_due(&n, 100 + 10 * NETLAST_GAP_S, why, sizeof why),
          "two per incident: a third is never written");

    netlast_settled(&n);
    netlast_event(&n, "carrier-lost");
    check(netlast_due(&n, 100 + 10 * NETLAST_GAP_S, why, sizeof why),
          "once the gateway answers, the next failure is a new incident");

    netlast_settled(&n);
    long last = 100 + 10 * NETLAST_GAP_S;
    netlast_event(&n, "address-lost");
    check(!netlast_due(&n, last + 60, why, sizeof why)
          && netlast_due(&n, last + NETLAST_GAP_S, why, sizeof why),
          "a new incident still waits out the gap");

    netlast_init(&n);
    int writes = 0;
    for (long t = 0; t < 24 * 3600; t += 5) {
        if (t % 240 == 0)
            netlast_settled(&n);
        else if (t % 60 == 0)
            netlast_event(&n, "carrier-lost");
        if (netlast_due(&n, t, why, sizeof why))
            writes++;
    }
    check(writes <= 24 * 3600 / NETLAST_GAP_S + 1,
          "a link failing all day: %d copies, %d KB of flash", writes,
          writes * NETLAST_TAIL / 1024);
}

/* `lines` numbered lines of exactly `width` bytes each. */
static void numbered(const char *path, const char *tag, int lines, int width)
{
    FILE *f = fopen(path, "w");
    for (int i = 0; i < lines; i++) {
        char l[256];
        int k = snprintf(l, sizeof l, "%s %06d ", tag, i);
        while (k < width - 1)
            l[k++] = 'x';
        l[k++] = '\n';
        fwrite(l, 1, (size_t)k, f);
    }
    fclose(f);
}

static void reset_logs(void)
{
    unlink(NETLOG);
    unlink(NETLOG ".1");
    unlink(NETLAST);
    unlink(NETLAST ".1");
    unlink(NETLAST ".new");
}

static void evidence(void)
{
    size_t len;
    char *b;

    reset_logs();
    numbered(NETLOG, "line", 2000, 40);           /* 80KB, past the tail */
    check(netlast_write("# s\n") == 0, "a copy is written");
    b = slurp(NETLAST, &len);
    check(b && strncmp(b, "# s\nline ", 9) == 0,
          "the summary leads, then the log from the start of a line");
    check(b && len <= 4 + NETLAST_TAIL && len > 4 + NETLAST_TAIL - 40,
          "about %d KB of the log (%zu bytes)", NETLAST_TAIL / 1024, len);
    check(b && strstr(b, "line 001999 ") && b[len - 1] == '\n'
          && strstr(b, "line 001999 ") + 40 == b + len,
          "it ends at the last line logged");
    free(b);

    char *first = slurp(NETLAST, NULL);
    netlast_write("# t\n");
    char *prev = slurp(NETLAST ".1", NULL);
    b = slurp(NETLAST, NULL);
    check(first && prev && strcmp(first, prev) == 0 && b
          && strncmp(b, "# t\n", 4) == 0,
          "the copy before is kept as .1");
    free(first);
    free(prev);
    free(b);

    reset_logs();
    numbered(NETLOG ".1", "old", 2000, 40);
    numbered(NETLOG, "new", 10, 40);
    netlast_write("# s\n");
    b = slurp(NETLAST, &len);
    {
        char *o = b ? strstr(b, "old 001999 ") : NULL;
        char *nw = b ? strstr(b, "new 000000 ") : NULL;
        check(b && strncmp(b, "# s\nold ", 8) == 0 && o && nw
              && o + 40 == nw && strstr(b, "new 000009 ") + 40 == b + len
              && len <= 4 + NETLOG_CAP,
              "a log just rotated is topped up from the rotated copy, "
              "in order");
    }
    free(b);

    reset_logs();
    put(NETLOG ".1", "a\nb\n");
    put(NETLOG, "c\n");
    netlast_write("# s\n");
    b = slurp(NETLAST, NULL);
    check(b && strcmp(b, "# s\na\nb\nc\n") == 0,
          "both logs short: all of both, no line dropped");
    free(b);

    reset_logs();
    numbered(NETLOG, "full", NETLAST_TAIL / 32, 32);  /* exactly the tail */
    netlast_write("# s\n");
    b = slurp(NETLAST, &len);
    check(b && len == 4 + NETLAST_TAIL && strncmp(b, "# s\nfull 000000 ", 16)
          == 0, "a log exactly the size of the tail is copied whole");
    free(b);

    reset_logs();
    numbered(NETLOG, "full", NETLAST_TAIL / 32 + 1, 32);
    netlast_write("# s\n");
    b = slurp(NETLAST, &len);
    check(b && len == 4 + NETLAST_TAIL
          && strncmp(b, "# s\nfull 000001 ", 16) == 0,
          "one line more, cut on a line boundary: no line lost");
    free(b);

    reset_logs();
    {
        FILE *f = fopen(NETLOG, "w");
        fputs("long first line, cut part way through by the tail......\n", f);
        fclose(f);
        numbered(NETLOG ".tmp", "full", NETLAST_TAIL / 32 - 1, 32);
        char *rest = slurp(NETLOG ".tmp", NULL);
        f = fopen(NETLOG, "a");
        fputs(rest ? rest : "", f);
        fclose(f);
        free(rest);
        unlink(NETLOG ".tmp");
    }
    netlast_write("# s\n");
    b = slurp(NETLAST, &len);
    check(b && len == 4 + NETLAST_TAIL - 32
          && strncmp(b, "# s\nfull 000000 ", 16) == 0,
          "cut part way through a line: the fragment is dropped");
    free(b);

    reset_logs();
    netlast_write("# s\n");
    b = slurp(NETLAST, NULL);
    check(b && strcmp(b, "# s\n") == 0, "no log at all: the summary alone");
    free(b);

    /* The summary, as the controller will read it. */
    reset_logs();
    put(NETLOG, "wlan0: CTRL-EVENT-DISCONNECTED reason=4\n");
    struct linkwd w;
    linkwd_init(&w);
    w.off_since = 1000;
    w.steps = 2;
    netlast_save("watchdog-restart", 1234, "192.168.1.1", &w);
    b = slurp(NETLAST, NULL);
    {
        static const char lead[] =
            "# emos-net-last why=watchdog-restart uptime=1234 wall=";
        char *nl = b ? strchr(b, '\n') : NULL;
        char *wall = b ? strstr(b, " wall=") : NULL;
        int wall_ok = 0;
        if (wall) {
            wall += 6;
            wall_ok = strncmp(wall, "unset ", 6) == 0
                   || (strlen(wall) > 21 && wall[4] == '-' && wall[10] == 'T'
                       && wall[19] == 'Z' && wall[20] == ' ');
        }
        check(b && strncmp(b, lead, sizeof lead - 1) == 0 && wall_ok
              && nl && wall < nl,
              "the summary leads with why, uptime and wall time");
        check(b && strstr(b, " gateway=192.168.1.1 off_network=234 "
                             "watchdog_steps=2 rx_packets=")
              && nl && strstr(b, " tx_packets=") < nl
              && strcmp(nl + 1, "wlan0: CTRL-EVENT-DISCONNECTED reason=4\n")
                 == 0,
              "... then the link's state, then the log");
    }
    free(b);
    netlast_save("carrier-lost", 50, "", &w);
    b = slurp(NETLAST, NULL);
    check(b && strstr(b, " gateway=none "), "no gateway known: none");
    free(b);
    reset_logs();
}

/* ── Stopping the supplicant ─────────────────────────────────────────────── */

static pid_t start(const char *script)
{
    put(T "child", script);
    chmod(T "child", 0755);
    char *argv[] = { T "child", NULL };
    return spawn(argv);
}

/* Both children block opening a FIFO nobody writes, which costs no CPU. */
static void stopping(void)
{
    unlink(T "fifo");
    mkfifo(T "fifo", 0600);
    pid_t p = start("#!/bin/sh\nread x < " T "fifo\n");
    usleep(200000);
    long t0 = mono_ms();
    stop_child(&p);
    check(p == -1 && mono_ms() - t0 < 3000,
          "a child that ends on SIGTERM is reaped at once");

    p = start("#!/bin/sh\ntrap '' TERM\nread x < " T "fifo\n");
    usleep(200000);
    t0 = mono_ms();
    stop_child(&p);
    long took = mono_ms() - t0;
    check(p == -1 && took >= 4900 && took < 9000,
          "one that ignores it is killed after 5s (%ld ms)", took);
    unlink(T "fifo");
    unlink(T "child");
}

/* What the supervisor runs from its loop (arping, wpa_cli) must not be able
 * to stop the loop. */
static void bounded(void)
{
    char *argv[] = { T "child", NULL };

    put(T "child", "#!/bin/sh\nexit 3\n");
    chmod(T "child", 0755);
    int st = run_bounded(argv, 5);
    check(st >= 0 && WIFEXITED(st) && WEXITSTATUS(st) == 3,
          "a command that finishes gives its exit status");

    /* Blocked opening a FIFO nobody writes, as in stopping(): spawn()'s PATH
     * is the device's, so there is no `sleep` to call here. */
    unlink(T "pid");
    unlink(T "fifo");
    mkfifo(T "fifo", 0600);
    put(T "child", "#!/bin/sh\necho $$ > " T "pid\nread x < " T "fifo\n");
    chmod(T "child", 0755);
    long t0 = mono_ms();
    st = run_bounded(argv, 1);
    long took = mono_ms() - t0;
    char *pid = slurp(T "pid", NULL);
    int gone = pid && atoi(pid) > 0 && kill(atoi(pid), 0) != 0
            && errno == ESRCH;
    check(st == -1 && took >= 900 && took < 4000 && gone,
          "one that hangs is stopped at its limit and reaped (%ld ms)", took);
    free(pid);
    unlink(T "pid");
    unlink(T "fifo");
    unlink(T "child");
}

int main(void)
{
    watchdog();
    gateway();
    probe();
    dhcp();
    ours();
    limiter();
    evidence();
    stopping();
    bounded();

    unlink(NETLOG);
    unlink(NETLOG ".1");
    if (failures) {
        printf("\n%d check(s) failed\n", failures);
        return 1;
    }
    printf("\nall ok\n");
    return 0;
}
