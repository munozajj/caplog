// Package web is CapLog's HTTP server: the embedded web page, the JSON API,
// live updates (Server-Sent Events), exports, imports and the verify page.
//
// Privacy: nothing here logs or stores IP addresses, user agents or cookies.
// There is no request log at all; only errors are logged, without client info.
package web

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/munozajj/caplog/internal/logbook"
	"github.com/munozajj/caplog/internal/pdfexport"
)

//go:embed static
var staticFiles embed.FS

// Config holds what the server needs to know.
type Config struct {
	CheckpointPath string            // where signed checkpoints are written
	PublicKey      ed25519.PublicKey // nil if no signing key is configured
	MaxImportBytes int64             // biggest import file accepted
}

// Server serves CapLog over HTTP.
type Server struct {
	store *logbook.Store
	cfg   Config
	hub   *hub
	mux   *http.ServeMux
}

// New wires up all routes.
func New(store *logbook.Store, cfg Config) *Server {
	if cfg.MaxImportBytes <= 0 {
		cfg.MaxImportBytes = 20 << 20
	}
	s := &Server{store: store, cfg: cfg, hub: newHub(), mux: http.NewServeMux()}
	store.OnAppend(s.hub.publish) // every saved record goes out live

	static, _ := fs.Sub(staticFiles, "static")
	files := http.FileServerFS(static)

	s.mux.HandleFunc("GET /{$}", page(static, "index.html"))
	s.mux.HandleFunc("GET /verify", page(static, "verify.html"))
	s.mux.Handle("GET /static/", http.StripPrefix("/static/", files))
	s.mux.HandleFunc("GET /healthz", s.health)

	s.mux.HandleFunc("GET /api/state", s.state)
	s.mux.HandleFunc("GET /api/records", s.records)
	s.mux.HandleFunc("GET /api/events", s.events)
	s.mux.HandleFunc("GET /api/verify", s.verify)
	s.mux.HandleFunc("POST /api/setup", guard(s.setup))
	s.mux.HandleFunc("POST /api/entries", guard(s.addEntry))
	s.mux.HandleFunc("POST /api/export/{format}", guard(s.export))
	s.mux.HandleFunc("POST /api/import", guard(s.importLog))
	return s
}

// ServeHTTP adds safety headers to every response.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	h := w.Header()
	h.Set("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Referrer-Policy", "no-referrer")
	h.Set("X-Frame-Options", "DENY")
	s.mux.ServeHTTP(w, r)
}

// guard protects actions that write to the log. Browsers can only send the
// custom "X-CapLog" header from our own page (other sites would need CORS
// permission, which we never grant), so random websites can't make a
// visitor's browser post entries.
func guard(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-CapLog") != "1" {
			writeErr(w, http.StatusForbidden, "missing X-CapLog: 1 header")
			return
		}
		next(w, r)
	}
}

func page(static fs.FS, name string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		b, err := fs.ReadFile(static, name)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-cache")
		w.Write(b)
	}
}

// ---- small JSON helpers ----

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

// fail turns a store error into the right HTTP response.
func fail(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, logbook.ErrInvalidInput):
		writeErr(w, http.StatusBadRequest, err.Error())
	case errors.Is(err, logbook.ErrTitleIsSet):
		writeErr(w, http.StatusConflict, err.Error())
	case errors.Is(err, logbook.ErrNoTitle):
		writeErr(w, http.StatusConflict, err.Error())
	default:
		slog.Error("request failed", "err", err)
		writeErr(w, http.StatusInternalServerError, "internal error")
	}
}

func readJSON(r *http.Request, max int64, v any) error {
	dec := json.NewDecoder(io.LimitReader(r.Body, max))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return &logbook.ValidationError{Msg: "invalid JSON body: " + err.Error()}
	}
	return nil
}

// ---- handlers ----

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	if err := s.store.DB().PingContext(r.Context()); err != nil {
		http.Error(w, "database unavailable", http.StatusServiceUnavailable)
		return
	}
	io.WriteString(w, "ok\n")
}

func (s *Server) state(w http.ResponseWriter, r *http.Request) {
	out := map[string]any{
		"setup_needed":   false,
		"max_text_chars": logbook.MaxTextChars,
		"max_name_chars": logbook.MaxNameChars,
		"signing":        s.cfg.PublicKey != nil,
	}
	head, err := s.store.Head(r.Context())
	switch {
	case errors.Is(err, logbook.ErrNoTitle):
		out["setup_needed"] = true
	case err != nil:
		fail(w, err)
		return
	default:
		title, _ := s.store.Title(r.Context())
		out["title"] = title
		out["head_seq"] = head.Seq
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) setup(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Title string `json:"title"`
	}
	if err := readJSON(r, 8<<10, &in); err != nil {
		fail(w, err)
		return
	}
	rec, err := s.store.Setup(r.Context(), in.Title)
	if err != nil {
		fail(w, err)
		return
	}
	slog.Info("logbook created", "title", rec.Body)
	writeJSON(w, http.StatusCreated, rec)
}

