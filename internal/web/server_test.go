package web

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/munozajj/caplog/internal/logbook"
)

func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	store, err := logbook.Open(filepath.Join(t.TempDir(), "web.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	ts := httptest.NewServer(New(store, Config{}))
	t.Cleanup(ts.Close)
	return ts
}

func post(t *testing.T, ts *httptest.Server, path, body string, guard bool) *http.Response {
	t.Helper()
	req, _ := http.NewRequest("POST", ts.URL+path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if guard {
		req.Header.Set("X-CapLog", "1")
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func TestEndToEnd(t *testing.T) {
	ts := newTestServer(t)

	// Fresh install needs setup.
	var st map[string]any
	res, _ := http.Get(ts.URL + "/api/state")
	json.NewDecoder(res.Body).Decode(&st)
	if st["setup_needed"] != true {
		t.Fatalf("expected setup_needed: %v", st)
	}
	if res := post(t, ts, "/api/setup", `{"title":"Ops Log"}`, true); res.StatusCode != 201 {
		t.Fatalf("setup: %d", res.StatusCode)
	}
	if res := post(t, ts, "/api/setup", `{"title":"Other"}`, true); res.StatusCode != 409 {
		t.Fatalf("second setup should be 409, got %d", res.StatusCode)
	}

	// Writes without the X-CapLog header are refused.
	if res := post(t, ts, "/api/entries", `{"first_name":"A","last_name":"B","text":"x"}`, false); res.StatusCode != 403 {
		t.Fatalf("unguarded post should be 403, got %d", res.StatusCode)
	}

	// Open a live stream, then post; the entry should arrive on the stream.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", ts.URL+"/api/events?after=0", nil)
	sse, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer sse.Body.Close()
	if ct := sse.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("content type %q", ct)
	}
	if res := post(t, ts, "/api/entries", `{"first_name":"Jane","last_name":"Doe","text":"hello"}`, true); res.StatusCode != 201 {
		t.Fatalf("entry: %d", res.StatusCode)
	}
	if res := post(t, ts, "/api/entries", `{"first_name":"Jane","last_name":"Doe","text":"`+strings.Repeat("a", 501)+`"}`, true); res.StatusCode != 400 {
		t.Fatalf("501 chars should be 400, got %d", res.StatusCode)
	}
	sc := bufio.NewScanner(sse.Body)
	found := false
	for sc.Scan() {
		if line := sc.Text(); strings.HasPrefix(line, "data: ") && strings.Contains(line, `"body":"hello"`) {
			found = true
			break
		}
	}
	if !found {
		t.Fatal("entry was not delivered over SSE")
	}

	// Exports.
	res = post(t, ts, "/api/export/pdf", "{}", true)
	pdf, _ := io.ReadAll(res.Body)
	if res.StatusCode != 200 || !bytes.HasPrefix(pdf, []byte("%PDF")) {
		t.Fatalf("pdf export failed: %d", res.StatusCode)
	}
	res = post(t, ts, "/api/export/json", "{}", true)
	js, _ := io.ReadAll(res.Body)
	f, err := logbook.ParseExport(js)
	if err != nil {
		t.Fatalf("json export invalid: %v", err)
	}
	if n := len(f.Records); n != 4 || f.Records[2].Kind != "export" || f.Records[3].Kind != "export" {
		t.Fatalf("expected title, entry, pdf-export note, json-export note; got %d records", n)
	}

	// Import the export back in.
	res = post(t, ts, "/api/import", string(js), true)
	if res.StatusCode != 201 {
		b, _ := io.ReadAll(res.Body)
		t.Fatalf("import: %d %s", res.StatusCode, b)
	}

	// Verify.
	var rep logbook.Report
	res, _ = http.Get(ts.URL + "/api/verify")
	json.NewDecoder(res.Body).Decode(&rep)
	if !rep.OK || rep.HeadSeq != 8 {
		t.Fatalf("verify: %+v", rep)
	}

	// Pages are served with security headers and no cookies.
	res, _ = http.Get(ts.URL + "/")
	if res.StatusCode != 200 || res.Header.Get("Content-Security-Policy") == "" || len(res.Cookies()) != 0 {
		t.Fatalf("index: %d headers %v", res.StatusCode, res.Header)
	}
}

func TestEntryStoresRemoteIPAndIgnoresForwardedHeadersByDefault(t *testing.T) {
	ts := newTestServer(t)
	if res := post(t, ts, "/api/setup", `{"title":"Ops Log"}`, true); res.StatusCode != 201 {
		t.Fatalf("setup: %d", res.StatusCode)
	}

	req, _ := http.NewRequest("POST", ts.URL+"/api/entries", strings.NewReader(`{"first_name":"Jane","last_name":"Doe","text":"hello"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-CapLog", "1")
	req.Header.Set("X-Forwarded-For", "203.0.113.9")
	req.Header.Set("X-Real-IP", "203.0.113.10")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if res.StatusCode != 201 {
		t.Fatalf("entry: %d", res.StatusCode)
	}
	var rec logbook.Record
	if err := json.NewDecoder(res.Body).Decode(&rec); err != nil {
		t.Fatal(err)
	}
	if rec.IP != "127.0.0.1" {
		t.Fatalf("default should record RemoteAddr, not forwarded headers; got %q", rec.IP)
	}
}

func TestEntryUsesForwardedIPOnlyFromTrustedProxy(t *testing.T) {
	store, err := logbook.Open(filepath.Join(t.TempDir(), "web.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	nets, err := ParseTrustedProxies("127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(New(store, Config{TrustedProxies: nets}))
	t.Cleanup(ts.Close)
	if res := post(t, ts, "/api/setup", `{"title":"Ops Log"}`, true); res.StatusCode != 201 {
		t.Fatalf("setup: %d", res.StatusCode)
	}

	// Peer is trusted, so X-Forwarded-For is used.
	req, _ := http.NewRequest("POST", ts.URL+"/api/entries", strings.NewReader(`{"first_name":"Jane","last_name":"Doe","text":"via proxy"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-CapLog", "1")
	req.Header.Set("X-Forwarded-For", "203.0.113.9, 198.51.100.1")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	var rec logbook.Record
	if err := json.NewDecoder(res.Body).Decode(&rec); err != nil {
		t.Fatal(err)
	}
	if rec.IP != "203.0.113.9" {
		t.Fatalf("trusted proxy should record first X-Forwarded-For address; got %q", rec.IP)
	}

	// X-Real-IP is used when X-Forwarded-For is absent.
	req, _ = http.NewRequest("POST", ts.URL+"/api/entries", strings.NewReader(`{"first_name":"Jane","last_name":"Doe","text":"real ip"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-CapLog", "1")
	req.Header.Set("X-Real-IP", "203.0.113.25")
	res, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if err := json.NewDecoder(res.Body).Decode(&rec); err != nil {
		t.Fatal(err)
	}
	if rec.IP != "203.0.113.25" {
		t.Fatalf("trusted proxy should record X-Real-IP; got %q", rec.IP)
	}

	// A trusted list that does not include the peer still ignores headers.
	other, _ := ParseTrustedProxies("10.0.0.0/8")
	ts2 := httptest.NewServer(New(store, Config{TrustedProxies: other}))
	t.Cleanup(ts2.Close)
	req, _ = http.NewRequest("POST", ts2.URL+"/api/entries", strings.NewReader(`{"first_name":"Jane","last_name":"Doe","text":"untrusted"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-CapLog", "1")
	req.Header.Set("X-Forwarded-For", "203.0.113.9")
	res, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	if err := json.NewDecoder(res.Body).Decode(&rec); err != nil {
		t.Fatal(err)
	}
	if rec.IP != "127.0.0.1" {
		t.Fatalf("untrusted peer should record RemoteAddr; got %q", rec.IP)
	}
}

func TestParseTrustedProxies(t *testing.T) {
	nets, err := ParseTrustedProxies("127.0.0.1, 10.0.0.0/8, ::1")
	if err != nil || len(nets) != 3 {
		t.Fatalf("got %v err %v", nets, err)
	}
	if !nets[0].Contains(net.ParseIP("127.0.0.1")) || nets[0].Contains(net.ParseIP("127.0.0.2")) {
		t.Fatalf("bare IP should be a single address: %v", nets[0])
	}
	if !nets[1].Contains(net.ParseIP("10.9.8.7")) {
		t.Fatalf("CIDR not parsed: %v", nets[1])
	}
	if _, err := ParseTrustedProxies("not-an-ip"); err == nil {
		t.Fatal("expected error")
	}
}
