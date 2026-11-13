package logbook

import (
	"context"
	"crypto/ed25519"
	"fmt"

	"github.com/munozajj/caplog/internal/checkpoint"
)

// Report is the result of a full verification.
type Report struct {
	OK       bool          `json:"ok"`
	Title    string        `json:"title"`
	Records  int64         `json:"records"`   // how many records passed the chain check
	HeadSeq  int64         `json:"head_seq"`  // newest entry number in the database
	HeadHash string        `json:"head_hash"` // its fingerprint
	ChainOK  bool          `json:"chain_ok"`
	ChainBrk *ChainProblem `json:"chain_problem,omitempty"`
	Checks   CPReport      `json:"checkpoints"`
	Problems []string      `json:"problems"` // anything that means "tampering likely"
	Warnings []string      `json:"warnings"` // things that weaken the guarantee
	Summary  string        `json:"summary"`
}

// CPReport covers the signed checkpoints part of a verification.
type CPReport struct {
	File              string `json:"file"`
	Total             int    `json:"total"`
	Matching          int    `json:"matching"` // signature valid AND hash matches the database
	SignaturesChecked bool   `json:"signatures_checked"`
	LatestSeq         int64  `json:"latest_seq"`
	LatestTime        string `json:"latest_time"`
}

// Verify recomputes the entire hash chain from the database and compares
// it with the signed checkpoint file.
//
// pub may be nil (no key configured); then checkpoint signatures can't be
// checked, only their hashes, and a warning says so.
func (s *Store) Verify(ctx context.Context, cpPath string, pub ed25519.PublicKey) Report {
	rep := Report{HeadSeq: -1, Problems: []string{}, Warnings: []string{}}
	rep.Checks.File = cpPath
	rep.Checks.LatestSeq = -1

	// 1. Load checkpoints first so we know which fingerprints to remember.
	var cps []checkpoint.Checkpoint
	if cpPath != "" {
		var err error
		cps, err = checkpoint.ReadAll(cpPath)
		if err != nil {
			rep.Problems = append(rep.Problems, err.Error())
		}
	}
	want := map[int64]string{} // entry number -> fingerprint stored in the database
	for _, c := range cps {
		want[c.Seq] = ""
	}

	// 2. Walk every record and recompute the chain.
	cc := NewChainChecker()
	err := s.Each(ctx, -1, func(r Record) error {
		if r.Seq == 0 {
			rep.Title = r.Body
		}
		if _, ok := want[r.Seq]; ok {
			want[r.Seq] = r.Hash
		}
		rep.HeadSeq, rep.HeadHash = r.Seq, r.Hash
		if rep.ChainBrk == nil {
			rep.ChainBrk = cc.Add(r)
		}
		return nil
	})
	if err != nil {
		rep.Problems = append(rep.Problems, "could not read the database: "+err.Error())
	}
	rep.Records = cc.Count()
	rep.ChainOK = rep.ChainBrk == nil && err == nil
	if rep.ChainBrk != nil {
		rep.Problems = append(rep.Problems, fmt.Sprintf("hash chain broken at record #%d: %s", rep.ChainBrk.Seq, rep.ChainBrk.Reason))
	}

	// 3. Check each checkpoint: valid signature, and the database still has
	//    exactly that fingerprint at that entry number.
	rep.Checks.Total = len(cps)
	rep.Checks.SignaturesChecked = pub != nil
	var myID string
	if pub != nil {
		myID = checkpoint.KeyID(pub)
	}
	otherKey := 0
	for _, c := range cps {
		if c.Seq > rep.Checks.LatestSeq {
			rep.Checks.LatestSeq, rep.Checks.LatestTime = c.Seq, c.Time
		}
		if pub != nil {
			if c.KeyID != myID {
				otherKey++
				continue
			}
			if !c.VerifySig(pub) {
				rep.Problems = append(rep.Problems, fmt.Sprintf("checkpoint for record #%d (%s) has an invalid signature (checkpoint file was edited)", c.Seq, c.Time))
				continue
			}
		}
		got := want[c.Seq]
		switch {
		case got == "":
			rep.Problems = append(rep.Problems, fmt.Sprintf("checkpoint from %s covers record #%d, but the database only goes up to #%d (records were removed or the database was rolled back)", c.Time, c.Seq, rep.HeadSeq))
		case got != c.Hash:
			rep.Problems = append(rep.Problems, fmt.Sprintf("record #%d no longer matches its signed checkpoint from %s (the log was rewritten)", c.Seq, c.Time))
		default:
			rep.Checks.Matching++
		}
	}
	if otherKey > 0 {
		rep.Warnings = append(rep.Warnings, fmt.Sprintf("%d checkpoint(s) were signed by a different key and could not be checked", otherKey))
	}

	// 4. Warnings about weaker setups.
	if pub == nil {
		rep.Warnings = append(rep.Warnings, "no signing key or public key is configured, so checkpoint signatures were NOT checked; set CAPLOG_SIGNING_KEY (see README)")
	}
	if cpPath == "" {
		rep.Warnings = append(rep.Warnings, "no checkpoint file configured")
	} else if len(cps) == 0 && rep.HeadSeq >= 0 {
		rep.Warnings = append(rep.Warnings, "no signed checkpoints exist yet, so a fully rewritten log could not be detected")
	}
	if rep.HeadSeq < 0 {
		rep.Warnings = append(rep.Warnings, "the logbook is empty (not set up yet)")
	}

	rep.OK = len(rep.Problems) == 0
	if rep.OK {
		rep.Summary = fmt.Sprintf("OK: %d records verified; %d of %d signed checkpoints match.", rep.Records, rep.Checks.Matching, rep.Checks.Total)
	} else {
		rep.Summary = "FAILED: " + rep.Problems[0]
	}
	return rep
}
