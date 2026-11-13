package logbook

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"unicode/utf8"
)

// ExportFormat identifies a CapLog JSON export file.
const (
	ExportFormat  = "caplog-export"
	ExportVersion = 1
)

// ExportFile is the layout of a JSON export. It contains every record with
// its fingerprints, so the file can be verified on its own (see VerifyExport).
type ExportFile struct {
	Format     string   `json:"format"`
	Version    int      `json:"version"`
	Title      string   `json:"title"`
	ExportedAt string   `json:"exported_at"`
	Records    []Record `json:"records"`
}

// ExportMeta is stored (as JSON) in the Meta field of an "export" record.
type ExportMeta struct {
	Format  string `json:"format"`   // "json" or "pdf"
	FromSeq int64  `json:"from_seq"` // first record included
	ToSeq   int64  `json:"to_seq"`   // last record included (this export note itself)
}

// ImportMeta is stored in the Meta field of an "import" system record.
type ImportMeta struct {
	Phase          string `json:"phase"` // "start" or "end"
	SourceTitle    string `json:"source_title"`
	SourceLastHash string `json:"source_last_hash"` // fingerprint of the last record in the file
	Count          int    `json:"count"`            // how many records were imported
}

// ImportedMeta is stored in the Meta field of every "imported" record and
// keeps the original details from the other logbook.
type ImportedMeta struct {
	SourceTitle     string `json:"source_title"`
	SourceSeq       int64  `json:"source_seq"`
	SourceKind      string `json:"source_kind"`
	SourceCreatedAt string `json:"source_created_at"`
	SourceHash      string `json:"source_hash"`
	SourceMeta      string `json:"source_meta,omitempty"`
}

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err) // only happens with programming errors
	}
	return string(b)
}

// RecordExport permanently appends a system record saying an export of the
// given format ("json" or "pdf") is happening, and returns it. The export
// then covers records #0 through this one.
func (s *Store) RecordExport(ctx context.Context, format string) (Record, error) {
	recs, err := s.appendWith(ctx, false, func(head Record) ([]Record, error) {
		to := head.Seq + 1 // this export note will be the last record included
		return []Record{{
			Kind: KindExport,
			Body: fmt.Sprintf("Logbook exported as %s (records #0-#%d).", map[string]string{"json": "JSON", "pdf": "PDF"}[format], to),
			Meta: mustJSON(ExportMeta{Format: format, FromSeq: 0, ToSeq: to}),
		}}, nil
	})
	if err != nil {
		return Record{}, err
	}
	return recs[0], nil
}

// WriteJSONExport streams records #0..upTo to w as an ExportFile.
// It is written by hand (rather than building one big slice) so huge logs
// don't need to fit in memory.
func (s *Store) WriteJSONExport(ctx context.Context, w io.Writer, upTo Record) error {
	title, err := s.Title(ctx)
	if err != nil {
		return err
	}
	hdr, _ := json.Marshal(struct {
		Format     string `json:"format"`
		Version    int    `json:"version"`
		Title      string `json:"title"`
		ExportedAt string `json:"exported_at"`
	}{ExportFormat, ExportVersion, title, upTo.CreatedAt})
	// Turn {"...":...} into {"...":..., "records":[ ... ]}
	if _, err := w.Write(append(hdr[:len(hdr)-1], []byte(",\n\"records\":[\n")...)); err != nil {
		return err
	}
	first := true
	err = s.Each(ctx, upTo.Seq, func(r Record) error {
		b, err := json.Marshal(r)
		if err != nil {
			return err
		}
		if !first {
			b = append([]byte(",\n"), b...)
		}
		first = false
		_, err = w.Write(b)
		return err
	})
	if err != nil {
		return err
	}
	_, err = io.WriteString(w, "\n]}\n")
	return err
}

