package wifi

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"sync"
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
		"5 GHz (5785 MHz)":                                   {5785},
		"2.4 GHz (2484 MHz) + 5955 MHz":                      {2484, 5955},
		"frequency not reported":                             nil,
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
// reassociate sent to it is lost with it. The nudge waits for it to go.
func TestNudgeWaitsForTheOldSupplicantToExit(t *testing.T) {
	onBase(t, platform.EmOS, "", "ctrl_interface="+emosDir+"\n")
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

	res := make(chan bool, 1)
	go func() { res <- nudgeNewSupplicant([]int{4242}) }()
	time.Sleep(50 * time.Millisecond)
	for _, c := range cli.seen() {
		if c == "reassociate" || c == "ping" {
			t.Fatalf("asked the supplicant (%s) while the retired one was still running", c)
		}
	}
	writeStat(t, dir, "4242", "4242 (wpa_supplicant) Z 1 1 1\n")
	select {
	case ok := <-res:
		if !ok {
			t.Fatal("nudge reported failure")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("nudge never sent")
	}
	calls := cli.seen()
	if len(calls) != 2 || calls[0] != "ping" || calls[1] != "reassociate" {
		t.Fatalf("calls %v, want [ping reassociate]", calls)
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

	if nudgeNewSupplicant(nil) {
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

	got := diagnoseNoAssociation([]byte("Home-5G"), "12345678", m)
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

	got := diagnoseNoAssociation([]byte("Home"), "12345678", m)
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
		{"[ESS]", "", "the passphrase, or the AP refusing this client, is the likely cause"},
	}
	for _, c := range cases {
		useFakeCli(t, scanReply("aa\t2462\t-60\t"+c.flags+"\tNet\n"))
		got := diagnoseNoAssociation([]byte("Net"), c.psk, markNetLog())
		if !strings.Contains(got, c.want) {
			t.Errorf("%s with psk %q: %s", c.flags, c.psk, got)
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
	got := diagnoseNoAssociation([]byte("Net"), "12345678", markNetLog())
	if !strings.HasPrefix(got, "Could not scan to find out why") {
		t.Errorf("got %q", got)
	}
}
