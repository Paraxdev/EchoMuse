package wifi

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/platform"
)

// fakeCli stands in for wpa_cli. reply gets the command (the last argument)
// and returns its output.
type fakeCli struct {
	mu    sync.Mutex
	calls []string
	reply func(cmd string) (string, error)
}

func (f *fakeCli) seen() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

func useFakeCli(t *testing.T, reply func(cmd string) (string, error)) *fakeCli {
	t.Helper()
	f := &fakeCli{reply: reply}
	oldExec, oldSleep := wpaCliExec, sleep
	t.Cleanup(func() { wpaCliExec, sleep = oldExec, oldSleep })
	wpaCliExec = func(args []string) (string, error) {
		cmd := args[len(args)-1]
		f.mu.Lock()
		f.calls = append(f.calls, cmd)
		f.mu.Unlock()
		return f.reply(cmd)
	}
	sleep = func(time.Duration) {}
	return f
}

func useNetLog(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "net.log")
	old := netLogP
	t.Cleanup(func() { netLogP = old })
	netLogP = path
	if body != "" {
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return path
}

func appendFile(t *testing.T, path, s string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err := f.WriteString(s); err != nil {
		t.Fatal(err)
	}
}

func resetLastScan(t *testing.T) {
	t.Helper()
	scanMu.Lock()
	old := lastScan
	lastScan = nil
	scanMu.Unlock()
	t.Cleanup(func() {
		scanMu.Lock()
		lastScan = old
		scanMu.Unlock()
	})
}

// ─── scan results ─────────────────────────────────────────────────────────────

func TestParseScanKeepsEveryFrequencyAndTheStrongestFlags(t *testing.T) {
	out := "bssid / frequency / signal level / flags / ssid\n" +
		"aa:01\t2437\t-55\t[WPA2-PSK-CCMP][ESS]\tHome\n" +
		"aa:02\t5180\t-48\t[WPA2-PSK+SAE-CCMP][ESS]\tHome\n" +
		"aa:03\t2412\t-70\t[WPA2-PSK-CCMP][ESS]\tHome\n" +
		"aa:04\t2437\t-60\t[WPA2-PSK-CCMP][ESS]\tHome\n" + // a second AP, same channel
		"bb:01\t?\t-65\t[ESS]\tCafé\n" // a frequency that does not parse
	nets := parseScan(out)
	if len(nets) != 2 {
		t.Fatalf("want 2 networks, got %+v", nets)
	}
	home := nets[0]
	if home.SSID != "Home" || home.Signal != -48 {
		t.Fatalf("strongest first, at the strongest signal: %+v", home)
	}
	if !reflect.DeepEqual(home.Freqs, []int{2412, 2437, 5180}) {
		t.Errorf("freqs %v, want every frequency once, ascending", home.Freqs)
	}
	if home.Flags != "[WPA2-PSK+SAE-CCMP][ESS]" {
		t.Errorf("flags %q, want the strongest BSS's", home.Flags)
	}
	if nets[1].SSID != "Café" || nets[1].Freqs != nil || nets[1].Flags != "[ESS]" {
		t.Errorf("a row with no usable frequency keeps its network and flags: %+v", nets[1])
	}
}

// The two fields are additive on the wire: absent when there is nothing to
// say, so an old reader sees exactly what it always did.
func TestNetworkJSONAddsFreqsAndFlagsOnlyWhenKnown(t *testing.T) {
	b, _ := json.Marshal(Network{SSID: "x", SSIDHex: "78", Signal: -50})
	if string(b) != `{"ssid":"x","ssid_hex":"78","signal":-50}` {
		t.Errorf("bare network encodes as %s", b)
	}
	b, _ = json.Marshal(Network{SSID: "x", SSIDHex: "78", Signal: -50, Freqs: []int{2412, 5180}, Flags: "[ESS]"})
	if string(b) != `{"ssid":"x","ssid_hex":"78","signal":-50,"freqs":[2412,5180],"flags":"[ESS]"}` {
		t.Errorf("full network encodes as %s", b)
	}
}

