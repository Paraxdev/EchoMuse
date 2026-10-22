// Tests for what the Home tab and Activity read: _activityTotals(),
// _outcomeLabel(), _lastDrop(), _wakeWhere(), _thresholdText(),
// _patienceText().
//
//     node controller/tests/home_tab.test.mjs
//
// Each is a statement about an Echo somebody acts on, so the cases are the
// ones where a plausible answer would be the wrong one: a day nobody
// measured read as a quiet day, a controller from before lastDisconnect read
// as an Echo that dropped for no reason, and a configured wake mode read as
// the mode the Echo is in.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(HERE, "..", "static", "dashboard.jsx"), "utf8");
function lift(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}()`);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) break; }
  }
  return eval(`(${src.slice(start, i + 1)})`);
}
function liftObject(name) {
  const start = src.indexOf(`const ${name} = {`);
  if (start < 0) throw new Error(`dashboard.jsx no longer defines ${name}`);
  return eval(`(${src.slice(src.indexOf("{", start), src.indexOf("};", start) + 1)})`);
}
const activityTotals = lift("_activityTotals");
const outcomeLabel   = lift("_outcomeLabel");
const lastDrop       = lift("_lastDrop");
const wakeWhere      = lift("_wakeWhere");
const thresholdText  = lift("_thresholdText");
const onDeviceMode   = lift("onDeviceMode");   // _patienceText calls it by name
const patienceText   = lift("_patienceText");
// The scan helpers call these by name, so they are lifted under it.
const wifiBand         = lift("wifiBand");
const classifySecurity = lift("classifySecurity");
const securityBlocker  = lift("securityBlocker");
const _SECURITY_LABEL  = liftObject("_SECURITY_LABEL");
const scanBands        = lift("_scanBands");
const scanSecurity     = lift("_scanSecurity");

let failed = 0;
const check = (name, cond) => { if (!cond) { failed++; console.error(`FAIL: ${name}`); } };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// _activityTotals: GET /api/devices/{id}/activity?days=1 buckets by local
// day, so the last 24 hours across midnight is two buckets.
const twoDays = { days: [
  { date: "2026-10-21", turns: 12, ok: 1, outcomes: { no_speech: 10, ok: 1, no_tts: 1 } },
  { date: "2026-10-22", turns: 6,  ok: 0, outcomes: { no_speech: 6 } },
] };
check("buckets either side of midnight are summed",
      same(activityTotals(twoDays), { turns: 18, ok: 1, heardNothing: 16 }));
check("a day with no no_speech counts zero heard-nothing",
      activityTotals({ days: [{ turns: 3, ok: 3, outcomes: { ok: 3 } }] }).heardNothing === 0);
check("a bucket without outcomes does not throw",
      same(activityTotals({ days: [{ turns: 2, ok: 2 }] }), { turns: 2, ok: 2, heardNothing: 0 }));
check("no turns at all is zero, which was measured", same(activityTotals({ days: [] }),
      { turns: 0, ok: 0, heardNothing: 0 }));
check("no answer is unknown, never zero", activityTotals(null) === null);
check("a body without days is unknown", activityTotals({ error: "x" }) === null);

// _outcomeLabel
check("no_speech reads as heard nothing", outcomeLabel("no_speech") === "heard nothing");
check("other outcomes keep their words", outcomeLabel("pipeline_refused") === "pipeline refused");
check("ok stays ok", outcomeLabel("ok") === "ok");
check("a missing outcome is a question mark", outcomeLabel(null) === "?");

// _lastDrop: absent on a controller from before the field. `at` is epoch
// milliseconds on the wire (em_device_view.note_disconnect) and seconds out,
// the unit relTime and _clockTime take.
check("no field, no reason", lastDrop({ connected: false }) === null);
check("a field with no why is no reason", lastDrop({ lastDisconnect: { at: 1 } }) === null);
const drop = lastDrop({ lastDisconnect: { at: 1761096317250, why: "we closed it: 1011 (internal error) 'keepalive ping timeout'", routine: false } });
check("the reason is passed through as written", drop.why.endsWith("'keepalive ping timeout'"));
check("epoch ms comes out as epoch seconds", drop.at === 1761096317.25);
const justNow = lastDrop({ lastDisconnect: { at: Date.now() - 5000, why: "x" } }).at;
check("a drop five seconds ago is five seconds ago, not in the future",
      Date.now() / 1000 - justNow >= 4 && Date.now() / 1000 - justNow < 60);
check("a routine close says so",
      lastDrop({ lastDisconnect: { at: 5, why: "peer closed it: 1001 (going away)", routine: true } })
        .why.endsWith("(routine)"));
check("a missing time is null, not 1970", lastDrop({ lastDisconnect: { why: "x" } }).at === null);

// _wakeWhere: the Echo's report, never the configuration.
const on = (state, extra = {}) => ({ connected: true, listen: { state, streams: null, reason: "" }, ...extra });
check("local is on this Echo", wakeWhere(on("local")) === "On this Echo");
check("controller says it streams", wakeWhere(on("controller")).includes("streaming"));
check("diagnostic says it streams", wakeWhere(on("diagnostic")).includes("streaming"));
check("degraded is button only", wakeWhere(on("degraded")).includes("button only"));
check("legacy with a trigger scores on the Echo",
      wakeWhere(on("legacy", { owwTriggerCapable: true })) === "On this Echo, streaming");
check("legacy without a trigger scores on the controller",
      wakeWhere(on("legacy", { owwTriggerCapable: false })) === "On the controller, streaming");
check("unknown with streams null is not reported, never private",
      wakeWhere(on("unknown")) === "Not reported yet");
check("unknown while streaming says it streams",
      wakeWhere({ connected: true, listen: { state: "unknown", streams: true } }).includes("streaming"));
check("offline has no report", wakeWhere({ connected: false, listen: { state: "local" } }) === null);
check("connected with no report has none", wakeWhere({ connected: true, listen: null }) === null);

// _thresholdText
check("a stored threshold reads as stored", thresholdText(0.175) === "0.175");
check("no trailing zeros", thresholdText(0.5) === "0.5");
check("float noise is trimmed", thresholdText(0.30000000000000004) === "0.3");
check("absent is a dash", thresholdText(undefined) === "—");

// _patienceText: the setting where it applies, never a claim about an Echo
// that decides its own wakes on firmware that ignores it.
const echo = (extra = {}) => ({ connected: true, owwShadowCapable: true, owwTriggerCapable: true, ...extra });
check("no key, no row (a controller from before owwPatience)",
      patienceText({ owwOnDevice: "on" }, echo()) === null);
check("the controller applies it to the wakes it decides",
      same(patienceText({ owwPatience: 2, owwOnDevice: "off" }, echo()), { text: "2 frames", applied: true }));
check("one frame is singular",
      patienceText({ owwPatience: 1, owwOnDevice: "off" }, echo()).text === "1 frame");
check("an Echo deciding its own wakes without oww_patience does not apply it",
      patienceText({ owwPatience: 2, owwOnDevice: "on" }, echo()).applied === false);
check("an Echo deciding its own wakes with oww_patience applies it",
      patienceText({ owwPatience: 3, owwOnDevice: "on" }, echo({ owwPatienceCapable: true })).applied === true);
check("on against firmware that cannot trigger is decided by the controller",
      patienceText({ owwPatience: 2, owwOnDevice: "on" }, echo({ owwTriggerCapable: false })).applied === true);
check("shadow is decided by the controller",
      patienceText({ owwPatience: 2, owwOnDevice: "shadow" }, echo()).applied === true);
check("offline is shown as set, as the Config form treats it",
      patienceText({ owwPatience: 2, owwOnDevice: "on" }, { connected: false }).applied === true);

// _scanBands: Connectivity's networks carry freqs from newer firmware only.
check("2.4 only", scanBands([2412, 2437]) === "2.4 GHz");
check("5 only", scanBands([5180]) === "5 GHz");
check("both bands, either order", scanBands([5785, 2462]) === "2.4 + 5 GHz");
check("absent freqs is unknown, not 2.4", scanBands(undefined) === null);
check("empty freqs is unknown", scanBands([]) === null);
check("a zero frequency names no band", scanBands([0]) === null);

// _scanSecurity: absent flags are unknown, never open.
check("absent flags are unknown", scanSecurity(undefined) === null);
check("empty flags are unknown", scanSecurity("") === null);
const wpa2 = scanSecurity("[WPA2-PSK-CCMP][ESS]");
check("WPA2 is labelled and joinable", wpa2.label === "WPA2" && wpa2.blocker === null);
const wpa3 = scanSecurity("[SAE-CCMP][ESS]");
check("WPA3-only is refused with a reason", wpa3.security === "wpa3" && typeof wpa3.blocker === "string");
check("WPA2/WPA3 transition joins through WPA2",
      scanSecurity("[WPA2-PSK+SAE-CCMP][ESS]").blocker === null);
check("open is labelled open", scanSecurity("[ESS]").label === "Open");
check("enterprise is refused", typeof scanSecurity("[WPA2-EAP-CCMP][ESS]").blocker === "string");
check("the refusal does not name the wizard, which this is not",
      !/wizard/i.test(scanSecurity("[WEP][ESS]").blocker));

if (failed) { console.error(`\n${failed} check(s) failed`); process.exit(1); }
console.log("home_tab: all checks passed");
