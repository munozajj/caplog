// Package logbook holds the heart of CapLog: the permanent, hash-chained
// list of records and everything that reads or appends to it.
//
// Every record (the title, each entry, and each system note about an export
// or import) is linked to the one before it by a SHA-256 "fingerprint".
// Changing any old record changes its fingerprint, which no longer matches
// the "previous hash" stored in the next record, so the break is detectable.
package logbook

import (
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"strings"
)

// Record kinds. A record's kind is part of its fingerprint.
const (
	KindGenesis  = "genesis"  // record #0: holds the logbook title
	KindEntry    = "entry"    // a normal entry typed by a person
	KindExport   = "export"   // system note: the log was exported
	KindImport   = "import"   // system note: an import started / finished
	KindImported = "imported" // a record copied in from another CapLog export
)

// GenesisHash is the "previous hash" of record #0 (there is nothing before it).
var GenesisHash = strings.Repeat("0", 64)

// Record is one row of the logbook. Field names in JSON are what appear in
// a JSON export, so changing them would break older exports.
type Record struct {
	Seq       int64  `json:"seq"`        // entry number; 0 is the title record
	Kind      string `json:"kind"`       // one of the Kind* constants
	CreatedAt string `json:"created_at"` // UTC, RFC 3339 with nanoseconds
	FirstName string `json:"first_name"`
	LastName  string `json:"last_name"`
	IP        string `json:"ip"`        // address of the device that added this record; empty for system notes
	Body      string `json:"body"`      // entry text, or the title for record #0
	Meta      string `json:"meta"`      // extra JSON for system/imported records, else ""
	PrevHash  string `json:"prev_hash"` // fingerprint of the record before this one
	Hash      string `json:"hash"`      // fingerprint of this record
}

// ComputeHash returns the SHA-256 fingerprint of a record.
//
// The canonical encoding is deliberately simple so anyone can re-implement it:
// the text "caplog-v1\n" followed by every field, in a fixed order, written as
// "<byte length>:<value>\n". The length prefix means two different records can
// never produce the same byte string (e.g. moving text between fields).
// The stored Hash field itself is of course not included.
func ComputeHash(r Record) string {
	var b strings.Builder
	b.WriteString("caplog-v1\n")
	for _, f := range []string{
		strconv.FormatInt(r.Seq, 10),
		r.Kind,
		r.CreatedAt,
		r.FirstName,
		r.LastName,
		r.IP,
		r.Body,
		r.Meta,
		r.PrevHash,
	} {
		b.WriteString(strconv.Itoa(len(f)))
		b.WriteByte(':')
		b.WriteString(f)
		b.WriteByte('\n')
	}
	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])
}

// ChainProblem describes the first place a chain is broken.
type ChainProblem struct {
	Seq    int64  `json:"seq"`
	Reason string `json:"reason"`
}

// ChainChecker checks records one at a time, so even a very large log can
// be verified without loading it all into memory. Feed it records in order
// starting at #0; Add returns the first problem it finds.
type ChainChecker struct {
	next int64  // entry number we expect next
	prev string // fingerprint of the last good record
	Last Record // the last record that checked out OK
}

// NewChainChecker returns a checker ready for record #0.
func NewChainChecker() *ChainChecker { return &ChainChecker{prev: GenesisHash} }

// Add checks one record against the chain so far.
func (c *ChainChecker) Add(r Record) *ChainProblem {
	want := c.next
	switch {
	case r.Seq != want:
		return &ChainProblem{want, "entry number " + strconv.FormatInt(r.Seq, 10) + " found where " + strconv.FormatInt(want, 10) + " was expected (entries missing or added)"}
	case want == 0 && r.Kind != KindGenesis:
		return &ChainProblem{0, "record #0 is not the title (genesis) record"}
	case want > 0 && r.Kind == KindGenesis:
		return &ChainProblem{r.Seq, "a second title record appears"}
	case r.PrevHash != c.prev:
		return &ChainProblem{r.Seq, "previous-hash link does not match the record before it"}
	case ComputeHash(r) != r.Hash:
		return &ChainProblem{r.Seq, "content does not match its fingerprint (record was altered)"}
	}
	c.next++
	c.prev = r.Hash
	c.Last = r
	return nil
}

// Count is how many records have been checked successfully.
func (c *ChainChecker) Count() int64 { return c.next }

// CheckChain checks a whole in-memory list of records (used for imports).
func CheckChain(recs []Record) *ChainProblem {
	c := NewChainChecker()
	for _, r := range recs {
		if p := c.Add(r); p != nil {
			return p
		}
	}
	return nil
}
