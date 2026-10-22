package client

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// slowConn stands in for a socket whose send buffer is full: a large write
// (a mic frame) sits in the kernel for `hold`, and gorilla holds its write
// lock for all of it. Small writes (control frames) pass straight through.
type slowConn struct {
	net.Conn
	hold    time.Duration
	started chan struct{}
	once    sync.Once
}

func (s *slowConn) Write(b []byte) (int, error) {
	if len(b) >= 1024 {
		s.once.Do(func() { close(s.started) })
		time.Sleep(s.hold)
	}
	return s.Conn.Write(b)
}

type pingHarness struct {
	server *websocket.Conn // the controller's end
	client *websocket.Conn // the device's end
	slow   *slowConn
	pongs  chan string
	texts  chan time.Time
	done   chan struct{}
}

// newPingHarness connects a real gorilla client to a real gorilla server
// in-process. install sets up the device end's ping handling; nil leaves
// gorilla's default.
func newPingHarness(t *testing.T, hold time.Duration, install func(*websocket.Conn, <-chan struct{})) *pingHarness {
	t.Helper()
	h := &pingHarness{
		pongs: make(chan string, 4),
		texts: make(chan time.Time, 4),
		done:  make(chan struct{}),
	}
	serverConn := make(chan *websocket.Conn, 1)
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		c.SetPongHandler(func(d string) error { h.pongs <- d; return nil })
		serverConn <- c
		for {
			if _, _, err := c.ReadMessage(); err != nil {
				return
			}
		}
	}))
	t.Cleanup(srv.Close)

	d := websocket.Dialer{
		NetDialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			c, err := (&net.Dialer{}).DialContext(ctx, network, addr)
			if err != nil {
				return nil, err
			}
			h.slow = &slowConn{Conn: c, hold: hold, started: make(chan struct{})}
			return h.slow, nil
		},
	}
	c, _, err := d.Dial("ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	h.client = c
	h.server = <-serverConn
	t.Cleanup(func() {
		close(h.done)
		h.client.Close()
		h.server.Close()
	})
	if install != nil {
		install(c, h.done)
	}
	go func() {
		for {
			mt, _, err := c.ReadMessage()
			if err != nil {
				return
			}
			if mt == websocket.TextMessage {
				h.texts <- time.Now()
			}
		}
	}()
	return h
}

// blockWriter starts a mic-sized write that holds the client's write lock,
// and returns once it is in the kernel.
func (h *pingHarness) blockWriter(t *testing.T) {
	t.Helper()
	go h.client.WriteMessage(websocket.BinaryMessage, make([]byte, 2563))
	select {
	case <-h.slow.started:
	case <-time.After(2 * time.Second):
		t.Fatal("the mic write never started")
	}
}

func (h *pingHarness) ping(t *testing.T, payload string) time.Time {
	t.Helper()
	sent := time.Now()
	if err := h.server.WriteControl(websocket.PingMessage, []byte(payload), sent.Add(time.Second)); err != nil {
		t.Fatalf("server ping: %v", err)
	}
	return sent
}

// The fault: a ping that arrives while a mic frame is stuck in the kernel for
// longer than gorilla's 1s writeWait. The default handler gives up on the pong
// and says nothing; the controller then times the connection out.
func TestPongIsAnsweredWhileAFrameWriteHoldsTheLock(t *testing.T) {
	const hold = 2 * time.Second
	h := newPingHarness(t, hold, func(c *websocket.Conn, done <-chan struct{}) {
		answerPings(c, "[test]", &rateLog{every: time.Minute}, done)
	})
	h.blockWriter(t)
	sent := h.ping(t, "ka-1")
	if err := h.server.WriteMessage(websocket.TextMessage, []byte("after the ping")); err != nil {
		t.Fatal(err)
	}

	// The read loop must not wait for the pong: the next message is read
	// while the frame is still stuck.
	select {
	case at := <-h.texts:
		if waited := at.Sub(sent); waited > hold/2 {
			t.Fatalf("the read loop waited %v behind the pong", waited)
		}
	case <-time.After(hold):
		t.Fatal("the read loop stalled behind the pong")
	}

	select {
	case got := <-h.pongs:
		if got != "ka-1" {
			t.Fatalf("pong carried %q, want the ping's payload", got)
		}
		if at := time.Since(sent); at < hold/2 {
			t.Fatalf("pong arrived after %v, before the blocked write could have finished", at)
		}
	case <-time.After(hold + 3*time.Second):
		t.Fatal("pong never arrived")
	}
}

// The control for the test above: with gorilla's own handler the same harness
// loses the pong, so the test is measuring the fault and not its own setup.
func TestDefaultPingHandlerDropsThePongBehindABlockedWrite(t *testing.T) {
	const hold = 2 * time.Second
	h := newPingHarness(t, hold, nil)
	h.blockWriter(t)
	h.ping(t, "ka-1")
	select {
	case got := <-h.pongs:
		t.Fatalf("gorilla's default handler answered (%q); the harness no longer holds the lock past its 1s", got)
	case <-time.After(hold + time.Second):
	}
}

// Pings that pile up behind one stuck write get one pong, for the latest.
func TestQueuedPingsCoalesceToTheLatest(t *testing.T) {
	const hold = 2 * time.Second
	h := newPingHarness(t, hold, func(c *websocket.Conn, done <-chan struct{}) {
		answerPings(c, "[test]", &rateLog{every: time.Minute}, done)
	})
	h.blockWriter(t)
	h.ping(t, "ka-1")
	// The writer has taken ka-1 and is waiting on the lock; these two queue,
	// and the second replaces the first.
	time.Sleep(100 * time.Millisecond)
	h.ping(t, "ka-2")
	h.ping(t, "ka-3")

	var got []string
	deadline := time.After(hold + 3*time.Second)
	for len(got) < 2 {
		select {
		case p := <-h.pongs:
			got = append(got, p)
		case <-deadline:
			t.Fatalf("pongs %v, want [ka-1 ka-3]", got)
		}
	}
	select {
	case p := <-h.pongs:
		got = append(got, p)
	case <-time.After(300 * time.Millisecond):
	}
	if strings.Join(got, ",") != "ka-1,ka-3" {
		t.Fatalf("pongs %v, want [ka-1 ka-3]", got)
	}
}

func TestRateLogCountsWhatItHolds(t *testing.T) {
	r := &rateLog{every: time.Hour}
	r.printf("one")
	r.printf("two")
	r.printf("three")
	r.mu.Lock()
	missed := r.missed
	r.mu.Unlock()
	if missed != 2 {
		t.Fatalf("held %d lines, want 2", missed)
	}
}

// Both long-lived planes must install it; gorilla's default is what failed.
func TestBothPlanesAnswerPingsThemselves(t *testing.T) {
	for _, f := range []string{"control.go", "data.go"} {
		src, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(string(src), "answerPings(conn, ") {
			t.Errorf("%s no longer installs answerPings on its connection", f)
		}
	}
}
