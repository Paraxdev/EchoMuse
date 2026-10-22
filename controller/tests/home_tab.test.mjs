// Tests for what the Home tab and Activity read: _activityTotals(),
// _outcomeLabel(), _lastDrop(), _wakeWhere(), _thresholdText().
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
const activityTotals = lift("_activityTotals");
const outcomeLabel   = lift("_outcomeLabel");
const lastDrop       = lift("_lastDrop");
const wakeWhere      = lift("_wakeWhere");
const thresholdText  = lift("_thresholdText");

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

// _lastDrop: absent on a controller from before the field.
check("no field, no reason", lastDrop({ connected: false }) === null);
check("a field with no why is no reason", lastDrop({ lastDisconnect: { at: 1 } }) === null);
const drop = lastDrop({ lastDisconnect: { at: 1761096317, why: "we closed it: 1011 (internal error) 'keepalive ping timeout'", routine: false } });
check("the reason is passed through as written", drop.why.endsWith("'keepalive ping timeout'"));
check("the time is kept", drop.at === 1761096317);
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

if (failed) { console.error(`\n${failed} check(s) failed`); process.exit(1); }
console.log("home_tab: all checks passed");
