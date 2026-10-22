// Tests for the Wake word stage: where it says the wake is decided, and when
// the Patience control is disabled.
//
//     node controller/tests/wake_stage.test.mjs
//
// The stage chip read "Controller" whatever the detection setting said, so an
// Echo listening for its own wake word was labelled as streaming to the
// controller. wakeDetector() resolves the mode the way
// em_shadow.effective_mode does: "on" against firmware without oww_trigger
// still runs on the controller, as shadow.
//
// Patience is applied by whoever decides the wake. The controller always can;
// an Echo deciding its own wakes can only on firmware announcing
// oww_patience, and elsewhere the control is shown disabled with the reason.

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
const onDeviceMode = lift("onDeviceMode");
const wakeDetector = lift("wakeDetector");

let failed = 0;
const check = (name, cond) => { if (!cond) { failed++; console.error(`FAIL: ${name}`); } };

check("off is the controller", wakeDetector({ owwOnDevice: "off" }, true) === "controller");
check("absent is the controller", wakeDetector({}, true) === "controller");
check("an unknown value is the controller, as normalise_mode reads it",
      wakeDetector({ owwOnDevice: "sideways" }, true) === "controller");
check("on with oww_trigger is the Echo", wakeDetector({ owwOnDevice: "on" }, true) === "device");
check("on is read case-insensitively", wakeDetector({ owwOnDevice: "ON" }, true) === "device");
check("on without oww_trigger runs as shadow",
      wakeDetector({ owwOnDevice: "on" }, false) === "both");
check("shadow is both", wakeDetector({ owwOnDevice: "shadow" }, true) === "both");
check("firmware without oww_shadow scores nothing, so the controller alone listens",
      wakeDetector({ owwOnDevice: "on" }, false, false) === "controller"
      && wakeDetector({ owwOnDevice: "shadow" }, false, false) === "controller");
check("shadowCapable defaults true, as the form's prop does",
      wakeDetector({ owwOnDevice: "shadow" }, false) === "both");

// The gate, lifted from the form so the test reads the expression in use.
const gate = src.match(/const patienceOff = ([^;]+);/);
if (!gate) throw new Error("dashboard.jsx no longer defines patienceOff");
const patienceOff = new Function("wakeBy", "patienceCapable", `return ${gate[1]};`);
check("an Echo deciding its own wakes without oww_patience is disabled",
      patienceOff("device", false) === true);
check("an Echo deciding its own wakes with oww_patience is live",
      patienceOff("device", true) === false);
check("the controller deciding is live whatever the firmware",
      patienceOff("controller", false) === false && patienceOff("both", false) === false);

const wakeBy = src.match(/const wakeBy = ([^;]+);/);
check("wakeBy comes from wakeDetector with the trigger and shadow capabilities",
      wakeBy && wakeBy[1].trim() === "wakeDetector(config, triggerCapable, shadowCapable)");

// The chips follow wakeBy rather than being fixed.
check("the Controller chip is hidden only where the Echo decides",
      src.includes(`{wakeBy !== 'device' && <ScopeChip tone="controller">Controller</ScopeChip>}`));
check("the Device chip is shown wherever the Echo scores",
      src.includes(`{wakeBy !== 'controller' && <ScopeChip tone="device">Device</ScopeChip>}`));

// Near-misses are on the Activity tab, not Status.
check("the stage no longer sends people to the Status tab for near-misses",
      !src.includes("near-misses (Status tab)"));
check("it names the Activity tab", src.includes("near-misses (Activity tab)"));

// The control: four values, writing owwPatience, disabled with the reason.
const at = src.indexOf('<Select label="Patience"');
check("the Patience control exists", at >= 0);
const el = src.slice(at, src.indexOf("/>", at) + 2);
check("it writes owwPatience", el.includes("set('owwPatience', v)"));
check("it offers 1 to 4", el.includes("[1, 2, 3, 4]"));
check("it defaults to 2 when the key is absent", el.includes("config.owwPatience ?? 2"));
check("it is disabled by the gate", el.includes("disabled={patienceOff}"));
check("and gives the reason when it is",
      /patienceOff\s*\?\s*'needs newer firmware/.test(el));

if (failed) { console.error(`${failed} check(s) failed.`); process.exit(1); }
console.log("wake_stage: all checks passed.");