func (s *Server) addEntry(w http.ResponseWriter, r *http.Request) {
	var in struct {
		FirstName string `json:"first_name"`
		LastName  string `json:"last_name"`
		Text      string `json:"text"`
	}
	if err := readJSON(r, 16<<10, &in); err != nil {
		fail(w, err)
		return
	}
	rec, err := s.store.AddEntry(r.Context(), in.FirstName, in.LastName, in.Text)
	if err != nil {
		fail(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, rec)
}

// records returns a page of records: ?after=<entry number>&limit=<n>.
func (s *Server) records(w http.ResponseWriter, r *http.Request) {
	after := int64(-1)
	if v := r.URL.Query().Get("after"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			writeErr(w, http.StatusBadRequest, "bad 'after'")
			return
		}
		after = n
	}
	limit := 500
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 1000 {
			limit = n
		}
	}
	recs, err := s.store.List(r.Context(), after, limit)
	if err != nil {
		fail(w, err)
		return
	}
	if recs == nil {
		recs = []logbook.Record{}
	}
	writeJSON(w, http.StatusOK, recs)
}

// events streams new records with Server-Sent Events. The browser tells us
// the last entry number it has (Last-Event-ID header on reconnect, or
// ?after= on first connect) and we send everything after that, then live
// updates as they happen.
func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	rc := http.NewResponseController(w)
	after := int64(-1)
	if v := r.Header.Get("Last-Event-ID"); v != "" {
		after, _ = strconv.ParseInt(v, 10, 64)
	} else if v := r.URL.Query().Get("after"); v != "" {
		after, _ = strconv.ParseInt(v, 10, 64)
	}

	// Subscribe BEFORE reading the backlog so nothing saved in between is missed.
	ch := s.hub.subscribe()
	if ch == nil {
		writeErr(w, http.StatusServiceUnavailable, "too many live connections, try again later")
		return
	}
	defer s.hub.unsubscribe(ch)

	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("X-Accel-Buffering", "no") // tell nginx-style proxies not to buffer
	w.WriteHeader(http.StatusOK)
	io.WriteString(w, "retry: 3000\n\n")

	send := func(seq int64, data []byte) error {
		if seq <= after {
			return nil // already sent
		}
		after = seq
		_, err := fmt.Fprintf(w, "id: %d\nevent: record\ndata: %s\n\n", seq, data)
		return err
	}

	// Catch up from the database.
	for {
		recs, err := s.store.List(r.Context(), after, 500)
		if err != nil {
			return
		}
		for _, rec := range recs {
			b, _ := json.Marshal(rec)
			if send(rec.Seq, b) != nil {
				return
			}
		}
		if len(recs) < 500 {
			break
		}
	}
	rc.Flush()

	ping := time.NewTicker(25 * time.Second) // keeps proxies from closing idle streams
	defer ping.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case ev, ok := <-ch:
			if !ok {
				return // we fell behind; the browser will reconnect and catch up
			}
			if send(ev.seq, ev.data) != nil {
				return
			}
		case <-ping.C:
			if _, err := io.WriteString(w, ": ping\n\n"); err != nil {
				return
			}
		}
		rc.Flush()
	}
}

// export appends an "export" note to the log, then sends the file.
func (s *Server) export(w http.ResponseWriter, r *http.Request) {
	format := r.PathValue("format")
	if format != "json" && format != "pdf" {
		writeErr(w, http.StatusNotFound, "format must be json or pdf")
		return
	}
	ctx := r.Context()
	title, err := s.store.Title(ctx)
	if err != nil {
		fail(w, err)
		return
	}
	rec, err := s.store.RecordExport(ctx, format)
	if err != nil {
		fail(w, err)
		return
	}
	stamp := strings.NewReplacer(":", "", "-", "").Replace(rec.CreatedAt)
	if len(stamp) > 15 {
		stamp = stamp[:15]
	}
	name := fmt.Sprintf("caplog-%s-%d", stamp, rec.Seq)

	switch format {
	case "json":
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Disposition", `attachment; filename="`+name+`.json"`)
		if err := s.store.WriteJSONExport(ctx, w, rec); err != nil {
			slog.Error("json export failed", "err", err)
		}
	case "pdf":
		var buf bytes.Buffer
		err := pdfexport.Write(&buf, title, rec, func(fn func(logbook.Record) error) error {
			return s.store.Each(ctx, rec.Seq, fn)
		})
		if err != nil {
			fail(w, err)
			return
		}
		w.Header().Set("Content-Type", "application/pdf")
		w.Header().Set("Content-Disposition", `attachment; filename="`+name+`.pdf"`)
		w.Write(buf.Bytes())
	}
}

// importLog accepts a CapLog JSON export as the request body.
func (s *Server) importLog(w http.ResponseWriter, r *http.Request) {
	data, err := io.ReadAll(io.LimitReader(r.Body, s.cfg.MaxImportBytes+1))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "could not read upload")
		return
	}
	if int64(len(data)) > s.cfg.MaxImportBytes {
		writeErr(w, http.StatusRequestEntityTooLarge, "import file is too large")
		return
	}
	res, err := s.store.Import(r.Context(), data)
	if err != nil {
		fail(w, err)
		return
	}
	slog.Info("import completed", "records", res.Imported, "source_title", res.SourceTitle)
	writeJSON(w, http.StatusCreated, res)
}

func (s *Server) verify(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Minute)
	defer cancel()
	writeJSON(w, http.StatusOK, s.store.Verify(ctx, s.cfg.CheckpointPath, s.cfg.PublicKey))
}
