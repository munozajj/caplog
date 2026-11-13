package checkpoint

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestKeygenSignAndVerify(t *testing.T) {
	dir := t.TempDir()
	privPEM, pubPEM, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(dir, "k"), privPEM, 0o600)
	os.WriteFile(filepath.Join(dir, "k.pub"), pubPEM, 0o644)
	priv, err := LoadPrivateKey(filepath.Join(dir, "k"))
	if err != nil {
		t.Fatal(err)
	}
	pub, err := LoadPublicKey(filepath.Join(dir, "k.pub"))
	if err != nil {
		t.Fatal(err)
	}

	cp := filepath.Join(dir, "cp.jsonl")
	w, _ := NewWriter(cp, priv)
	if c, err := w.Write(5, strings.Repeat("a", 64), time.Now()); err != nil || c == nil {
		t.Fatalf("write: %v", err)
	}
	if c, _ := w.Write(5, strings.Repeat("a", 64), time.Now()); c != nil {
		t.Fatal("same entry should not be checkpointed twice")
	}
	w.Write(9, strings.Repeat("b", 64), time.Now())

	// A restarted writer remembers where it left off.
	w2, _ := NewWriter(cp, priv)
	if c, _ := w2.Write(9, strings.Repeat("b", 64), time.Now()); c != nil {
		t.Fatal("restart should not duplicate the last checkpoint")
	}

	cps, err := ReadAll(cp)
	if err != nil || len(cps) != 2 {
		t.Fatalf("want 2 checkpoints, got %d (%v)", len(cps), err)
	}
	for _, c := range cps {
		if !c.VerifySig(pub) {
			t.Fatal("signature should verify")
		}
	}
	// Editing any field breaks the signature.
	forged := cps[0]
	forged.Hash = strings.Repeat("c", 64)
	if forged.VerifySig(pub) {
		t.Fatal("edited checkpoint must not verify")
	}
}

func TestMissingCheckpointFileIsEmpty(t *testing.T) {
	cps, err := ReadAll(filepath.Join(t.TempDir(), "none.jsonl"))
	if err != nil || cps != nil {
		t.Fatalf("got %v %v", cps, err)
	}
}