// securityOf is security_of() in emos/device/em-wifi. Driven by that script's
// own check vectors, so a case added there is checked here too.
func TestSecurityOfAgreesWithEmWifiOnItsVectors(t *testing.T) {
	src, err := os.ReadFile("../../../emos/device/em-wifi-check.sh")
	if err != nil {
		t.Fatal(err)
	}
	re := regexp.MustCompile(`(?m)^expect '([^']*)'\s+(\w+)\s*$`)
	vectors := re.FindAllStringSubmatch(string(src), -1)
	if len(vectors) < 20 {
		t.Fatalf("found %d vectors in em-wifi-check.sh; has its format changed?", len(vectors))
	}
	for _, v := range vectors {
		if got := securityOf(v[1]); got != v[2] {
			t.Errorf("securityOf(%q) = %s, em-wifi says %s", v[1], got, v[2])
		}
	}
}

func TestDescribeFreqsGroupsByBand(t *testing.T) {
	cases := map[string][]int{
		"2.4 GHz (2412, 2437 MHz) + 5 GHz (5180, 5500 MHz)": {2412, 2437, 5180, 5500},
		"5 GHz (5785 MHz)":              {5785},
		"2.4 GHz (2484 MHz) + 5955 MHz": {2484, 5955},
		"frequency not reported":        nil,
	}
	for want, in := range cases {
		if got := describeFreqs(in); got != want {
			t.Errorf("describeFreqs(%v) = %q, want %q", in, got, want)
		}
	}
}

// ─── scan_ssid ────────────────────────────────────────────────────────────────

func TestComposeConfProbesByNameOnlyWhenAsked(t *testing.T) {
	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
	if conf := composeConf([]byte("net"), "12345678", false); strings.Contains(conf, "scan_ssid") {
		t.Errorf("a network the last scan saw needs no probing:\n%s", conf)
	}
	conf := composeConf([]byte("net"), "12345678", true)
	if !strings.Contains(conf, "network={\n\tssid=\"net\"\n\tscan_ssid=1\n") {
		t.Errorf("scan_ssid=1 missing from the network block:\n%s", conf)
	}
}

// The last scan is matched on the SSID's bytes, at the spec's edges: an SSID
// is 0-32 arbitrary octets.
func TestSeenInLastScanMatchesBytes(t *testing.T) {
	resetLastScan(t)
	if seenInLastScan([]byte("Home")) {
		t.Fatal("before any scan nothing has been seen")
	}
	long := strings.Repeat("x", 32)
	rememberScan(parseScan(
		"aa\t2412\t-40\t[ESS]\tHome\n" +
			"bb\t2412\t-40\t[ESS]\tCaf\\xc3\\xa9\n" +
			"cc\t2412\t-40\t[ESS]\t\\xff\\x01\n" +
			"dd\t2412\t-40\t[ESS]\tBob's \\\"Net\\\"\n" +
			"ee\t2412\t-40\t[ESS]\t" + long + "\n"))
	for _, s := range [][]byte{[]byte("Home"), []byte("Café"), {0xff, 0x01}, []byte(`Bob's "Net"`), []byte(long)} {
		if !seenInLastScan(s) {
			t.Errorf("%q was in the scan", s)
		}
	}
	for _, s := range [][]byte{[]byte("home"), []byte("Home "), []byte("Caf\\xc3\\xa9"), {0xff}, []byte(long + "x")} {
		if seenInLastScan(s) {
			t.Errorf("%q was not in the scan", s)
		}
	}
}

// ─── the emOS nudge ───────────────────────────────────────────────────────────

func useProc(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	old := procDir
	t.Cleanup(func() { procDir = old })
	procDir = dir
	return dir
}

func writeStat(t *testing.T, dir string, pid, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, pid), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, pid, "stat"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestProcStatFindsTheStateAfterTheLastParen(t *testing.T) {
	dir := useProc(t)
	writeStat(t, dir, "77", "77 (we) S) R 1 2 3\n")
	writeStat(t, dir, "78", "78 (wpa_supplicant) Z 1 2 3\n")
	writeStat(t, dir, "79", "79 (broken")
	if comm, st, ok := procStat(77); !ok || comm != "we) S" || st != 'R' {
		t.Errorf("procStat(77) = %q %c %v", comm, st, ok)
	}
	if comm, st, ok := procStat(78); !ok || comm != "wpa_supplicant" || st != 'Z' {
		t.Errorf("procStat(78) = %q %c %v", comm, st, ok)
	}
	if _, _, ok := procStat(79); ok {
		t.Error("a truncated stat parsed")
	}
	if _, _, ok := procStat(80); ok {
		t.Error("a missing pid parsed")
	}
}

