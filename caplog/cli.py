"""Commands: serve, keygen, verify, healthcheck, version, migrate."""

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

from caplog import __version__
from caplog.checkpoint import Scheduler, Writer, load_private_key, load_public_key
from caplog.errors import ValidationError
from caplog.net import parse_trusted_proxies
from caplog.store import Store, migrate, parse_export
from caplog.timeutil import parse_duration
from caplog.web import Config, create_app, static_dir

log = logging.getLogger("caplog")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None else value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_duration(name: str, default: str) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return parse_duration(default)
    try:
        return parse_duration(raw)
    except ValueError:
        return parse_duration(default)


def _split_listen(listen: str) -> tuple[str, int]:
    if listen.startswith("["):
        host, _, port = listen[1:].rpartition("]:")
        return host, int(port)
    if listen.startswith(":"):
        return "0.0.0.0", int(listen[1:])
    host, port = listen.rsplit(":", 1)
    return host or "0.0.0.0", int(port)


def _write_new(path: str, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to overwrite {path}") from exc
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, mode)


def _cmd_keygen(args: argparse.Namespace) -> None:
    from caplog.checkpoint import generate_key, key_id

    private, public = generate_key()
    _write_new(args.out, private, 0o600)
    _write_new(args.out + ".pub", public, 0o644)
    loaded = load_public_key(args.out + ".pub")
    print(
        f"Private key: {args.out}  (keep secret; mount as CAPLOG_SIGNING_KEY)\n"
        f"Public key:  {args.out}.pub  (copy somewhere off this host)\n"
        f"Key ID:      {key_id(loaded)}"
    )


def _print_report(report, as_json: bool) -> None:
    if as_json:
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    print(report.summary)
    checked = report.checkpoints["signatures_checked"]
    print(
        f'Title: {json.dumps(report.title)}   newest record: #{report.head_seq}   '
        f"checkpoints: {report.checkpoints['matching']} of {report.checkpoints['total']} "
        f"match (signatures checked: {str(checked).lower()})"
    )
    for problem in report.problems:
        print("PROBLEM:", problem)
    for warning in report.warnings:
        print("warning:", warning)


def _cmd_verify(args: argparse.Namespace) -> None:
    if args.file:
        try:
            parsed = parse_export(Path(args.file).read_bytes())
        except ValidationError as exc:
            print("FAILED:", exc)
            raise SystemExit(2) from exc
        last = parsed.records[-1]
        print(
            f"OK: export of {json.dumps(parsed.title)} has an intact chain of "
            f"{len(parsed.records)} records (#0-#{last.seq})."
        )
        print("Last fingerprint:", last.hash)
        print(
            "Compare the last fingerprint with a signed checkpoint or another copy "
            "to prove it wasn't rewritten."
        )
        return
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")
    public = None
    if args.public_key:
        public = load_public_key(args.public_key)
    elif args.signing_key:
        public = load_public_key(args.signing_key)
    store = Store(args.database_url)
    try:
        report = store.verify(args.checkpoints, public)
    finally:
        store.close()
    _print_report(report, args.json)
    if not report.ok:
        raise SystemExit(2)


def _cmd_healthcheck(args: argparse.Namespace) -> None:
    _host, port = _split_listen(args.listen)
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            if response.status != 200:
                raise SystemExit("unhealthy: " + str(response.status))
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


def _cmd_migrate(args: argparse.Namespace) -> None:
    if not args.migrate_url:
        raise SystemExit("CAPLOG_MIGRATE_URL is required")
    migrate(args.migrate_url, args.app_role)


def _backup_loop(store: Store, directory: str) -> None:
    from caplog.errors import ErrNoTitle

    while True:
        time.sleep(60)
        try:
            settings = store.settings()
            if settings.get("feature_scheduled_export") != "true" or settings.get("export_every") == "off":
                continue
            gap = 3600 if settings.get("export_every") == "hourly" else 86400
            last = settings.get("export_last") or ""
            if last:
                from datetime import datetime, timezone

                try:
                    previous = datetime.fromisoformat(last.replace("Z", "+00:00"))
                except ValueError:
                    previous = datetime.fromtimestamp(0, timezone.utc)
                if (datetime.now(timezone.utc) - previous).total_seconds() < gap:
                    continue
            record = store.record_export("json")
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            stamp = record.created_at.replace(":", "").replace("-", "")
            target = path / f"caplog-{stamp}-{record.seq}.json"
            with target.open("wb") as handle:
                store.write_json_export(handle, record)
            if settings.get("export_pdf") == "true":
                from caplog.pdfexport import write_pdf

                rows = []
                store.each(record.seq, rows.append)
                pdf = path / f"caplog-{stamp}-{record.seq}.pdf"
                with pdf.open("wb") as handle:
                    write_pdf(handle, store.title(), rows, record, settings.get("site_fqdn", ""))
            keep = int(settings.get("export_keep") or "7")
            files = sorted(path.glob("caplog-*"))
            for old in files[:-keep]:
                old.unlink(missing_ok=True)
            store.set_setting("export_last", record.created_at)
        except ErrNoTitle:
            continue
        except Exception:
            log.exception("scheduled export failed")


