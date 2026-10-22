package wifi

import (
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

// A failed association used to report "wrong passphrase or AP out of range?"
// whatever happened, so a network this radio cannot see, one it cannot join
// and a wrong passphrase all read alike: three attempts on one Echo in October
// 2026, 5-8 minutes offline each, ended with that line and nothing else. The
// provisioning wizard has asked the air since #82 (reportWhyNoAssociation in
// dashboard.jsx); this is the same diagnosis for a fielded device, plus what
// the supplicant itself said where emOS keeps its output.

// Security classes, as em-wifi's security_of() names them.
const (
	secPSK   = "psk"
	secOpen  = "open"
	secWPA3  = "wpa3"
	secEAP   = "eap"
	secOWE   = "owe"
	secWEP   = "wep"
	secOther = "other"
)

// securityOf classifies a scan result's flags by what this Echo can join. It
// is security_of() in emos/device/em-wifi, rule for rule, and is tested against
// that script's own vectors (em-wifi-check.sh), which follow the grammar of
// wpa_supplicant_ie_txt() in hostap 2.10: [<proto>-<AKM>+<AKM>-<cipher>...].
//
// PSK is decided first, because a WPA2/WPA3 transition network offers PSK and
// SAE and this radio joins it through PSK (it has no SAE). PSK-SHA256 and
// FT/PSK are other AKMs, which the driver does not list and the conf does not
// write, so they are renamed before the token test can see a PSK inside them.
func securityOf(flags string) string {
	f := strings.ReplaceAll(flags, "PSK-SHA256", "PSKSHA256")
	f = strings.ReplaceAll(f, "FT/PSK", "FTPSK")
	switch {
	case containsAny(f, "-PSK-", "-PSK+", "+PSK-", "+PSK+"):
		return secPSK
	case strings.Contains(f, "SAE"):
		return secWPA3
	case strings.Contains(f, "EAP"):
		return secEAP
	case containsAny(f, "-OWE-", "-OWE+", "+OWE-", "+OWE+"):
		return secOWE
	case strings.Contains(f, "WEP"):
		return secWEP
	case containsAny(f, "[WPA-", "[WPA2-", "[RSN-", "[OSEN-"):
		return secOther
	}
	return secOpen
}

func containsAny(s string, subs ...string) bool {
	for _, sub := range subs {
		if strings.Contains(s, sub) {
			return true
		}
	}
	return false
}

// band names a frequency's band. 6 GHz (5925 MHz up) is shown as a number:
// this radio predates it.
func band(mhz int) string {
	switch {
	case mhz >= 2400 && mhz < 2500:
		return "2.4 GHz"
	case mhz >= 4900 && mhz < 5925:
		return "5 GHz"
	}
	return ""
}

// describeFreqs groups frequencies by band: "2.4 GHz (2412, 2437 MHz) + 5 GHz
// (5180 MHz)".
func describeFreqs(freqs []int) string {
	if len(freqs) == 0 {
		return "frequency not reported"
	}
	var order []string
	groups := map[string][]string{}
	for _, f := range freqs {
		b := band(f)
		if _, ok := groups[b]; !ok {
			order = append(order, b)
		}
		groups[b] = append(groups[b], strconv.Itoa(f))
	}
	parts := make([]string, 0, len(order))
	for _, b := range order {
		mhz := strings.Join(groups[b], ", ") + " MHz"
		if b == "" {
			parts = append(parts, mhz)
		} else {
			parts = append(parts, b+" ("+mhz+")")
		}
	}
	return strings.Join(parts, " + ")
}

func on5GHz(n Network) bool {
	for _, f := range n.Freqs {
		if band(f) == "5 GHz" {
			return true
		}
	}
	return false
}

// diagnoseNoAssociation says why a change did not associate, from a fresh
// scan and the supplicant's own log since mark. Always returns a sentence.
func diagnoseNoAssociation(ssid []byte, psk string, mark logMark) string {
	var out []string
	// The supplicant may already be scanning and refuse a new one (FAIL-BUSY);
	// its results are still the freshest there are.
	_, _ = wpaCli("scan")
	nets, err := collectScan()
	reason := supplicantReason(netLogSince(mark))
	if err != nil {
		out = append(out, fmt.Sprintf("Could not scan to find out why (%v).", err))
	} else {
		out = append(out, describeAir(ssid, psk, nets, reason != "")...)
	}
	if reason != "" {
		out = append(out, "The supplicant reported "+reason+".")
	}
	return strings.Join(out, " ")
}

// describeAir is the scan half of the diagnosis. hasReason says whether the
// supplicant's own reason will follow, which then replaces the guess.
func describeAir(ssid []byte, psk string, nets []Network, hasReason bool) []string {
	name := strconv.Quote(SSIDText(ssid))
	var match *Network
	fives := 0
	for i := range nets {
		if on5GHz(nets[i]) {
			fives++
		}
		if b, err := hex.DecodeString(nets[i].SSIDHex); err == nil && string(b) == string(ssid) {
			match = &nets[i]
		}
	}
	if match == nil {
		five := "none of them on 5 GHz"
		if fives > 0 {
			five = fmt.Sprintf("%d of them on 5 GHz", fives)
		}
		return []string{fmt.Sprintf("%s was not seen in a scan just now (%d other networks were, %s): "+
			"out of range, hidden under a different spelling, or on a channel this radio does not scan.",
			name, len(nets), five)}
	}

	flags := match.Flags
	if flags == "" {
		flags = "no security flags"
	}
	out := []string{fmt.Sprintf("%s is on the air: %s, %d dBm, %s.",
		name, describeFreqs(match.Freqs), match.Signal, flags)}
	switch securityOf(match.Flags) {
	case secWPA3:
		return append(out, "It is WPA3 only (SAE), which this Echo cannot join: "+
			"turn on WPA2/WPA3 mixed mode on the router.")
	case secEAP:
		return append(out, "It is WPA Enterprise (802.1X), which a network change cannot set up.")
	case secOWE:
		return append(out, "It is Enhanced Open (OWE) only, which this Echo cannot join.")
	case secWEP:
		return append(out, "It uses WEP, which a network change cannot set up.")
	case secOther:
		return append(out, "That security is not one this Echo can join.")
	case secOpen:
		if psk != "" {
			return append(out, "It is an open network, but a passphrase was given, "+
				"so the Echo looked for a secured one.")
		}
	case secPSK:
		if psk == "" {
			return append(out, "It needs a passphrase and none was given.")
		}
	}
	if !hasReason {
		out = append(out, "This Echo can join that kind of network, so the passphrase, "+
			"or the AP refusing this client, is the likely cause.")
	}
	return out
}

// supplicantReason picks out what the supplicant said about the attempt:
// hostap 2.10 prints these at its default level, and emOS's init sends its
// stdout to the network log. Nothing else on the line is used, the SSID least
// of all.
func supplicantReason(log string) string {
	var tempDisabled, rejected string
	notFound := 0
	for _, line := range strings.Split(log, "\n") {
		name, rest, ok := ctrlEvent(line)
		if !ok {
			continue
		}
		switch name {
		case "CTRL-EVENT-SSID-TEMP-DISABLED":
			// reason= is the last field; LastIndex because the quoted SSID
			// before it may contain the same text.
			if i := strings.LastIndex(rest, " reason="); i >= 0 {
				tempDisabled = firstField(rest[i+len(" reason="):])
			}
		case "CTRL-EVENT-ASSOC-REJECT":
			if i := strings.Index(rest, " status_code="); i >= 0 {
				rejected = firstField(rest[i+len(" status_code="):])
			}
		case "CTRL-EVENT-NETWORK-NOT-FOUND":
			notFound++
		}
	}
	switch {
	case tempDisabled == "WRONG_KEY":
		return "WRONG_KEY: the AP rejected the passphrase"
	case tempDisabled != "":
		return "that it gave up on the network for now (" + tempDisabled + ")"
	case rejected != "":
		return "that the AP rejected the association (status " + rejected + ")"
	case notFound > 0:
		return "that it never found the network in its own scans"
	}
	return ""
}

// ctrlEvent splits a line that IS a control event: the event name first,
// after at most an interface ("wlan0:") and a timestamp ("1234.567890:").
// Anywhere else on a line it is somebody's text, such as an SSID quoted in
// "Trying to associate with ... (SSID='...')", and an SSID can say anything.
func ctrlEvent(line string) (name, rest string, ok bool) {
	i := strings.Index(line, "CTRL-EVENT-")
	if i < 0 {
		return "", "", false
	}
	prefix := strings.Fields(line[:i])
	if len(prefix) > 2 || (len(line[:i]) > 0 && !strings.HasSuffix(line[:i], " ")) {
		return "", "", false
	}
	for _, p := range prefix {
		if !strings.HasSuffix(p, ":") {
			return "", "", false
		}
	}
	ev := line[i:]
	if j := strings.IndexAny(ev, " \t\r"); j >= 0 {
		return ev[:j], ev[j:], true
	}
	return ev, "", true
}

func firstField(s string) string {
	if i := strings.IndexAny(s, " \t\r"); i >= 0 {
		return s[:i]
	}
	return s
}

// logMark is how far the network log had got before a change began.
type logMark struct {
	size int64
	ok   bool
}

func markNetLog() logMark {
	st, err := os.Stat(netLogP)
	if err != nil {
		return logMark{}
	}
	return logMark{size: st.Size(), ok: true}
}

// netLogCap bounds what is read back: one attempt writes a few KB.
const netLogCap = 64 * 1024

// netLogSince is what the network log gained after m. init rotates the log at
// 128KB to a single ".1", so a log now shorter than the mark rotated in
// between, and the rest of the old one is read first.
func netLogSince(m logMark) string {
	if !m.ok {
		return ""
	}
	st, err := os.Stat(netLogP)
	if err != nil {
		return ""
	}
	var s string
	if st.Size() >= m.size {
		s = readFrom(netLogP, m.size)
	} else {
		s = readFrom(netLogP+".1", m.size) + readFrom(netLogP, 0)
	}
	if len(s) > netLogCap {
		s = s[len(s)-netLogCap:]
	}
	return s
}

func readFrom(path string, off int64) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	if _, err := f.Seek(off, io.SeekStart); err != nil {
		return ""
	}
	b, _ := io.ReadAll(io.LimitReader(f, 4*netLogCap))
	return string(b)
}