// The old supplicant answers PONG until SIGTERM has taken effect, and a
// reassociate sent to it is lost with it. The nudge waits for it to go, and
// marks the network log only once it has: its stdio flushes on the way out.
func TestNudgeWaitsForTheOldSupplicantToExit(t *testing.T) {
	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
	logPath := useNetLog(t, "boot\n")
	dir := useProc(t)
	writeStat(t, dir, "4242", "4242 (wpa_supplicant) S 1 1 1\n")
	cli := useFakeCli(t, func(cmd string) (string, error) {
		if cmd == "ping" {
			return "PONG\n", nil
		}
		return "OK\n", nil
	})
	sleep = func(time.Duration) { time.Sleep(time.Millisecond) }
	oldWait := supplicantWait
	t.Cleanup(func() { supplicantWait = oldWait })
	supplicantWait = 5 * time.Second

	type nudged struct {
		mark logMark
		ok   bool
	}
	res := make(chan nudged, 1)
	go func() {
		m, ok := nudgeNewSupplicant([]int{4242})
		res <- nudged{m, ok}
	}()
	time.Sleep(50 * time.Millisecond)
	for _, c := range cli.seen() {
		if c == "reassociate" || c == "ping" {
			t.Fatalf("asked the supplicant (%s) while the retired one was still running", c)
		}
	}
	appendFile(t, logPath, "wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"Old\" auth_failures=1 duration=10 reason=CONN_FAILED\n")
	writeStat(t, dir, "4242", "4242 (wpa_supplicant) Z 1 1 1\n")
	var got nudged
	select {
	case got = <-res:
		if !got.ok {
			t.Fatal("nudge reported failure")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("nudge never sent")
	}
	calls := cli.seen()
	if len(calls) != 2 || calls[0] != "ping" || calls[1] != "reassociate" {
		t.Fatalf("calls %v, want [ping reassociate]", calls)
	}
	if s := netLogSince(got.mark); s != "" || !got.mark.ok {
		t.Fatalf("the mark (%+v) lets the retired supplicant's last words through: %q", got.mark, s)
	}
}

// A supplicant that will not exit leaves nothing to mark from, and is not
// asked anything.
func TestNudgeLeavesNoMarkWhenTheOldSupplicantStays(t *testing.T) {
	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
	useNetLog(t, "boot\n")
	dir := useProc(t)
	writeStat(t, dir, "4242", "4242 (wpa_supplicant) S 1 1 1\n")
	cli := useFakeCli(t, func(cmd string) (string, error) { return "PONG\n", nil })
	sleep = func(time.Duration) { time.Sleep(time.Millisecond) }
	oldWait := supplicantWait
	t.Cleanup(func() { supplicantWait = oldWait })
	supplicantWait = 30 * time.Millisecond

	m, ok := nudgeNewSupplicant([]int{4242})
	if ok || m.ok {
		t.Fatalf("nudged %v with mark %+v past a supplicant that never exited", ok, m)
	}
	if calls := cli.seen(); len(calls) != 0 {
		t.Fatalf("asked %v of the supplicant that was told to go", calls)
	}
}

func TestNudgeGivesUpQuietlyWhenNothingAnswers(t *testing.T) {
	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
	useProc(t)
	cli := useFakeCli(t, func(cmd string) (string, error) {
		return "Failed to connect to non-global ctrl_ifname: wlan0  error: No such file or directory\n", os.ErrNotExist
	})
	sleep = func(time.Duration) { time.Sleep(time.Millisecond) }
	oldWait := supplicantWait
	t.Cleanup(func() { supplicantWait = oldWait })
	supplicantWait = 30 * time.Millisecond

	if _, ok := nudgeNewSupplicant(nil); ok {
		t.Fatal("reported a nudge nobody received")
	}
	for _, c := range cli.seen() {
		if c != "ping" {
			t.Fatalf("sent %q to a supplicant that never answered", c)
		}
	}
}

// A pid now naming another program was reused; it is not the old supplicant.
func TestAReusedPidIsNotTheOldSupplicant(t *testing.T) {
	dir := useProc(t)
	writeStat(t, dir, "10", "10 (sh) S 1 1 1\n")
	writeStat(t, dir, "11", "11 (wpa_supplicant) X 1 1 1\n")
	if !allExited([]int{10, 11, 12}) {
		t.Fatal("reused, dead and missing pids all count as exited")
	}
	writeStat(t, dir, "12", "12 (wpa_supplicant) S 1 1 1\n")
	if allExited([]int{10, 11, 12}) {
		t.Fatal("a running supplicant is not exited")
	}
}

// ─── the supplicant's own reason ──────────────────────────────────────────────

// The line formats are hostap 2.10's (wpa_supplicant/events.c, notify.c and
// wpa_supplicant.c), as its stdout prints them with the interface prefix.
func TestSupplicantReasonFromHostapEvents(t *testing.T) {
	cases := []struct{ log, want string }{
		{"", ""},
		{"wlan0: Trying to associate with 11:22:33:44:55:66 (SSID='Home' freq=5180 MHz)\n", ""},
		{"wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"Home\" auth_failures=1 duration=10 reason=WRONG_KEY\n",
			"WRONG_KEY: the AP rejected the passphrase"},
		// The SSID is quoted text and may hold anything, including the key
		// itself; reason= is the line's last field.
		{"wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"x reason=WRONG_KEY y\" auth_failures=2 duration=20 reason=CONN_FAILED\r\n",
			"that it gave up on the network for now (CONN_FAILED)"},
		{"wlan0: CTRL-EVENT-ASSOC-REJECT bssid=11:22:33:44:55:66 status_code=17\n",
			"that the AP rejected the association (status 17)"},
		{"wlan0: CTRL-EVENT-NETWORK-NOT-FOUND \nwlan0: CTRL-EVENT-NETWORK-NOT-FOUND \n",
			"that it never found the network in its own scans"},
		// With -t the supplicant stamps each line first.
		{"1700000000.123456: wlan0: CTRL-EVENT-ASSOC-REJECT bssid=11:22:33:44:55:66 status_code=1\n",
			"that the AP rejected the association (status 1)"},
		// An SSID is anyone's text, and the supplicant quotes it on lines of
		// its own: an event name inside one is not an event.
		{"wlan0: Trying to associate with 11:22:33:44:55:66 (SSID='CTRL-EVENT-NETWORK-NOT-FOUND' freq=2412 MHz)\n" +
			"wlan0: SME: Trying to authenticate with 11:22:33:44:55:66 (SSID='x: CTRL-EVENT-ASSOC-REJECT status_code=9' freq=2412 MHz)\n",
			""},
		// The most specific wins, whatever the order.
		{"wlan0: CTRL-EVENT-NETWORK-NOT-FOUND \n" +
			"wlan0: CTRL-EVENT-ASSOC-REJECT bssid=11:22:33:44:55:66 status_code=1\n" +
			"wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"Home\" auth_failures=1 duration=10 reason=WRONG_KEY\n",
			"WRONG_KEY: the AP rejected the passphrase"},
	}
	for _, c := range cases {
		if got := supplicantReason(c.log); got != c.want {
			t.Errorf("supplicantReason(%q) = %q, want %q", c.log, got, c.want)
		}
	}
}

func TestNetLogSinceReadsOnlyThisAttempt(t *testing.T) {
	path := useNetLog(t, strings.Repeat("old line\n", 10))
	m := markNetLog()
	appendFile(t, path, "new line\n")
	if got := netLogSince(m); got != "new line\n" {
		t.Errorf("got %q", got)
	}

	// init rotated the log in between: the rest of the old one, then the new.
	path = useNetLog(t, strings.Repeat("old line\n", 10))
	m = markNetLog()
	appendFile(t, path, "tail of the old\n")
	if err := os.Rename(path, path+".1"); err != nil {
		t.Fatal(err)
	}
	appendFile(t, path, "head\n")
	if got := netLogSince(m); got != "tail of the old\nhead\n" {
		t.Errorf("across a rotation got %q", got)
	}

	// FireOS has no such log.
	useNetLog(t, "")
	if got := netLogSince(markNetLog()); got != "" {
		t.Errorf("no log, got %q", got)
	}
}

// The path is init's NETLOG, which is where the supplicant's stdout goes.
func TestNetLogIsWhereInitWritesIt(t *testing.T) {
	src, err := os.ReadFile("../../../emos/init/init.c")
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`#define NETLOG\s+"([^"]+)"`).FindSubmatch(src)
	if m == nil {
		t.Fatal("NETLOG not found in init.c")
	}
	if string(m[1]) != netLog {
		t.Fatalf("init.c logs to %s, the firmware reads %s", m[1], netLog)
	}
}

