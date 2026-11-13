// CapLog: a permanent, shared, green-book style logbook.
//
// Commands:
//
//	caplog serve        run the web app (the default)
//	caplog keygen       create an Ed25519 signing key pair for checkpoints
//	caplog verify       check the whole log (database or a JSON export file)
//	caplog healthcheck  used by Docker/Kubernetes to see if the app is up
//	caplog version
//
// Settings come from flags or CAPLOG_* environment variables (see README).
package main

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/munozajj/caplog/internal/checkpoint"
	"github.com/munozajj/caplog/internal/logbook"
	"github.com/munozajj/caplog/internal/web"
)

// version is set at build time with -ldflags "-X main.version=v1.2.3".
var version = "dev"

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, nil)))
	cmd, args := "serve", os.Args[1:]
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		cmd, args = args[0], args[1:]
	}
	var err error
	switch cmd {
	case "serve":
		err = serve(args)
	case "keygen":
		err = keygen(args)
	case "verify":
		err = verify(args)
	case "healthcheck":
		err = healthcheck(args)
	case "version":
		fmt.Println("caplog", version)
	default:
		err = fmt.Errorf("unknown command %q (use serve, keygen, verify, healthcheck or version)", cmd)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "caplog:", err)
		os.Exit(1)
	}
}

// env returns an environment variable or a default.
func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	return def
}

// Shared settings for serve and verify.
type settings struct {
	listen, db, key, pubkey, checkpoints string
}

func commonFlags(fs *flag.FlagSet) *settings {
	s := &settings{}
	fs.StringVar(&s.listen, "listen", env("CAPLOG_LISTEN", ":8080"), "address:port to listen on (CAPLOG_LISTEN)")
	fs.StringVar(&s.db, "db", env("CAPLOG_DB", "/data/caplog.db"), "SQLite database file (CAPLOG_DB)")
	fs.StringVar(&s.key, "signing-key", env("CAPLOG_SIGNING_KEY", ""), "Ed25519 private key PEM file for checkpoints (CAPLOG_SIGNING_KEY)")
	fs.StringVar(&s.pubkey, "public-key", env("CAPLOG_PUBLIC_KEY", ""), "public key PEM, used by verify when no private key is available (CAPLOG_PUBLIC_KEY)")
	fs.StringVar(&s.checkpoints, "checkpoints", env("CAPLOG_CHECKPOINTS", "/checkpoints/checkpoints.jsonl"), "signed checkpoint file (CAPLOG_CHECKPOINTS)")
	return s
}

// ---- serve ----

func serve(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	s := commonFlags(fs)
	every := fs.Int("checkpoint-every", envInt("CAPLOG_CHECKPOINT_EVERY", 25), "write a checkpoint after this many new records (CAPLOG_CHECKPOINT_EVERY)")
	interval := fs.Duration("checkpoint-interval", envDur("CAPLOG_CHECKPOINT_INTERVAL", 10*time.Minute), "also write a checkpoint this often if anything changed (CAPLOG_CHECKPOINT_INTERVAL)")
	maxImport := fs.Int("max-import-mb", envInt("CAPLOG_MAX_IMPORT_MB", 20), "largest import file accepted, in MB (CAPLOG_MAX_IMPORT_MB)")
	trusted := fs.String("trusted-proxies", env("CAPLOG_TRUSTED_PROXIES", ""), "comma-separated IPs or CIDRs of reverse proxies; if the client is one of these, X-Forwarded-For / X-Real-IP is used (CAPLOG_TRUSTED_PROXIES)")
	fs.Parse(args)

	trustedNets, err := web.ParseTrustedProxies(*trusted)
	if err != nil {
		return err
	}

	store, err := logbook.Open(s.db)
	if err != nil {
		return err
	}
	defer store.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Signing key: optional, but strongly recommended.
	var pub ed25519.PublicKey
	cpDone := make(chan struct{})
	if s.key == "" {
		slog.Warn("NO SIGNING KEY CONFIGURED: signed checkpoints are disabled, so a fully rewritten log could go undetected. " +
			"Create one with 'caplog keygen' and set CAPLOG_SIGNING_KEY (see README).")
		close(cpDone)
	} else {
		priv, err := checkpoint.LoadPrivateKey(s.key)
		if err != nil {
			return fmt.Errorf("loading signing key: %w", err)
		}
		pub = priv.Public().(ed25519.PublicKey)
		w, err := checkpoint.NewWriter(s.checkpoints, priv)
		if err != nil {
			return fmt.Errorf("opening checkpoint file: %w", err)
		}
		sched := &checkpoint.Scheduler{
			W: w, Every: *every, Interval: *interval,
			Head: func(ctx context.Context) (int64, string, error) {
				h, err := store.Head(ctx)
				return h.Seq, h.Hash, err
			},
		}
		store.OnAppend(func(r []logbook.Record) { sched.Added(len(r)) })
		go func() { sched.Run(ctx); close(cpDone) }()
		slog.Info("signed checkpoints enabled", "file", s.checkpoints, "key_id", checkpoint.KeyID(pub))
	}

	srv := &http.Server{
		Addr:              s.listen,
		Handler:           web.New(store, web.Config{CheckpointPath: s.checkpoints, PublicKey: pub, MaxImportBytes: int64(*maxImport) << 20, TrustedProxies: trustedNets}),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       2 * time.Minute, // generous, for large imports
		IdleTimeout:       2 * time.Minute,
		// No WriteTimeout: live-update streams stay open indefinitely.
		ErrorLog: nil,
	}
	errCh := make(chan error, 1)
	go func() {
		slog.Info("CapLog listening", "addr", s.listen, "db", s.db, "version", version)
		errCh <- srv.ListenAndServe()
	}()

	select {
	case err := <-errCh:
		stop()
		<-cpDone
		return err
	case <-ctx.Done():
	}
	slog.Info("shutting down")
	shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	srv.Shutdown(shutCtx) // live streams are cut; browsers reconnect when it's back
	<-cpDone              // wait for the final checkpoint
	return nil
}

