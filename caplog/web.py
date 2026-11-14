"""HTTP server: the pages, the JSON API, and live updates."""

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from caplog.errors import ErrNoTitle, ErrTitleIsSet, ValidationError
from caplog.jsonutil import dumps
from caplog.net import client_ip
from caplog.pdfexport import write_pdf
from caplog.store import MAX_NAME_CHARS, MAX_TEXT_CHARS, Store

log = logging.getLogger("caplog")
_MAX_SUBSCRIBERS = 2000


def static_dir() -> Path:
    import os

    override = os.environ.get("CAPLOG_STATIC")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "static"


@dataclass
class Config:
    checkpoint_path: str
    public_key: object
    max_import_bytes: int
    trusted: list
    static_path: Path


class Hub:
    """Fan out new records to browsers. A slow browser is dropped and reconnects."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._subs: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue | None:
        with self._lock:
            if len(self._subs) >= _MAX_SUBSCRIBERS:
                return None
            queue: asyncio.Queue = asyncio.Queue(maxsize=256)
            self._subs.append(queue)
            return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            if queue in self._subs:
                self._subs.remove(queue)

    def publish(self, records) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        payload = [(record.seq, dumps(record.to_dict())) for record in records]

        def fanout() -> None:
            with self._lock:
                subs = list(self._subs)
            dead = []
            for seq, data in payload:
                for queue in subs:
                    try:
                        queue.put_nowait((seq, data))
                    except asyncio.QueueFull:
                        dead.append(queue)
            if dead:
                with self._lock:
                    for queue in dead:
                        if queue in self._subs:
                            self._subs.remove(queue)

        try:
            loop.call_soon_threadsafe(fanout)
        except RuntimeError:
            # The process is shutting down. The row is already stored.
            return


def _json(data, status: int = 200) -> Response:
    body = dumps(data).encode() + b"\n"
    return Response(
        content=body,
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _error(message: str, status: int) -> Response:
    return _json({"error": message}, status)


def lifespan(hub: Hub):
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        try:
            yield
        finally:
            hub.loop = None

    return _lifespan


def _fail(exc: Exception) -> Response:
    if isinstance(exc, ValidationError):
        return _error(str(exc), 400)
    if isinstance(exc, (ErrTitleIsSet, ErrNoTitle)):
        return _error(str(exc), 409)
    log.exception("request failed")
    return _error("internal error", 500)


async def _read_limited(request: Request, limit: int) -> bytes:
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ValidationError("too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json(request: Request, limit: int, keys: set[str]) -> dict:
    try:
        raw = await _read_limited(request, limit)
        import json

        obj = json.loads(raw or b"null")
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("invalid JSON body: " + str(exc)) from exc
    if not isinstance(obj, dict) or set(obj) - keys:
        raise ValidationError("invalid JSON body: unexpected fields")
    return obj


def _remote_host(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host or ""


def create_app(store: Store, config: Config) -> FastAPI:
    hub = Hub()
    store.on_append(hub.publish)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan(hub))
    app.state.store = store
    app.state.config = config
    app.state.hub = hub

    @app.middleware("http")
    async def guard_and_headers(request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith("/api/"):
            if request.headers.get("x-caplog") != "1":
                response = _error("missing X-CapLog: 1 header", 403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/")
    def index():
        return FileResponse(config.static_path / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/verify")
    def verify_page():
        return FileResponse(config.static_path / "verify.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    def health():
        try:
            store.ping()
        except Exception:
            return Response("database unavailable\n", status_code=503, media_type="text/plain")
        return Response("ok\n", media_type="text/plain")

    @app.get("/api/state")
    def state():
        out = {
            "setup_needed": False,
            "max_text_chars": MAX_TEXT_CHARS,
            "max_name_chars": MAX_NAME_CHARS,
            "signing": config.public_key is not None,
        }
        try:
            head = store.head()
        except ErrNoTitle:
            out["setup_needed"] = True
            return _json(out)
        except Exception as exc:
            return _fail(exc)
        try:
            out["title"] = store.title()
        except Exception as exc:
            return _fail(exc)
        out["head_seq"] = head.seq
        return _json(out)

    @app.post("/api/setup")
    async def setup(request: Request):
        try:
            body = await _read_json(request, 8 * 1024, {"title"})
            record = store.setup(str(body.get("title", "")))
        except (ValidationError, ErrTitleIsSet, ErrNoTitle) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        log.info("logbook created")
        return _json(record.to_dict(), 201)

    @app.post("/api/entries")
    async def add_entry(request: Request):
        try:
            body = await _read_json(request, 16 * 1024, {"first_name", "last_name", "text"})
            ip = client_ip(
                _remote_host(request),
                request.headers.get("x-forwarded-for", ""),
                request.headers.get("x-real-ip", ""),
                config.trusted,
            )
            record = store.add_entry(
                str(body.get("first_name", "")),
                str(body.get("last_name", "")),
                str(body.get("text", "")),
                ip,
            )
        except (ValidationError, ErrTitleIsSet, ErrNoTitle) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        return _json(record.to_dict(), 201)

    @app.get("/api/records")
    def records(request: Request):
        after = -1
        raw_after = request.query_params.get("after")
        if raw_after is not None and raw_after != "":
            try:
                after = int(raw_after)
            except ValueError:
                return _error("bad 'after'", 400)
        limit = 500
        raw_limit = request.query_params.get("limit")
        if raw_limit:
            try:
                parsed = int(raw_limit)
            except ValueError:
                parsed = 0
            if 0 < parsed <= 1000:
                limit = parsed
        try:
            rows = store.list_after(after, limit)
        except Exception as exc:
            return _fail(exc)
        return _json([row.to_dict() for row in rows])

    @app.get("/api/events")
    async def events(request: Request):
        queue = hub.subscribe()
        if queue is None:
            return _error("too many live connections, try again later", 503)
        header = request.headers.get("last-event-id")
        if header:
            try:
                after = int(header)
            except ValueError:
                after = 0
        else:
            raw = request.query_params.get("after")
            if raw:
                try:
                    after = int(raw)
                except ValueError:
                    after = 0
            else:
                after = -1

        async def generate():
            current = after
            try:
                yield "retry: 3000\n\n"
                while True:
                    rows = await asyncio.to_thread(store.list_after, current, 500)
                    for record in rows:
                        if record.seq <= current:
                            continue
                        current = record.seq
                        yield f"id: {record.seq}\nevent: record\ndata: {dumps(record.to_dict())}\n\n"
                    if len(rows) < 500:
                        break
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        seq, data = await asyncio.wait_for(queue.get(), timeout=25)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if seq <= current:
                        continue
                    current = seq
                    yield f"id: {seq}\nevent: record\ndata: {data}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/export/{fmt}")
    def export(fmt: str):
        if fmt not in ("json", "pdf"):
            return _error("format must be json or pdf", 404)
        try:
            store.title()
            record = store.record_export(fmt)
        except (ValidationError, ErrNoTitle, ErrTitleIsSet) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        stamp = record.created_at.replace(":", "").replace("-", "")[:15]
        name = f"caplog-{stamp}-{record.seq}"
        if fmt == "json":
            import io

            buf = io.BytesIO()
            try:
                store.write_json_export(buf, record)
            except Exception as exc:
                return _fail(exc)
            return Response(
                content=buf.getvalue(),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
            )
        rows = []
        try:
            store.each(record.seq, rows.append)
            import io

            buf = io.BytesIO()
            write_pdf(buf, store.title(), rows, record)
        except Exception as exc:
            return _fail(exc)
        return Response(
            content=buf.getvalue(),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'},
        )

    @app.post("/api/import")
    async def import_log(request: Request):
        try:
            data = await _read_limited(request, config.max_import_bytes + 1)
        except ValidationError:
            return _error("import file is too large", 413)
        except Exception:
            return _error("could not read upload", 400)
        if len(data) > config.max_import_bytes:
            return _error("import file is too large", 413)
        try:
            result = store.import_data(data)
        except (ValidationError, ErrNoTitle, ErrTitleIsSet) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        log.info("import completed records=%s", result.imported)
        return _json(result.to_dict(), 201)

    @app.get("/api/verify")
    def verify():
        report = store.verify(config.checkpoint_path, config.public_key)
        return _json(report.to_dict())

    app.mount("/static", StaticFiles(directory=config.static_path), name="static")
    return app
