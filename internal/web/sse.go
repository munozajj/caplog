package web

import (
	"encoding/json"
	"sync"

	"github.com/munozajj/caplog/internal/logbook"
)

// maxSubscribers caps how many browsers can hold a live connection at once,
// so a flood of connections can't exhaust memory.
const maxSubscribers = 2000

// event is one record, already encoded as JSON for sending.
type event struct {
	seq  int64
	data []byte
}

// hub fans out newly saved records to every connected browser.
type hub struct {
	mu   sync.Mutex
	subs map[chan event]struct{}
}

func newHub() *hub { return &hub{subs: map[chan event]struct{}{}} }

// subscribe returns a channel of new records, or nil if the hub is full.
func (h *hub) subscribe() chan event {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.subs) >= maxSubscribers {
		return nil
	}
	ch := make(chan event, 256)
	h.subs[ch] = struct{}{}
	return ch
}

func (h *hub) unsubscribe(ch chan event) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.subs[ch]; ok {
		delete(h.subs, ch)
		close(ch)
	}
}

// publish sends records to everyone. It never blocks: a browser that has
// fallen far behind is disconnected, and its EventSource reconnects and
// catches up from the database using the last entry number it saw.
func (h *hub) publish(recs []logbook.Record) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, r := range recs {
		b, _ := json.Marshal(r)
		ev := event{r.Seq, b}
		for ch := range h.subs {
			select {
			case ch <- ev:
			default:
				delete(h.subs, ch)
				close(ch)
			}
		}
	}
}