// ─── the whole diagnosis ──────────────────────────────────────────────────────

// diagnose is the failure path's diagnosis in one call: the scan, then the
// supplicant's reason since m.
func diagnose(ssid []byte, psk string, m logMark) string {
	return explainNoAssociation(ssid, psk, scanAir(), supplicantReason(netLogSince(m)))
}

func scanReply(results string) func(string) (string, error) {
	return func(cmd string) (string, error) {
		switch cmd {
		case "scan":
			return "FAIL-BUSY\n", nil
		case "scan_results":
			return "bssid / frequency / signal level / flags / ssid\n" + results, nil
		}
		return "OK\n", nil
	}
}

func TestDiagnosisOfANetworkNotOnTheAir(t *testing.T) {
	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
	resetLastScan(t)
	path := useNetLog(t, "boot\n")
	m := markNetLog()
	appendFile(t, path, "wlan0: CTRL-EVENT-NETWORK-NOT-FOUND \n")
	useFakeCli(t, scanReply(
		"aa\t2437\t-50\t[WPA2-PSK-CCMP][ESS]\tHome\n"+
			"bb\t2412\t-70\t[WPA2-PSK-CCMP][ESS]\tNeighbour\n"))

	got := diagnose([]byte("Home-5G"), "12345678", m)
	for _, want := range []string{
		`"Home-5G" was not seen in a scan just now (2 other networks were, none of them on 5 GHz)`,
		"on a channel this radio does not scan",
		"The supplicant reported that it never found the network in its own scans.",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("diagnosis lacks %q:\n%s", want, got)
		}
	}
}

