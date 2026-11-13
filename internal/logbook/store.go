package logbook

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	_ "modernc.org/sqlite" // pure-Go SQLite driver (no CGO needed)
)

// Limits on what a person can type. The text limit is also enforced in the
// browser, but the server is the one that really decides.
const (
	MaxTextChars  = 500
	MaxNameChars  = 100
	MaxTitleChars = 200
	MaxIPChars    = 64
)

// schema creates the records table and the triggers that make it append-only.
//
// The triggers are the database's own line of defence: even someone using the
// sqlite3 command line against the file gets an error if they try to UPDATE or
// DELETE a record. (They could drop the triggers first, which is why we also
// have the hash chain and signed checkpoints to detect that.)
const schema = `
CREATE TABLE IF NOT EXISTS records (
	seq        INTEGER PRIMARY KEY,  -- entry number, 0 = title record
	kind       TEXT NOT NULL,
	created_at TEXT NOT NULL,        -- UTC, RFC 3339
	first_name TEXT NOT NULL,
	last_name  TEXT NOT NULL,
	ip         TEXT NOT NULL,
	body       TEXT NOT NULL,
	meta       TEXT NOT NULL,
	prev_hash  TEXT NOT NULL,
	hash       TEXT NOT NULL UNIQUE
) STRICT;

CREATE TRIGGER IF NOT EXISTS records_no_update
BEFORE UPDATE ON records
BEGIN
	SELECT RAISE(ABORT, 'caplog: records are permanent and cannot be modified');
END;

CREATE TRIGGER IF NOT EXISTS records_no_delete
BEFORE DELETE ON records
BEGIN
	SELECT RAISE(ABORT, 'caplog: records are permanent and cannot be deleted');
END;

-- New rows must continue the chain: next number, correct previous hash,
-- and only row 0 may be the title.
CREATE TRIGGER IF NOT EXISTS records_insert_check
BEFORE INSERT ON records
BEGIN
	SELECT RAISE(ABORT, 'caplog: entry number must be the next in sequence')
	WHERE NEW.seq IS NOT (SELECT COALESCE(MAX(seq), -1) + 1 FROM records);
	SELECT RAISE(ABORT, 'caplog: previous hash must match the last record')
	WHERE NEW.prev_hash IS NOT COALESCE((SELECT hash FROM records ORDER BY seq DESC LIMIT 1), '` + "0000000000000000000000000000000000000000000000000000000000000000" + `');
	SELECT RAISE(ABORT, 'caplog: only record 0 may be the title')
	WHERE (NEW.seq = 0) IS NOT (NEW.kind = 'genesis');
END;
`

// Errors callers can check for.
var (
	ErrNoTitle      = errors.New("the logbook has not been set up yet (no title)")
	ErrTitleIsSet   = errors.New("the logbook title is already set and can never be changed")
	ErrInvalidInput = errors.New("invalid input")
)

// ValidationError explains which field was wrong. It wraps ErrInvalidInput.
type ValidationError struct{ Msg string }

func (e *ValidationError) Error() string { return e.Msg }
func (e *ValidationError) Unwrap() error { return ErrInvalidInput }

// Store is the logbook database. It is safe to use from many goroutines.
type Store struct {
	db *sql.DB

	// writeMu makes sure only one append happens at a time, so entry
	// numbers are always sequential with no gaps, even under heavy load.
	writeMu sync.Mutex

	// onAppend is called (while still holding writeMu, so in order) after
	// new records are safely committed. Used for live updates and checkpoints.
	onAppend []func([]Record)

	now func() time.Time // replaceable in tests
}

// Open opens (or creates) the logbook database file at path.
func Open(path string) (*Store, error) {
	return open(path, true)
}

// OpenExisting opens a database that must already exist, without creating
// or changing anything. Used by the "verify" command.
func OpenExisting(path string) (*Store, error) {
	return open(path, false)
}

func open(path string, create bool) (*Store, error) {
	// Connection settings ("pragmas") applied to every connection:
	//  - WAL journal: readers never block the writer.
	//  - synchronous FULL: every commit is flushed to disk (safest).
	//  - busy_timeout: wait instead of failing if the file is briefly locked.
	//  - temp_store MEMORY: never write temp files (root filesystem is read-only).
	//  - _txlock=immediate: transactions take the write lock up front.
	q := url.Values{}
	q.Add("_pragma", "busy_timeout(10000)")
	q.Add("_pragma", "journal_mode(WAL)")
	q.Add("_pragma", "synchronous(FULL)")
	q.Add("_pragma", "temp_store(MEMORY)")
	q.Add("_txlock", "immediate")
	if !create {
		q.Add("mode", "rw") // fail if the file does not exist
	}
	db, err := sql.Open("sqlite", "file:"+path+"?"+q.Encode())
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(4) // keep memory use small
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("opening database %s: %w", path, err)
	}
	if create {
		if _, err := db.Exec(schema); err != nil {
			db.Close()
			return nil, fmt.Errorf("creating schema: %w", err)
		}
	}
	if err := checkIPColumn(db); err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db, now: time.Now}, nil
}

