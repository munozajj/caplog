package logbook

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/munozajj/caplog/internal/checkpoint"
)

var ctx = context.Background()

// newStore makes a fresh logbook in a temp folder, already set up with a title.
func newStore(t *testing.T, title string) *Store {
	t.Helper()
	s, err := Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	if title != "" {
		if _, err := s.Setup(ctx, title); err != nil {
			t.Fatal(err)
		}
	}
	return s
}

func addN(t *testing.T, s *Store, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		if _, err := s.AddEntry(ctx, "Jane", "Doe", "entry text "+strings.Repeat("x", i), ""); err != nil {
			t.Fatal(err)
		}
	}
}

func all(t *testing.T, s *Store) []Record {
	t.Helper()
	var out []Record
	if err := s.Each(ctx, -1, func(r Record) error { out = append(out, r); return nil }); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestHashChainCreateAndVerify(t *testing.T) {
	s := newStore(t, "Alpha Company Log")
	addN(t, s, 5)
	recs := all(t, s)
	if len(recs) != 6 {
		t.Fatalf("want 6 records, got %d", len(recs))
	}
	if recs[0].Kind != KindGenesis || recs[0].Body != "Alpha Company Log" || recs[0].PrevHash != GenesisHash {
		t.Fatalf("bad genesis record: %+v", recs[0])
	}
	for i := 1; i < len(recs); i++ {
		if recs[i].PrevHash != recs[i-1].Hash {
			t.Fatalf("record %d not linked to %d", i, i-1)
		}
	}
	if p := CheckChain(recs); p != nil {
		t.Fatalf("chain should be valid: %+v", p)
	}
	rep := s.Verify(ctx, "", nil)
	if !rep.OK || !rep.ChainOK || rep.Records != 6 {
		t.Fatalf("verify failed: %+v", rep)
	}
	// Timestamps are stored in UTC.
	if !strings.HasSuffix(recs[1].CreatedAt, "Z") {
		t.Fatalf("timestamp not UTC: %s", recs[1].CreatedAt)
	}
}

func TestTriggersBlockUpdateAndDelete(t *testing.T) {
	s := newStore(t, "Log")
	addN(t, s, 2)
	db := s.DB()
	if _, err := db.Exec(`UPDATE records SET body = 'changed' WHERE seq = 1`); err == nil || !strings.Contains(err.Error(), "cannot be modified") {
		t.Fatalf("UPDATE should be blocked, got %v", err)
	}
	if _, err := db.Exec(`DELETE FROM records WHERE seq = 2`); err == nil || !strings.Contains(err.Error(), "cannot be deleted") {
		t.Fatalf("DELETE should be blocked, got %v", err)
	}
	if _, err := db.Exec(`DELETE FROM records`); err == nil {
		t.Fatal("DELETE of everything should be blocked")
	}
	// Inserting out of sequence is blocked too.
	_, err := db.Exec(`INSERT INTO records VALUES (10, 'entry', 'x', 'a', 'b', '', 'c', '', 'bad', 'h1')`)
	if err == nil || !strings.Contains(err.Error(), "next in sequence") {
		t.Fatalf("out-of-sequence insert should be blocked, got %v", err)
	}
	if got := all(t, s); len(got) != 3 || got[1].Body != "entry text" {
		t.Fatalf("data changed: %+v", got)
	}
}

func TestTamperDetectedWhenTriggerDropped(t *testing.T) {
	s := newStore(t, "Log")
	addN(t, s, 4)
	db := s.DB()
	// Simulate an attacker with direct database access.
	if _, err := db.Exec(`DROP TRIGGER records_no_update`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE records SET body = 'forged' WHERE seq = 2`); err != nil {
		t.Fatal(err)
	}
	rep := s.Verify(ctx, "", nil)
	if rep.OK || rep.ChainBrk == nil || rep.ChainBrk.Seq != 2 {
		t.Fatalf("verify should fail at #2: %+v", rep)
	}
	if !strings.Contains(rep.Summary, "#2") {
		t.Fatalf("summary should name record #2: %s", rep.Summary)
	}
}

// An attacker who edits a record AND recomputes every later fingerprint gets
// a self-consistent chain, but the signed checkpoints catch it.
func TestRewrittenChainCaughtBySignedCheckpoint(t *testing.T) {
	s := newStore(t, "Log")
	addN(t, s, 5)
	cpFile, pub := writeCheckpoint(t, s)

	db := s.DB()
	db.Exec(`DROP TRIGGER records_no_update`)
	recs := all(t, s)
	prev := recs[2].PrevHash
	for i := 2; i < len(recs); i++ {
		r := recs[i]
		if i == 2 {
			r.Body = "forged"
		}
		r.PrevHash = prev
		r.Hash = ComputeHash(r)
		if _, err := db.Exec(`UPDATE records SET body=?, prev_hash=?, hash=? WHERE seq=?`, r.Body, r.PrevHash, r.Hash, r.Seq); err != nil {
			t.Fatal(err)
		}
		prev = r.Hash
	}
	rep := s.Verify(ctx, cpFile, pub)
	if !rep.ChainOK {
		t.Fatalf("rewritten chain should look internally consistent: %+v", rep.ChainBrk)
	}
	if rep.OK || !strings.Contains(strings.Join(rep.Problems, " "), "no longer matches its signed checkpoint") {
		t.Fatalf("checkpoint should catch the rewrite: %+v", rep)
	}
}

func TestDeletedEntriesCaughtByCheckpoint(t *testing.T) {
	s := newStore(t, "Log")
	addN(t, s, 5)
	cpFile, pub := writeCheckpoint(t, s)
	db := s.DB()
	db.Exec(`DROP TRIGGER records_no_delete`)
	if _, err := db.Exec(`DELETE FROM records WHERE seq >= 4`); err != nil {
		t.Fatal(err)
	}
	rep := s.Verify(ctx, cpFile, pub)
	if rep.OK || !strings.Contains(strings.Join(rep.Problems, " "), "only goes up to #3") {
		t.Fatalf("truncation should be caught: %+v", rep)
	}
}

func TestForgedCheckpointRejected(t *testing.T) {
	s := newStore(t, "Log")
	addN(t, s, 2)
	cpFile, _ := writeCheckpoint(t, s)
	otherPub, _, _ := ed25519.GenerateKey(rand.Reader)
	rep := s.Verify(ctx, cpFile, otherPub)
	if len(rep.Warnings) == 0 || rep.Checks.Matching != 0 {
		t.Fatalf("checkpoints from another key must not count: %+v", rep)
	}
}

func writeCheckpoint(t *testing.T, s *Store) (string, ed25519.PublicKey) {
	t.Helper()
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	path := filepath.Join(t.TempDir(), "cp.jsonl")
	w, err := checkpoint.NewWriter(path, priv)
	if err != nil {
		t.Fatal(err)
	}
	h, _ := s.Head(ctx)
	if _, err := w.Write(h.Seq, h.Hash, time.Now()); err != nil {
		t.Fatal(err)
	}
	if rep := s.Verify(ctx, path, pub); !rep.OK || rep.Checks.Matching != 1 {
		t.Fatalf("fresh checkpoint should verify: %+v", rep)
	}
	return path, pub
}

func TestTextLimitAndValidation(t *testing.T) {
	s := newStore(t, "Log")
	if _, err := s.AddEntry(ctx, "A", "B", strings.Repeat("a", 500), ""); err != nil {
		t.Fatalf("500 chars should be accepted: %v", err)
	}
	if _, err := s.AddEntry(ctx, "A", "B", strings.Repeat("a", 501), ""); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("501 chars should be rejected, got %v", err)
	}
	// Limit is in characters, not bytes: 500 emoji are fine.
	if _, err := s.AddEntry(ctx, "A", "B", strings.Repeat("😀", 500), ""); err != nil {
		t.Fatalf("500 emoji should be accepted: %v", err)
	}
	// Windows line endings count as one character.
	if _, err := s.AddEntry(ctx, "A", "B", strings.Repeat("a\r\n", 250), ""); err != nil {
		t.Fatalf("CRLF should be normalised: %v", err)
	}
	for _, c := range [][3]string{{"", "B", "x"}, {"A", " ", "x"}, {"A", "B", "   "}, {"A\nB", "C", "x"}} {
		if _, err := s.AddEntry(ctx, c[0], c[1], c[2], ""); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("%q should be rejected, got %v", c, err)
		}
	}
	h, _ := s.Head(ctx)
	if h.Seq != 3 {
		t.Fatalf("rejected entries must not use up numbers; head is #%d", h.Seq)
	}
}

func TestEntryBeforeSetupRejected(t *testing.T) {
	s := newStore(t, "")
	if _, err := s.AddEntry(ctx, "A", "B", "x", ""); !errors.Is(err, ErrNoTitle) {
		t.Fatalf("want ErrNoTitle, got %v", err)
	}
}

func TestConcurrentPostingIsGapless(t *testing.T) {
	s := newStore(t, "Busy Log")
	const workers, each = 20, 25
	var wg sync.WaitGroup
	errs := make(chan error, workers*each)
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < each; i++ {
				if _, err := s.AddEntry(ctx, "Pvt", "Ryan", "concurrent", ""); err != nil {
					errs <- err
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	recs := all(t, s)
	if len(recs) != workers*each+1 {
		t.Fatalf("want %d records, got %d", workers*each+1, len(recs))
	}
	for i, r := range recs {
		if r.Seq != int64(i) {
			t.Fatalf("gap or duplicate at %d (seq %d)", i, r.Seq)
		}
	}
	if rep := s.Verify(ctx, "", nil); !rep.OK {
		t.Fatalf("verify after concurrent posting: %+v", rep)
	}
}

func TestTitleCannotBeChanged(t *testing.T) {
	s := newStore(t, "Original Title")
	if _, err := s.Setup(ctx, "New Title"); !errors.Is(err, ErrTitleIsSet) {
		t.Fatalf("second setup should fail, got %v", err)
	}
	if _, err := s.DB().Exec(`UPDATE records SET body = 'New Title' WHERE seq = 0`); err == nil {
		t.Fatal("title UPDATE should be blocked")
	}
	if _, err := s.DB().Exec(`INSERT INTO records VALUES (1, 'genesis', 'x', '', '', '', 'Another', '', ?, 'h')`, all(t, s)[0].Hash); err == nil {
		t.Fatal("a second genesis record should be blocked")
	}
	if title, _ := s.Title(ctx); title != "Original Title" {
		t.Fatalf("title changed to %q", title)
	}
}

// exportJSON runs a full JSON export the same way the web server does.
func exportJSON(t *testing.T, s *Store) []byte {
	t.Helper()
	rec, err := s.RecordExport(ctx, "json")
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	if err := s.WriteJSONExport(ctx, &buf, rec); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestExportWritesSystemEntry(t *testing.T) {
	s := newStore(t, "Log")
	addN(t, s, 3)
	data := exportJSON(t, s)
	h, _ := s.Head(ctx)
	if h.Kind != KindExport || h.Seq != 4 || h.FirstName != "" || h.LastName != "" {
		t.Fatalf("export should append an anonymous system entry: %+v", h)
	}
	var m ExportMeta
	json.Unmarshal([]byte(h.Meta), &m)
	if m.Format != "json" || m.FromSeq != 0 || m.ToSeq != 4 {
		t.Fatalf("bad export meta: %+v", m)
	}
	f, err := ParseExport(data)
	if err != nil {
		t.Fatal(err)
	}
	if len(f.Records) != 5 || f.Records[4].Kind != KindExport {
		t.Fatalf("export should include itself as the last record, got %d records", len(f.Records))
	}
	if _, err := s.RecordExport(ctx, "pdf"); err != nil {
		t.Fatal(err)
	}
}

func TestExportImportRoundTrip(t *testing.T) {
	a := newStore(t, "Bravo Log")
	a.AddEntry(ctx, "John", "Smith", "first line\nsecond line", "203.0.113.10")
	a.AddEntry(ctx, "Ana", "García", "ünïcödé ✓", "2001:db8::1")
	data := exportJSON(t, a)
	src, _ := ParseExport(data)

	b := newStore(t, "Charlie Log")
	b.AddEntry(ctx, "Local", "Person", "already here", "192.0.2.1")
	before := all(t, b)

	res, err := b.Import(ctx, data)
	if err != nil {
		t.Fatal(err)
	}
	if res.Imported != 3 || res.SourceTitle != "Bravo Log" || res.FirstSeq != 2 || res.LastSeq != 6 {
		t.Fatalf("unexpected result: %+v", res)
	}
	after := all(t, b)
	// Existing records are untouched.
	for i := range before {
		if before[i] != after[i] {
			t.Fatalf("existing record %d changed", i)
		}
	}
	// start note, 3 imported records, end note
	if after[2].Kind != KindImport || after[6].Kind != KindImport {
		t.Fatalf("import notes missing: %s / %s", after[2].Kind, after[6].Kind)
	}
	var start, end ImportMeta
	json.Unmarshal([]byte(after[2].Meta), &start)
	json.Unmarshal([]byte(after[6].Meta), &end)
	if start.Phase != "start" || end.Phase != "end" || start.Count != 3 || start.SourceTitle != "Bravo Log" {
		t.Fatalf("bad import meta: %+v %+v", start, end)
	}
	for i := 0; i < 3; i++ {
		got, orig := after[3+i], src.Records[1+i]
		var m ImportedMeta
		json.Unmarshal([]byte(got.Meta), &m)
		if got.Kind != KindImported || got.FirstName != orig.FirstName || got.LastName != orig.LastName || got.IP != orig.IP || got.Body != orig.Body ||
			m.SourceSeq != orig.Seq || m.SourceCreatedAt != orig.CreatedAt || m.SourceTitle != "Bravo Log" || m.SourceHash != orig.Hash || m.SourceKind != orig.Kind {
			t.Fatalf("imported record %d wrong: %+v meta %+v (orig %+v)", i, got, m, orig)
		}
	}
	if rep := b.Verify(ctx, "", nil); !rep.OK {
		t.Fatalf("verify after import: %+v", rep)
	}
	// The re-exported log (with imported records) can itself be imported.
	c := newStore(t, "Delta Log")
	if _, err := c.Import(ctx, exportJSON(t, b)); err != nil {
		t.Fatalf("re-import failed: %v", err)
	}
}

func TestImportRejectsBrokenChain(t *testing.T) {
	a := newStore(t, "Source")
	addN(t, a, 3)
	data := exportJSON(t, a)

	var f ExportFile
	json.Unmarshal(data, &f)
	f.Records[2].Body = "tampered"
	bad, _ := json.Marshal(f)

	b := newStore(t, "Target")
	headBefore, _ := b.Head(ctx)
	_, err := b.Import(ctx, bad)
	if !errors.Is(err, ErrInvalidInput) || !strings.Contains(err.Error(), "broken at record #2") {
		t.Fatalf("want broken-chain error at #2, got %v", err)
	}
	// Also reject: removed record, garbage, wrong format.
	f.Records[2] = f.Records[3]
	gap, _ := json.Marshal(f)
	for _, d := range [][]byte{gap, []byte("not json"), []byte(`{"format":"other","version":1}`)} {
		if _, err := b.Import(ctx, d); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("should reject, got %v", err)
		}
	}
	if h, _ := b.Head(ctx); h != headBefore {
		t.Fatal("a rejected import must not write anything")
	}
}

func TestEntryIPIsStoredAndInHash(t *testing.T) {
	s := newStore(t, "Log")
	rec, err := s.AddEntry(ctx, "Jane", "Doe", "posted", "203.0.113.50")
	if err != nil {
		t.Fatal(err)
	}
	if rec.IP != "203.0.113.50" {
		t.Fatalf("stored ip %q", rec.IP)
	}
	if ComputeHash(rec) != rec.Hash {
		t.Fatal("hash should include the stored ip")
	}
	// Changing the ip without recomputing the hash is detected.
	tampered := rec
	tampered.IP = "192.0.2.1"
	if ComputeHash(tampered) == rec.Hash {
		t.Fatal("hash should change when ip changes")
	}
	// Title / export system records leave ip empty.
	recs := all(t, s)
	if recs[0].Kind != KindGenesis || recs[0].IP != "" {
		t.Fatalf("title record should have empty ip: %+v", recs[0])
	}
	if _, err := s.RecordExport(ctx, "json"); err != nil {
		t.Fatal(err)
	}
	h, _ := s.Head(ctx)
	if h.Kind != KindExport || h.IP != "" {
		t.Fatalf("export note should have empty ip: %+v", h)
	}
}