// The count of what else was on the air reads as a sentence at every size,
// and a scan that saw nothing at all says so.
func TestNotSeenCountsTheOtherNetworks(t *testing.T) {
	five := Network{SSID: "a", SSIDHex: "61", Freqs: []int{5180}}
	two := Network{SSID: "b", SSIDHex: "62", Freqs: []int{2412}}
	cases := []struct {
		nets []Network
		want string
	}{
		{nil, `"x" was not seen in a scan just now, and neither was any other network.`},
		{[]Network{two}, "(1 other network was, not on 5 GHz)"},
		{[]Network{five}, "(1 other network was, on 5 GHz)"},
		{[]Network{two, two}, "(2 other networks were, none of them on 5 GHz)"},
		{[]Network{five, two, five}, "(3 other networks were, 2 of them on 5 GHz)"},
	}
	for _, c := range cases {
		got := strings.Join(describeAir([]byte("x"), "12345678", c.nets, false), " ")
		if !strings.Contains(got, c.want) {
			t.Errorf("%d networks: %s", len(c.nets), got)
		}
	}
}

func TestDiagnosisOfAWrongPassphraseOn5GHz(t *testing.T) {
	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
	resetLastScan(t)
	path := useNetLog(t, "")
	m := markNetLog()
	if m.ok {
		t.Fatal("an empty log path should not exist yet")
	}
	appendFile(t, path, "")
	m = markNetLog()
	appendFile(t, path, "wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"Home\" auth_failures=1 duration=10 reason=WRONG_KEY\n")
	useFakeCli(t, scanReply(
		"aa\t2437\t-55\t[WPA2-PSK-CCMP][ESS]\tHome\n"+
			"ab\t5180\t-61\t[WPA2-PSK+SAE-CCMP][ESS]\tHome\n"))

	got := diagnose([]byte("Home"), "12345678", m)
	want := `"Home" is on the air: 2.4 GHz (2437 MHz) + 5 GHz (5180 MHz), -55 dBm, [WPA2-PSK-CCMP][ESS]. ` +
		`The supplicant reported WRONG_KEY: the AP rejected the passphrase.`
	if got != want {
		t.Errorf("got  %s\nwant %s", got, want)
	}
	if !seenInLastScan([]byte("Home")) {
		t.Error("the diagnosis scan is the device's last scan")
	}
}