// Close closes the database.
func (s *Store) Close() error { return s.db.Close() }

// DB exposes the raw database handle. Only tests should need this.
func (s *Store) DB() *sql.DB { return s.db }

// OnAppend registers a function to be told about newly committed records.
// It must not block for long and must not write to the store.
func (s *Store) OnAppend(fn func([]Record)) { s.onAppend = append(s.onAppend, fn) }

// Title returns the logbook title, or ErrNoTitle if setup has not happened.
func (s *Store) Title(ctx context.Context) (string, error) {
	var t string
	err := s.db.QueryRowContext(ctx, `SELECT body FROM records WHERE seq = 0`).Scan(&t)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrNoTitle
	}
	return t, err
}

// Head returns the newest record (or ErrNoTitle if the log is empty).
func (s *Store) Head(ctx context.Context) (Record, error) {
	recs, err := s.query(ctx, `SELECT `+cols+` FROM records ORDER BY seq DESC LIMIT 1`)
	if err != nil {
		return Record{}, err
	}
	if len(recs) == 0 {
		return Record{}, ErrNoTitle
	}
	return recs[0], nil
}

// Get returns a single record by entry number.
func (s *Store) Get(ctx context.Context, seq int64) (Record, bool, error) {
	recs, err := s.query(ctx, `SELECT `+cols+` FROM records WHERE seq = ?`, seq)
	if err != nil || len(recs) == 0 {
		return Record{}, false, err
	}
	return recs[0], true, nil
}

// List returns up to limit records with entry number greater than after,
// oldest first. Use after = -1 to start from the title record.
func (s *Store) List(ctx context.Context, after int64, limit int) ([]Record, error) {
	return s.query(ctx, `SELECT `+cols+` FROM records WHERE seq > ? ORDER BY seq LIMIT ?`, after, limit)
}

// Each calls fn for every record from #0 up to and including maxSeq
// (use -1 for "all"), in order, streaming from the database.
func (s *Store) Each(ctx context.Context, maxSeq int64, fn func(Record) error) error {
	if maxSeq < 0 {
		maxSeq = 1<<62 - 1
	}
	rows, err := s.db.QueryContext(ctx, `SELECT `+cols+` FROM records WHERE seq <= ? ORDER BY seq`, maxSeq)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		r, err := scan(rows)
		if err != nil {
			return err
		}
		if err := fn(r); err != nil {
			return err
		}
	}
	return rows.Err()
}

// Setup writes the title as record #0. It works exactly once.
func (s *Store) Setup(ctx context.Context, title string) (Record, error) {
	title = strings.TrimSpace(normalizeNewlines(title))
	if title == "" {
		return Record{}, &ValidationError{"a logbook title is required"}
	}
	if utf8.RuneCountInString(title) > MaxTitleChars {
		return Record{}, &ValidationError{fmt.Sprintf("the title can be at most %d characters", MaxTitleChars)}
	}
	recs, err := s.appendRecords(ctx, true, []Record{{Kind: KindGenesis, Body: title}})
	if err != nil {
		return Record{}, err
	}
	return recs[0], nil
}

