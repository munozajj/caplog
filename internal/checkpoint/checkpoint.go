// Package checkpoint writes and checks signed checkpoints.
//
// A checkpoint says "at this time, the newest record was #N and its
// fingerprint was H", signed with an Ed25519 private key. The key lives in
// a file mounted as a Docker/Kubernetes secret - never in the database or
// the repo - and checkpoints go to a separate file (ideally a separate
// volume). Someone who edits the database and recomputes every fingerprint
// to hide it still cannot produce matching signed checkpoints without the key.
package checkpoint

import (
	"bufio"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// Checkpoint is one line of the checkpoint file (JSON Lines format).
type Checkpoint struct {
	Seq   int64  `json:"seq"`    // newest entry number at the time
	Hash  string `json:"hash"`   // fingerprint of that entry
	Time  string `json:"time"`   // when the checkpoint was made (UTC)
	KeyID string `json:"key_id"` // short ID of the public key that signed it
	Sig   string `json:"sig"`    // base64 Ed25519 signature over Message()
}

// Message is the exact text that gets signed.
func Message(seq int64, hash, t string) []byte {
	return []byte("caplog-checkpoint-v1\n" + strconv.FormatInt(seq, 10) + "\n" + hash + "\n" + t + "\n")
}

// KeyID is a short, stable identifier for a public key (first 16 hex chars
// of its SHA-256), handy for telling keys apart.
func KeyID(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return hex.EncodeToString(sum[:8])
}

// VerifySig reports whether the checkpoint was validly signed by pub.
func (c Checkpoint) VerifySig(pub ed25519.PublicKey) bool {
	sig, err := base64.StdEncoding.DecodeString(c.Sig)
	if err != nil {
		return false
	}
	return ed25519.Verify(pub, Message(c.Seq, c.Hash, c.Time), sig)
}

// ---- keys ----

// GenerateKey makes a new Ed25519 key pair as PEM files (PKCS#8 private key,
// PKIX public key - the same formats OpenSSL uses).
func GenerateKey() (privPEM, pubPEM []byte, err error) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, nil, err
	}
	pk, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return nil, nil, err
	}
	pb, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		return nil, nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: pk}),
		pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pb}), nil
}

// LoadPrivateKey reads a PEM Ed25519 private key file.
func LoadPrivateKey(path string) (ed25519.PrivateKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	blk, _ := pem.Decode(data)
	if blk == nil {
		return nil, fmt.Errorf("%s: not a PEM file", path)
	}
	k, err := x509.ParsePKCS8PrivateKey(blk.Bytes)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	ek, ok := k.(ed25519.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("%s: not an Ed25519 key", path)
	}
	return ek, nil
}

// LoadPublicKey reads a PEM Ed25519 public key file. A private key file is
// also accepted (the public half is taken from it).
func LoadPublicKey(path string) (ed25519.PublicKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	blk, _ := pem.Decode(data)
	if blk == nil {
		return nil, fmt.Errorf("%s: not a PEM file", path)
	}
	if blk.Type == "PRIVATE KEY" {
		k, err := LoadPrivateKey(path)
		if err != nil {
			return nil, err
		}
		return k.Public().(ed25519.PublicKey), nil
	}
	k, err := x509.ParsePKIXPublicKey(blk.Bytes)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	ek, ok := k.(ed25519.PublicKey)
	if !ok {
		return nil, fmt.Errorf("%s: not an Ed25519 key", path)
	}
	return ek, nil
}

// ---- reading ----

// ReadAll reads every checkpoint in the file. A missing file is not an error
// (it just means no checkpoints have been written yet).
func ReadAll(path string) ([]Checkpoint, error) {
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out []Checkpoint
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	line := 0
	for sc.Scan() {
		line++
		if len(sc.Bytes()) == 0 {
			continue
		}
		var c Checkpoint
		if err := json.Unmarshal(sc.Bytes(), &c); err != nil {
			return out, fmt.Errorf("checkpoint file line %d is damaged: %w", line, err)
		}
		out = append(out, c)
	}
	return out, sc.Err()
}

// ---- writing ----

// Writer appends signed checkpoints to a file.
type Writer struct {
	path string
	key  ed25519.PrivateKey
	mu   sync.Mutex
	last int64 // newest entry number already checkpointed (-1 = none)
}

// NewWriter prepares to append to path. It reads the existing file to learn
// which entry was checkpointed last, so restarts don't write duplicates.
func NewWriter(path string, key ed25519.PrivateKey) (*Writer, error) {
	w := &Writer{path: path, key: key, last: -1}
	cps, err := ReadAll(path)
	if err != nil {
		return nil, err
	}
	if len(cps) > 0 {
		w.last = cps[len(cps)-1].Seq
	}
	return w, nil
}

// Write signs and appends a checkpoint for (seq, hash) unless that entry
// was already checkpointed. The file is flushed to disk before returning.
func (w *Writer) Write(seq int64, hash string, now time.Time) (*Checkpoint, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if seq <= w.last {
		return nil, nil // nothing new since the last checkpoint
	}
	t := now.UTC().Format(time.RFC3339Nano)
	c := Checkpoint{
		Seq:   seq,
		Hash:  hash,
		Time:  t,
		KeyID: KeyID(w.key.Public().(ed25519.PublicKey)),
		Sig:   base64.StdEncoding.EncodeToString(ed25519.Sign(w.key, Message(seq, hash, t))),
	}
	line, _ := json.Marshal(c)
	f, err := os.OpenFile(w.path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o640)
	if err != nil {
		return nil, err
	}
	if _, err := f.Write(append(line, '\n')); err != nil {
		f.Close()
		return nil, err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return nil, err
	}
	if err := f.Close(); err != nil {
		return nil, err
	}
	w.last = seq
	return &c, nil
}

// ---- scheduling ----

// HeadFunc returns the newest record's entry number and fingerprint.
type HeadFunc func(ctx context.Context) (seq int64, hash string, err error)

// Scheduler writes a checkpoint every `Every` new records, every `Interval`
// (if anything changed), and once more on shutdown.
type Scheduler struct {
	W        *Writer
	Head     HeadFunc
	Every    int
	Interval time.Duration

	pending atomic.Int64
	kick    chan struct{}
	once    sync.Once
}

func (s *Scheduler) init() { s.once.Do(func() { s.kick = make(chan struct{}, 1) }) }

// Added tells the scheduler that n new records were committed.
func (s *Scheduler) Added(n int) {
	s.init()
	if s.Every > 0 && s.pending.Add(int64(n)) >= int64(s.Every) {
		select {
		case s.kick <- struct{}{}:
		default: // a checkpoint is already queued
		}
	}
}

// Run loops until ctx is cancelled.
func (s *Scheduler) Run(ctx context.Context) {
	s.init()
	interval := s.Interval
	if interval <= 0 {
		interval = 10 * time.Minute
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	s.checkpoint(context.Background()) // one at startup
	for {
		select {
		case <-ctx.Done():
			s.checkpoint(context.Background()) // one at shutdown
			return
		case <-t.C:
		case <-s.kick:
		}
		s.checkpoint(ctx)
	}
}

func (s *Scheduler) checkpoint(ctx context.Context) {
	s.pending.Store(0)
	seq, hash, err := s.Head(ctx)
	if err != nil {
		return // e.g. logbook not set up yet
	}
	c, err := s.W.Write(seq, hash, time.Now())
	if err != nil {
		slog.Error("could not write signed checkpoint", "err", err)
		return
	}
	if c != nil {
		slog.Info("signed checkpoint written", "seq", c.Seq)
	}
}