def _cmd_serve(args: argparse.Namespace) -> None:
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")
    if args.migrate_url:
        migrate(args.migrate_url, args.app_role)
    trusted = parse_trusted_proxies(args.trusted_proxies)
    store = Store(args.database_url)
    public = None
    scheduler = None
    if not args.signing_key:
        log.warning(
            "NO SIGNING KEY CONFIGURED: signed checkpoints are disabled, so a fully "
            "rewritten log could go undetected. Create one with 'caplog keygen' and "
            "set CAPLOG_SIGNING_KEY (see README)."
        )
    else:
        private = load_private_key(args.signing_key)
        public = private.public_key()
        writer = Writer(args.checkpoints, private)
        scheduler = Scheduler(
            writer,
            head=lambda: (lambda record: (record.seq, record.hash))(store.head()),
            every=args.checkpoint_every,
            interval=args.checkpoint_interval,
        )
        store.on_append(lambda records: scheduler.added(len(records)))
        scheduler.start()
        from caplog.checkpoint import key_id

        log.info("signed checkpoints enabled file=%s key_id=%s", args.checkpoints, key_id(public))
    cert_dir = _env("CAPLOG_CERTS", "/certs")
    backup_dir = _env("CAPLOG_BACKUPS", "/backups")
    app = create_app(
        store,
        Config(
            checkpoint_path=args.checkpoints,
            public_key=public,
            max_import_bytes=args.max_import_mb * 1024 * 1024,
            trusted=trusted,
            static_path=static_dir(),
            cert_dir=cert_dir,
            backup_dir=backup_dir,
        ),
    )
    host, port = _split_listen(args.listen)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        access_log=False,
        proxy_headers=False,
        timeout_keep_alive=120,
        timeout_graceful_shutdown=5,
        log_level="info",
    )
    server = uvicorn.Server(config)
    try:
        https_on = store.settings().get("https_enabled") == "true"
    except Exception:
        https_on = False
    cert_file = Path(cert_dir) / "cert.pem"
    key_file = Path(cert_dir) / "key.pem"
    if https_on and cert_file.exists() and key_file.exists():
        https_port = _env_int("CAPLOG_HTTPS_PORT", 8443)

        def _https() -> None:
            uvicorn.run(
                app,
                host=host,
                port=https_port,
                ssl_certfile=str(cert_file),
                ssl_keyfile=str(key_file),
                access_log=False,
                proxy_headers=False,
                log_level="info",
            )

        threading.Thread(target=_https, name="caplog-https", daemon=True).start()
        log.info("HTTPS listening port=%s", https_port)
    threading.Thread(target=_backup_loop, args=(store, backup_dir), name="caplog-backup", daemon=True).start()
    log.info("CapLog listening addr=%s version=%s", args.listen, os.environ.get("CAPLOG_VERSION", __version__))
    try:
        server.run()
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        store.close()


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--listen", default=_env("CAPLOG_LISTEN", ":8080"))
    parser.add_argument("--database-url", default=_env("DATABASE_URL", ""))
    parser.add_argument("--migrate-url", default=_env("CAPLOG_MIGRATE_URL", ""))
    parser.add_argument("--app-role", default=_env("CAPLOG_APP_ROLE", "caplog_app"))
    parser.add_argument("--signing-key", default=_env("CAPLOG_SIGNING_KEY", ""))
    parser.add_argument("--public-key", default=_env("CAPLOG_PUBLIC_KEY", ""))
    parser.add_argument("--checkpoints", default=_env("CAPLOG_CHECKPOINTS", "/checkpoints/checkpoints.jsonl"))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    argv = list(sys.argv[1:] if argv is None else argv)
    command = "serve"
    if argv and not argv[0].startswith("-"):
        command, argv = argv[0], argv[1:]
    parser = argparse.ArgumentParser(prog="caplog")
    _add_common(parser)
    if command == "serve":
        parser.add_argument("--checkpoint-every", type=int, default=_env_int("CAPLOG_CHECKPOINT_EVERY", 25))
        parser.add_argument(
            "--checkpoint-interval",
            type=parse_duration,
            default=_env_duration("CAPLOG_CHECKPOINT_INTERVAL", "10m"),
        )
        parser.add_argument("--max-import-mb", type=int, default=_env_int("CAPLOG_MAX_IMPORT_MB", 20))
        parser.add_argument("--trusted-proxies", default=_env("CAPLOG_TRUSTED_PROXIES", ""))
    elif command == "verify":
        parser.add_argument("--file", default="")
        parser.add_argument("--json", action="store_true")
    elif command == "keygen":
        argv = ["--out" if item == "-out" else item for item in argv]
        parser.add_argument("--out", default="caplog-signing.key")
    elif command == "healthcheck":
        pass
    elif command == "migrate":
        pass
    elif command == "version":
        print("caplog", os.environ.get("CAPLOG_VERSION", __version__))
        return 0
    else:
        print(
            f"caplog: unknown command {command!r} "
            "(use serve, keygen, verify, healthcheck, migrate or version)",
            file=sys.stderr,
        )
        return 1
    args = parser.parse_args(argv)
    try:
        if command == "serve":
            _cmd_serve(args)
        elif command == "keygen":
            _cmd_keygen(args)
        elif command == "verify":
            _cmd_verify(args)
        elif command == "healthcheck":
            _cmd_healthcheck(args)
        elif command == "migrate":
            _cmd_migrate(args)
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        if exc.code:
            print("caplog:", exc.code, file=sys.stderr)
        return 1
    except Exception as exc:
        print("caplog:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
