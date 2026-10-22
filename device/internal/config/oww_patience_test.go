package config

import (
	"encoding/json"
	"testing"
)

// owwPatience: 1 frame unless the controller says otherwise, and a push that
// does not mention it leaves it alone.
func TestOwwPatienceDefaultsToOneFrameAndSurvivesSparsePushes(t *testing.T) {
	d := &Device{}
	d.loadDefaults()
	if got := d.Snapshot().OwwPatience; got != 1 {
		t.Fatalf("default patience %d, want 1", got)
	}

	var msg ConfigMessage
	if err := json.Unmarshal([]byte(`{"type":"config","owwPatience":3}`), &msg); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	d.Apply(msg)
	if got := d.Snapshot().OwwPatience; got != 3 {
		t.Fatalf("owwPatience=3 applied as %d", got)
	}

	d.Apply(ConfigMessage{Type: "config", OwwThreshold: 0.4})
	if got := d.Snapshot().OwwPatience; got != 3 {
		t.Fatalf("a push without owwPatience changed it to %d", got)
	}

	d.Apply(ConfigMessage{Type: "config", OwwPatience: 1})
	if got := d.Snapshot().OwwPatience; got != 1 {
		t.Fatalf("owwPatience=1 did not turn it back off: %d", got)
	}
}

func TestOwwPatienceIsClamped(t *testing.T) {
	d := &Device{}
	d.loadDefaults()
	d.Apply(ConfigMessage{OwwPatience: 500})
	if got := d.Snapshot().OwwPatience; got != MaxOwwPatience {
		t.Fatalf("owwPatience=500 applied as %d, want %d", got, MaxOwwPatience)
	}
	d.Apply(ConfigMessage{OwwPatience: -2})
	if got := d.Snapshot().OwwPatience; got != MaxOwwPatience {
		t.Fatalf("a negative value is not a setting, yet it changed patience to %d", got)
	}
}

// The wire name is the contract with the controller.
func TestOwwPatienceWireName(t *testing.T) {
	b, _ := json.Marshal(ConfigMessage{OwwPatience: 2})
	if string(b) != `{"owwPatience":2}` {
		t.Fatalf("encodes as %s", b)
	}
}