func TestDiagnosisNamesWhatThisEchoCannotJoin(t *testing.T) {
	onBase(t, platform.FireOS, "ctrl_interface="+androidDir+"\n", "")
	useNetLog(t, "")
	cases := []struct {
		flags, psk, want string
	}{
		{"[WPA2-SAE-CCMP][ESS]", "12345678", "WPA3 only (SAE)"},
		{"[WPA2-EAP-CCMP][ESS]", "12345678", "WPA Enterprise"},
		{"[WPA2-OWE-CCMP][OWE-TRANS][ESS]", "", "Enhanced Open (OWE)"},
		{"[WEP][ESS]", "12345678", "uses WEP"},
		{"[WPA2-PSK-SHA256-CCMP][ESS]", "12345678", "not one this Echo can join"},
		{"[ESS]", "12345678", "open network, but a passphrase was given"},
		{"[WPA2-PSK-CCMP][ESS]", "", "needs a passphrase and none was given"},
		{"[WPA2-PSK+SAE-CCMP][ESS]", "12345678", "the passphrase, or the AP refusing this client, is the likely cause"},
		// No passphrase was involved, so it cannot be the cause.
		{"[ESS]", "", "so the AP refusing this client is the likely cause"},
	}
	for _, c := range cases {
		useFakeCli(t, scanReply("aa\t2462\t-60\t"+c.flags+"\tNet\n"))
		got := diagnose([]byte("Net"), c.psk, markNetLog())
		if !strings.Contains(got, c.want) {
			t.Errorf("%s with psk %q: %s", c.flags, c.psk, got)
		}
		if c.psk == "" && strings.Contains(got, "passphrase,") {
			t.Errorf("%s with no passphrase blames one: %s", c.flags, got)
		}
		if strings.Contains(got, "supplicant reported") {
			t.Errorf("no log, yet a supplicant reason: %s", got)
		}
	}
}

func TestDiagnosisWhenTheScanFails(t *testing.T) {
	onBase(t, platform.FireOS, "ctrl_interface="+androidDir+"\n", "")
	useNetLog(t, "")
	useFakeCli(t, func(cmd string) (string, error) { return "", os.ErrPermission })
	got := diagnose([]byte("Net"), "12345678", markNetLog())
	if !strings.HasPrefix(got, "Could not scan to find out why") {
		t.Errorf("got %q", got)
	}
}

// ─── a whole failed change, on emOS ───────────────────────────────────────────