// ---- keygen ----

func keygen(args []string) error {
	fs := flag.NewFlagSet("keygen", flag.ExitOnError)
	out := fs.String("out", "caplog-signing.key", "where to write the private key; the public key goes to <out>.pub")
	fs.Parse(args)
	priv, pub, err := checkpoint.GenerateKey()
	if err != nil {
		return err
	}
	// O_EXCL: never overwrite an existing key by accident.
	if err := writeNew(*out, priv, 0o600); err != nil {
		return err
	}
	if err := writeNew(*out+".pub", pub, 0o644); err != nil {
		return err
	}
	pk, _ := checkpoint.LoadPublicKey(*out + ".pub")
	fmt.Printf("Private key: %s  (keep secret; mount as CAPLOG_SIGNING_KEY)\nPublic key:  %s.pub  (copy somewhere off this host)\nKey ID:      %s\n", *out, *out, checkpoint.KeyID(pk))
	return nil
}

func writeNew(path string, data []byte, mode os.FileMode) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// ---- verify ----

func verify(args []string) error {
	fs := flag.NewFlagSet("verify", flag.ExitOnError)
	s := commonFlags(fs)
	file := fs.String("file", "", "verify a JSON export file instead of the database")
	asJSON := fs.Bool("json", false, "print the full report as JSON")
	fs.Parse(args)

	if *file != "" {
		data, err := os.ReadFile(*file)
		if err != nil {
			return err
		}
		f, err := logbook.ParseExport(data)
		if err != nil {
			fmt.Println("FAILED:", err)
			os.Exit(2)
		}
		last := f.Records[len(f.Records)-1]
		fmt.Printf("OK: export of %q has an intact chain of %d records (#0-#%d).\nLast fingerprint: %s\n", f.Title, len(f.Records), last.Seq, last.Hash)
		fmt.Println("Compare the last fingerprint with a signed checkpoint or another copy to prove it wasn't rewritten.")
		return nil
	}

	store, err := logbook.OpenExisting(s.db)
	if err != nil {
		return err
	}
	defer store.Close()

	var pub ed25519.PublicKey
	switch {
	case s.pubkey != "":
		pub, err = checkpoint.LoadPublicKey(s.pubkey)
	case s.key != "":
		pub, err = checkpoint.LoadPublicKey(s.key)
	}
	if err != nil {
		return err
	}
	rep := store.Verify(context.Background(), s.checkpoints, pub)
	if *asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		enc.Encode(rep)
	} else {
		fmt.Println(rep.Summary)
		fmt.Printf("Title: %q   newest record: #%d   checkpoints: %d of %d match (signatures checked: %v)\n",
			rep.Title, rep.HeadSeq, rep.Checks.Matching, rep.Checks.Total, rep.Checks.SignaturesChecked)
		for _, p := range rep.Problems {
			fmt.Println("PROBLEM:", p)
		}
		for _, w := range rep.Warnings {
			fmt.Println("warning:", w)
		}
	}
	if !rep.OK {
		os.Exit(2)
	}
	return nil
}

// ---- healthcheck ----

// healthcheck calls /healthz on the local server. The container image has no
// shell or curl, so Docker's HEALTHCHECK runs this instead.
func healthcheck(args []string) error {
	fs := flag.NewFlagSet("healthcheck", flag.ExitOnError)
	s := commonFlags(fs)
	fs.Parse(args)
	_, port, err := net.SplitHostPort(s.listen)
	if err != nil {
		return err
	}
	c := http.Client{Timeout: 3 * time.Second}
	res, err := c.Get("http://127.0.0.1:" + port + "/healthz")
	if err != nil {
		return err
	}
	res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return errors.New("unhealthy: " + res.Status)
	}
	return nil
}

func envInt(key string, def int) int {
	if v, err := strconv.Atoi(os.Getenv(key)); err == nil {
		return v
	}
	return def
}

func envDur(key string, def time.Duration) time.Duration {
	if v, err := time.ParseDuration(os.Getenv(key)); err == nil {
		return v
	}
	return def
}
