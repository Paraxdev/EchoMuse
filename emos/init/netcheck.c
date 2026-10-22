/* Prove init.c handles a DHCP lease the way an Echo on a home network needs.
 *
 * Both parts are silent when wrong, which is the criterion for a check:
 *
 *   - the udhcpc script is the whole of what a lease does to wlan0: deconfig
 *     ignored keeps a lapsed address (#871), a route left behind keeps an old
 *     router in front;
 *   - -a handed to a udhcpc that does not know it ends DHCP before it starts.
 *
 * init.c is included whole, as the other checks do, so this drives the real
 * functions. The udhcpc script is run for real, by sh (and mksh, the device's
 * shell, where installed), against a stub busybox.
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

int main(void)
{
    dhcp();
    ours();

    if (failures) {
        printf("\n%d check(s) failed\n", failures);
        return 1;
    }
    printf("\nall ok\n");
    return 0;
}