// A supplicant's stdout is the network log, a file, so stdio holds its lines
// until it exits. A failed change therefore meets two flushes: the supplicant
// it replaced, writing about the old network as it goes, and the one that
// tried the new network, which writes only when the revert retires it. The
// report must carry the second and never the first.
func TestAFailedChangeReportsTheSupplicantThatTriedIt(t *testing.T) {
	const oldConf = "ctrl_interface=" + emosDir + "\nnetwork={\n\tssid=\"Old\"\n\tpsk=\"oldpassword\"\n}\n"
	onBase(t, platform.EmOS, "", oldConf)
	resetLastScan(t)
	logPath := useNetLog(t, "boot\n")
	proc := useProc(t)
	tmp := t.TempDir()
	oldSupp, oldChown, oldMarker, oldAssoc, oldSignal := emosSuppP, chownFile, markerPath, associateTimeout, signalProc
	t.Cleanup(func() {
		emosSuppP, chownFile, markerPath, associateTimeout, signalProc = oldSupp, oldChown, oldMarker, oldAssoc, oldSignal
		mu.Lock()
		pending = nil
		mu.Unlock()
	})
	emosSuppP = filepath.Join(tmp, "no-supplicant-here")
	chownFile = func(string, int, int) error { return nil }
	markerPath = filepath.Join(tmp, "pending")
	associateTimeout = 10 * time.Millisecond

	var evMu sync.Mutex
	var events []string
	event := func(e string) {
		evMu.Lock()
		events = append(events, e)
		evMu.Unlock()
	}
	// What each supplicant holds unwritten when it is told to go. 100 ran the
	// old network and lost it for a moment once; 101 tried the new one.
	lastWords := map[int]string{
		100: "wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"Old\" auth_failures=1 duration=10 reason=CONN_FAILED\n",
		101: "wlan0: Trying to associate with 11:22:33:44:55:66 (SSID='Home' freq=2437 MHz)\n" +
			"wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"Home\" auth_failures=1 duration=10 reason=WRONG_KEY\n",
	}
	start := func(pid int) {
		d := filepath.Join(proc, strconv.Itoa(pid))
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, "comm"), []byte("wpa_supplicant\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		writeStat(t, proc, strconv.Itoa(pid), fmt.Sprintf("%d (wpa_supplicant) S 1 1 1\n", pid))
	}
	start(100)
	var confTried string
	signalProc = func(pid int, sig syscall.Signal) error {
		if sig != syscall.SIGTERM {
			t.Errorf("signal %v to %d, want SIGTERM", sig, pid)
		}
		event(fmt.Sprintf("SIGTERM %d", pid))
		if pid == 100 {
			b, _ := os.ReadFile(emosConfP)
			confTried = string(b)
		}
		// It exits, flushing; init reaps it and starts the next.
		appendFile(t, logPath, lastWords[pid])
		if err := os.RemoveAll(filepath.Join(proc, strconv.Itoa(pid))); err != nil {
			t.Fatal(err)
		}
		start(pid + 1)
		return nil
	}
	useFakeCli(t, func(cmd string) (string, error) {
		event(cmd)
		switch cmd {
		case "ping":
			return "PONG\n", nil
		case "status":
			return "wpa_state=COMPLETED\nssid=Old\n", nil
		case "scan_results":
			return "bssid / frequency / signal level / flags / ssid\n" +
				"aa:01\t2437\t-55\t[WPA2-PSK-CCMP][ESS]\tHome\n", nil
		}
		return "OK\n", nil
	})

	Change([]byte("Home"), "notthepassword", func() bool { return false })

	r := PendingResult()
	if r == nil || r.OK {
		t.Fatalf("result %+v, want a failure", r)
	}
	want := `did not associate to "Home" within 10ms. ` +
		`"Home" is on the air: 2.4 GHz (2437 MHz), -55 dBm, [WPA2-PSK-CCMP][ESS]. ` +
		`The supplicant reported WRONG_KEY: the AP rejected the passphrase.`
	if r.Error != want {
		t.Errorf("reported\n  %s\nwant\n  %s", r.Error, want)
	}

	evMu.Lock()
	seq := strings.Join(events, ",")
	evMu.Unlock()
	scanned, retired := strings.Index(seq, "scan_results"), strings.Index(seq, "SIGTERM 101")
	if scanned < 0 || retired < 0 || scanned > retired {
		t.Errorf("the air must be scanned while the supplicant that failed still runs: %s", seq)
	}
	if !strings.Contains(confTried, "\tssid=\"Home\"\n\tscan_ssid=1\n") {
		t.Errorf("a network no scan had seen was not probed for by name:\n%s", confTried)
	}
	if b, _ := os.ReadFile(emosConfP); string(b) != oldConf {
		t.Errorf("the old conf was not put back:\n%s", b)
	}
	if _, err := os.Stat(markerPath); !os.IsNotExist(err) {
		t.Errorf("the pending marker outlived a clean revert: %v", err)
	}
}