// ParseExport reads a JSON export and checks its format and hash chain.
// It returns an error describing exactly what is wrong if anything is.
func ParseExport(data []byte) (*ExportFile, error) {
	var f ExportFile
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&f); err != nil {
		return nil, &ValidationError{"not a valid CapLog JSON export: " + err.Error()}
	}
	if f.Format != ExportFormat || f.Version != ExportVersion {
		return nil, &ValidationError{fmt.Sprintf("not a CapLog export (format %q version %d)", f.Format, f.Version)}
	}
	if len(f.Records) == 0 {
		return nil, &ValidationError{"the export contains no records"}
	}
	if p := CheckChain(f.Records); p != nil {
		return nil, &ValidationError{fmt.Sprintf("hash chain is broken at record #%d: %s", p.Seq, p.Reason)}
	}
	if f.Records[0].Body != f.Title {
		return nil, &ValidationError{"the title in the file does not match its title record"}
	}
	for _, r := range f.Records {
		if !utf8.ValidString(r.FirstName+r.LastName+r.Body+r.Meta) ||
			utf8.RuneCountInString(r.FirstName) > MaxNameChars ||
			utf8.RuneCountInString(r.LastName) > MaxNameChars ||
			utf8.RuneCountInString(r.Body) > 2000 || len(r.Meta) > 64*1024 {
			return nil, &ValidationError{fmt.Sprintf("record #%d has fields that are too long or not valid text", r.Seq)}
		}
		if (r.Kind == KindEntry || (r.Kind == KindImported && r.FirstName != "")) && utf8.RuneCountInString(r.Body) > MaxTextChars {
			return nil, &ValidationError{fmt.Sprintf("record #%d is longer than %d characters", r.Seq, MaxTextChars)}
		}
	}
	return &f, nil
}

// ImportResult summarises a successful import.
type ImportResult struct {
	SourceTitle string `json:"source_title"`
	Imported    int    `json:"imported"`
	FirstSeq    int64  `json:"first_seq"` // the "import started" note
	LastSeq     int64  `json:"last_seq"`  // the "import finished" note
}

// Import verifies a JSON export and, only if its chain is intact, appends
// its records to the end of this log. Nothing already in the log is touched.
//
// What gets written, all in one transaction:
//  1. an "import" note (phase start) with the source title and count,
//  2. one "imported" record per source record (the source's title record is
//     not copied; its title is kept in every record's meta instead),
//  3. an "import" note (phase end).
func (s *Store) Import(ctx context.Context, data []byte) (ImportResult, error) {
	f, err := ParseExport(data)
	if err != nil {
		return ImportResult{}, err
	}
	src := f.Records[1:] // skip the source title record
	if len(src) == 0 {
		return ImportResult{}, &ValidationError{"the export has a title but no entries to import"}
	}
	last := f.Records[len(f.Records)-1]
	im := ImportMeta{SourceTitle: f.Title, SourceLastHash: last.Hash, Count: len(src)}

	drafts := make([]Record, 0, len(src)+2)
	im.Phase = "start"
	drafts = append(drafts, Record{
		Kind: KindImport,
		Body: fmt.Sprintf("Import started: %d records from logbook %q.", len(src), f.Title),
		Meta: mustJSON(im),
	})
	for _, r := range src {
		drafts = append(drafts, Record{
			Kind:      KindImported,
			FirstName: r.FirstName,
			LastName:  r.LastName,
			Body:      r.Body,
			Meta: mustJSON(ImportedMeta{
				SourceTitle:     f.Title,
				SourceSeq:       r.Seq,
				SourceKind:      r.Kind,
				SourceCreatedAt: r.CreatedAt,
				SourceHash:      r.Hash,
				SourceMeta:      r.Meta,
			}),
		})
	}
	im.Phase = "end"
	drafts = append(drafts, Record{
		Kind: KindImport,
		Body: fmt.Sprintf("Import finished: %d records from logbook %q.", len(src), f.Title),
		Meta: mustJSON(im),
	})

	recs, err := s.appendRecords(ctx, false, drafts)
	if err != nil {
		return ImportResult{}, err
	}
	return ImportResult{
		SourceTitle: f.Title,
		Imported:    len(src),
		FirstSeq:    recs[0].Seq,
		LastSeq:     recs[len(recs)-1].Seq,
	}, nil
}