// AddEntry validates and permanently appends a person's entry.
// ip is the address of the device that submitted it (may be empty).
func (s *Store) AddEntry(ctx context.Context, first, last, text, ip string) (Record, error) {
	first = strings.TrimSpace(first)
	last = strings.TrimSpace(last)
	text = strings.TrimSpace(normalizeNewlines(text))
	ip = strings.TrimSpace(ip)
	switch {
	case first == "" || last == "":
		return Record{}, &ValidationError{"first name and last name are both required"}
	case strings.ContainsAny(first+last, "\r\n"):
		return Record{}, &ValidationError{"names must be on one line"}
	case utf8.RuneCountInString(first) > MaxNameChars || utf8.RuneCountInString(last) > MaxNameChars:
		return Record{}, &ValidationError{fmt.Sprintf("names can be at most %d characters each", MaxNameChars)}
	case text == "":
		return Record{}, &ValidationError{"the entry text is required"}
	case utf8.RuneCountInString(text) > MaxTextChars:
		return Record{}, &ValidationError{fmt.Sprintf("the entry text can be at most %d characters", MaxTextChars)}
	case strings.ContainsAny(ip, "\r\n"):
		return Record{}, &ValidationError{"ip must be on one line"}
	case utf8.RuneCountInString(ip) > MaxIPChars:
		return Record{}, &ValidationError{fmt.Sprintf("ip can be at most %d characters", MaxIPChars)}
	case !utf8.ValidString(first + last + text + ip):
		return Record{}, &ValidationError{"text must be valid UTF-8"}
	}
	recs, err := s.appendRecords(ctx, false, []Record{{Kind: KindEntry, FirstName: first, LastName: last, Body: text, IP: ip}})
	if err != nil {
		return Record{}, err
	}
	return recs[0], nil
}

// appendRecords is the ONLY place that writes to the database. It fills in
// the entry number, timestamp, previous hash and hash for each draft record
// and inserts them all in one transaction (all or nothing).
//
// genesis must be true only when writing the title record.
func (s *Store) appendRecords(ctx context.Context, genesis bool, drafts []Record) ([]Record, error) {
	return s.appendWith(ctx, genesis, func(Record) ([]Record, error) { return drafts, nil })
}

// appendWith is like appendRecords, but the drafts are built by a function
// that is given the current head record, while the write lock is held.
// That lets exports record an exact entry range with no race.
func (s *Store) appendWith(ctx context.Context, genesis bool, build func(head Record) ([]Record, error)) ([]Record, error) {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback() // no-op after a successful Commit

	// Find the current end of the chain.
	head := Record{Seq: -1, Hash: GenesisHash}
	row := tx.QueryRowContext(ctx, `SELECT `+cols+` FROM records ORDER BY seq DESC LIMIT 1`)
	if h, err := scan(row); err == nil {
		head = h
	} else if !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	switch {
	case genesis && head.Seq >= 0:
		return nil, ErrTitleIsSet
	case !genesis && head.Seq < 0:
		return nil, ErrNoTitle
	}

	drafts, err := build(head)
	if err != nil {
		return nil, err
	}

	now := s.now().UTC().Format(time.RFC3339Nano)
	out := make([]Record, 0, len(drafts))
	seq, prev := head.Seq, head.Hash
	for _, r := range drafts {
		seq++
		r.Seq = seq
		r.CreatedAt = now
		r.PrevHash = prev
		r.Hash = ComputeHash(r)
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO records (seq, kind, created_at, first_name, last_name, ip, body, meta, prev_hash, hash)
			 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			r.Seq, r.Kind, r.CreatedAt, r.FirstName, r.LastName, r.IP, r.Body, r.Meta, r.PrevHash, r.Hash); err != nil {
			return nil, fmt.Errorf("saving record %d: %w", r.Seq, err)
		}
		prev = r.Hash
		out = append(out, r)
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	for _, fn := range s.onAppend {
		fn(out)
	}
	return out, nil
}

const cols = `seq, kind, created_at, first_name, last_name, ip, body, meta, prev_hash, hash`

type scanner interface{ Scan(dest ...any) error }

func scan(row scanner) (Record, error) {
	var r Record
	err := row.Scan(&r.Seq, &r.Kind, &r.CreatedAt, &r.FirstName, &r.LastName, &r.IP, &r.Body, &r.Meta, &r.PrevHash, &r.Hash)
	return r, err
}

// checkIPColumn rejects databases created before the ip column existed.
// Auto-adding the column would break existing fingerprints (the hash now includes ip).
func checkIPColumn(db *sql.DB) error {
	var n int
	if err := db.QueryRow(`SELECT COUNT(*) FROM pragma_table_info('records') WHERE name = 'ip'`).Scan(&n); err != nil {
		return err
	}
	if n == 0 {
		return fmt.Errorf("this database was created before CapLog stored IP addresses; start a new database (the record format changed before release)")
	}
	return nil
}

func (s *Store) query(ctx context.Context, q string, args ...any) ([]Record, error) {
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Record
	for rows.Next() {
		r, err := scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// normalizeNewlines turns Windows/Mac line endings into plain "\n" so the
// browser and server count characters the same way.
func normalizeNewlines(s string) string {
	return strings.ReplaceAll(strings.ReplaceAll(s, "\r\n", "\n"), "\r", "\n")
}
