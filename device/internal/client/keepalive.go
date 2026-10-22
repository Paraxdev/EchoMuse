package client

import (
	"errors"
	"fmt"
	"log"
	"net"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// answerPings replaces gorilla's default ping handler on conn, for both planes.
//
// The default answers inline with a 1s deadline (writeWait in gorilla v1.5.3's
// conn.go) and swallows the timeout. WriteControl has to take the write lock
// that a frame write holds for the whole kernel write, and a mic frame on a
// backed-up socket holds it for up to wsWriteWait. So on exactly the links that
// need the keepalive most, the pong was dropped without a word and the
// controller closed the connection 30s later with "1011 keepalive ping
// timeout". On one Echo the data plane, which carries the continuous wake
// stream, closed that way about 15 times as often as the control plane.
//
// The handler here only hands the payload over and returns, so the read loop
// never waits on the write lock, and the writer gives the pong wsWriteWait.
// One pong is queued at a time: a ping arriving before the previous pong went
// out replaces it. RFC 6455 5.5.3 allows answering only the most recent ping,
// and the controller's websockets library counts that pong as answering the
// earlier ones too.
func answerPings(conn *websocket.Conn, plane string, failures *rateLog, done <-chan struct{}) {
	pending := make(chan string, 1)
	conn.SetPingHandler(func(appData string) error {
		for {
			select {
			case pending <- appData:
				return nil
			default:
			}
			select {
			case <-pending:
			default:
			}
		}
	})
	go func() {
		for {
			var data string
			select {
			case <-done:
				return
			case data = <-pending:
			}
			err := conn.WriteControl(websocket.PongMessage, []byte(data), time.Now().Add(wsWriteWait))
			if err == nil || errors.Is(err, websocket.ErrCloseSent) {
				continue
			}
			select {
			case <-done:
				return // the connection is going anyway
			default:
			}
			// gorilla marks one case temporary: the write lock was not free
			// within the deadline, and the connection is still sound. Anything
			// else broke its write side for good, which the default handler
			// would have returned to the read loop to end the connection; close
			// it so the read loop ends now rather than at its deadline.
			var ne net.Error
			if errors.As(err, &ne) && ne.Temporary() {
				failures.printf("%s keepalive pong not sent within %s: %v", plane, wsWriteWait, err)
				continue
			}
			failures.printf("%s keepalive pong failed: %v (closing connection)", plane, err)
			conn.Close()
			return
		}
	}()
}

// Pong failures come in runs on a bad link, and the device's log is RAM-backed.
var (
	controlPongLog = &rateLog{every: time.Minute}
	dataPongLog    = &rateLog{every: time.Minute}
)

// rateLog prints at most one line per interval, saying how many it stood for.
type rateLog struct {
	mu     sync.Mutex
	every  time.Duration
	last   time.Time
	missed int
}

func (r *rateLog) printf(format string, args ...interface{}) {
	r.mu.Lock()
	now := time.Now()
	if !r.last.IsZero() && now.Sub(r.last) < r.every {
		r.missed++
		r.mu.Unlock()
		return
	}
	missed := r.missed
	r.last, r.missed = now, 0
	r.mu.Unlock()
	msg := fmt.Sprintf(format, args...)
	if missed > 0 {
		msg += fmt.Sprintf(" [%d more since the last report]", missed)
	}
	log.Print(msg)
}
