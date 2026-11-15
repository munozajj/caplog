"""HTTP server: the pages, the JSON API, and live updates."""

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from caplog.accounts import check_person_name, new_token
from caplog.certs import describe, generate_self_signed, write_pair
from caplog.errors import (
    ErrDenied,
    ErrLocked,
    ErrNoTitle,
    ErrRateLimited,
    ErrTitleIsSet,
    ErrUnauthenticated,
    ValidationError,
)
from caplog.jsonutil import dumps
from caplog.net import client_ip
from caplog.ntpcheck import warning as clock_warning
from caplog.pdfexport import write_pdf
from caplog.prefs import DISPLAY_KEYS
from caplog.store import Store

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
    cert_dir: str = "/certs"
    backup_dir: str = "/backups"


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
        loop = asyncio.get_running_loop()
        if hub.loop is None or hub.loop.is_closed():
            hub.loop = loop
        try:
            yield
        finally:
            if hub.loop is loop:
                hub.loop = None

    return _lifespan


def _fail(exc: Exception) -> Response:
    if isinstance(exc, ErrUnauthenticated):
        return _error(str(exc), 401)
    if isinstance(exc, ValidationError):
        return _error(str(exc), 400)
    if isinstance(exc, (ErrTitleIsSet, ErrNoTitle)):
        return _error(str(exc), 409)
    if isinstance(exc, ErrDenied):
        return _error(str(exc), 403)
    if isinstance(exc, ErrLocked):
        return _error(str(exc), 429)
    if isinstance(exc, ErrRateLimited):
        return _error(str(exc), 429)
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


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"


def _csrf_cookie(response: Response, request: Request) -> str:
    token = request.cookies.get("caplog_csrf") or new_token()
    response.set_cookie(
        "caplog_csrf",
        token,
        httponly=False,
        samesite="lax",
        secure=_secure(request),
        path="/",
        max_age=86400,
    )
    return token


def _session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        "caplog_session",
        token,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
        path="/",
        max_age=12 * 3600,
    )


def _clear_session(response: Response, request: Request) -> None:
    response.delete_cookie("caplog_session", path="/", samesite="lax", secure=_secure(request), httponly=True)


def _admin_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        "caplog_admin",
        token,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
        path="/admin",
        max_age=12 * 3600,
    )


def _clear_admin(response: Response, request: Request) -> None:
    response.delete_cookie("caplog_admin", path="/admin", samesite="lax", secure=_secure(request), httponly=True)


def _ready(store: Store) -> bool:
    try:
        store.title()
    except ErrNoTitle:
        return False
    return store.user_count() > 0


_NOT_READY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CapLog</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <main class="unconfigured">
    <p>This logbook has not been configured by the administrator yet.</p>
  </main>
</body>
</html>
"""


def _ip(request: Request, config: Config) -> str:
    return client_ip(
        _remote_host(request),
        request.headers.get("x-forwarded-for", ""),
        request.headers.get("x-real-ip", ""),
        config.trusted,
    )


def _public_user(user: dict | None) -> dict | None:
    if user is None:
        return None
    return {
        "username": user["username"],
        "first_name": user["first_name"],
        "last_name": user["last_name"],
        "role": user["role"],
        "is_master": user["is_master"],
    }


def _redirect_target(store: Store, request: Request) -> str | None:
    """Send browsers to HTTPS when the admin turned that on. Health checks stay on HTTP."""
    if request.url.path == "/healthz" or request.url.scheme == "https":
        return None
    try:
        settings = store.settings()
    except Exception:
        return None
    if settings.get("http_redirect") != "true" or settings.get("https_enabled") != "true":
        return None
    host = settings.get("site_fqdn") or request.url.hostname or "localhost"
    port = settings.get("https_port") or "8443"
    target = f"https://{host}:{port}{request.url.path}"
    if request.url.query:
        target += "?" + request.url.query
    return target


def create_app(store: Store, config: Config) -> FastAPI:
    hub = Hub()
    store.on_append(hub.publish)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan(hub))
    app.state.store = store
    app.state.config = config
    app.state.hub = hub

    @app.middleware("http")
    async def guard_and_headers(request: Request, call_next):
        target = _redirect_target(store, request)
        if target is not None:
            response = RedirectResponse(target, status_code=307)
        elif request.url.path.startswith("/api/") and not _ready(store):
            response = _error("This logbook has not been configured by the administrator yet.", 409)
        elif request.method == "POST" and (
            request.url.path.startswith("/api/") or request.url.path.startswith("/admin")
        ):
            if request.headers.get("x-caplog") != "1":
                response = _error("missing X-CapLog: 1 header", 403)
            elif request.cookies.get("caplog_csrf", "") == "" or request.headers.get("x-csrf") != request.cookies.get("caplog_csrf"):
                response = _error("missing CSRF token", 403)
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

    def _html(request: Request, name: str) -> FileResponse:
        response = FileResponse(
            config.static_path / name,
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )
        _csrf_cookie(response, request)
        return response

    @app.get("/")
    def index(request: Request):
        if not _ready(store):
            response = Response(
                _NOT_READY_HTML,
                media_type="text/html",
                headers={"Cache-Control": "no-cache"},
            )
            _csrf_cookie(response, request)
            return response
        return _html(request, "index.html")

    @app.get("/verify")
    def verify_page(request: Request):
        return _html(request, "verify.html")

    @app.get("/admin/login")
    def admin_login_page(request: Request):
        if not _ready(store):
            return RedirectResponse("/admin", status_code=303)
        if _admin_user(request) is not None:
            return RedirectResponse("/admin", status_code=303)
        return _html(request, "admin-login.html")

    @app.get("/admin")
    def admin_page(request: Request):
        if not _ready(store):
            return _html(request, "admin-setup.html")
        if _admin_user(request) is None:
            return RedirectResponse("/admin/login", status_code=303)
        return _html(request, "admin.html")

    @app.get("/display")
    def display_page(request: Request):
        if store.settings().get("feature_display_page") != "true":
            return _error("the display page is turned off", 404)
        return _html(request, "display.html")

    @app.get("/healthz")
    def health():
        try:
            store.ping()
        except Exception:
            return Response("database unavailable\n", status_code=503, media_type="text/plain")
        return Response("ok\n", media_type="text/plain")

    def _current(request: Request) -> dict | None:
        return store.session_user(request.cookies.get("caplog_session", ""))

    def _admin_user(request: Request) -> dict | None:
        token = request.cookies.get("caplog_admin", "")
        if not token:
            return None
        user = store.session_user(token)
        if user is None or user["role"] != "admin":
            return None
        return user

    def _admin(request: Request) -> dict:
        user = _admin_user(request)
        if user is None:
            raise ErrUnauthenticated()
        return user

    def _clock_text() -> str:
        settings = store.settings()
        if settings.get("ntp_enabled") != "true" or not settings.get("ntp_server"):
            return ""
        cached = getattr(app.state, "clock_cache", None)
        now = time.time()
        if cached and now - cached[0] < 60:
            return cached[1]
        text = clock_warning(settings["ntp_server"], float(settings["ntp_threshold"]))
        app.state.clock_cache = (now, text)
        return text

    @app.get("/api/state")
    def state(request: Request):
        settings = store.settings()
        user = _current(request)
        csrf = request.cookies.get("caplog_csrf") or new_token()
        out = {
            "setup_needed": False,
            "admin_needed": store.user_count() == 0,
            "max_text_chars": store.max_text(),
            "max_name_chars": 60 if settings.get("guest_name_rules") == "true" else 100,
            "signing": config.public_key is not None,
            "settings": settings,
            "user": _public_user(user),
            "user_display": {},
            "corrections": store.correction_map(),
            "clock_warning": "",
            "csrf": csrf,
        }
        if user and user.get("display_json"):
            try:
                out["user_display"] = json.loads(user["display_json"])
            except json.JSONDecodeError:
                out["user_display"] = {}
        try:
            head = store.head()
        except ErrNoTitle:
            out["setup_needed"] = True
            response = _json(out)
            response.set_cookie(
                "caplog_csrf",
                csrf,
                httponly=False,
                samesite="lax",
                secure=_secure(request),
                path="/",
                max_age=86400,
            )
            return response
        except Exception as exc:
            return _fail(exc)
        try:
            out["title"] = store.title()
            out["clock_warning"] = _clock_text()
        except Exception as exc:
            return _fail(exc)
        out["head_seq"] = head.seq
        response = _json(out)
        response.set_cookie(
            "caplog_csrf",
            csrf,
            httponly=False,
            samesite="lax",
            secure=_secure(request),
            path="/",
            max_age=86400,
        )
        return response

    @app.post("/admin/setup")
    async def setup(request: Request):
        try:
            body = await _read_json(
                request,
                8 * 1024,
                {"title", "username", "first_name", "last_name", "password"},
            )
            record = store.setup_with_admin(
                str(body.get("title", "")),
                str(body.get("username", "")),
                str(body.get("first_name", "")),
                str(body.get("last_name", "")),
                str(body.get("password", "")),
            )
        except (ValidationError, ErrTitleIsSet, ErrNoTitle) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        log.info("logbook created")
        return _json(record.to_dict(), 201)

    @app.post("/api/login")
    async def login(request: Request):
        try:
            body = await _read_json(request, 8 * 1024, {"username", "password"})
            user, token = store.login(
                str(body.get("username", "")),
                str(body.get("password", "")),
                _ip(request, config),
            )
        except (ValidationError, ErrDenied, ErrLocked) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        response = _json(_public_user(user))
        _session_cookie(response, request, token)
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        store.logout(request.cookies.get("caplog_session", ""))
        response = _json({"ok": True})
        _clear_session(response, request)
        return response

    @app.post("/admin/login")
    async def admin_login(request: Request):
        try:
            body = await _read_json(request, 8 * 1024, {"username", "password"})
            user, token = store.login(
                str(body.get("username", "")),
                str(body.get("password", "")),
                _ip(request, config),
            )
        except (ValidationError, ErrDenied, ErrLocked) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        if user["role"] != "admin":
            store.logout(token)
            return _error("an admin account is required", 403)
        response = _json(_public_user(user))
        _admin_cookie(response, request, token)
        return response

    @app.post("/admin/logout")
    def admin_logout(request: Request):
        store.logout(request.cookies.get("caplog_admin", ""))
        response = _json({"ok": True})
        _clear_admin(response, request)
        return response

    @app.post("/api/entries")
    async def add_entry(request: Request):
        try:
            if store.user_count() == 0:
                raise ErrNoTitle()
            body = await _read_json(
                request,
                16 * 1024,
                {"first_name", "last_name", "text", "tag", "corrects"},
            )
            settings = store.settings()
            user = _current(request)
            mode = settings.get("post_mode", "both")
            ip = _ip(request, config)
            use_account = user is not None and mode != "guests"
            if mode == "accounts" and user is None:
                raise ErrDenied("sign in to add an entry")
            if use_account:
                first = user["first_name"]
                last = user["last_name"]
                meta = {"poster": "account", "username": user["username"]}
                user_id = user["id"]
            else:
                rules = settings.get("guest_name_rules") == "true"
                first = check_person_name(str(body.get("first_name", "")), "first name", rules)
                last = check_person_name(str(body.get("last_name", "")), "last name", rules)
                meta = {"poster": "guest"}
                user_id = None
            tag = str(body.get("tag", "")).strip()
            if tag:
                if settings.get("feature_tags") != "true":
                    raise ErrDenied("tags are turned off")
                allowed = [item for item in settings.get("tags", "").split(",") if item]
                if tag not in allowed:
                    raise ValidationError("that tag is not one of the tags the admin defined")
                meta["tag"] = tag
            corrects = body.get("corrects")
            side = None
            if corrects not in (None, "", 0):
                if settings.get("feature_corrections") != "true":
                    raise ErrDenied("corrections are turned off")
                if type(corrects) is not int or store.get(corrects) is None:
                    raise ValidationError("that entry number does not exist")
                meta["corrects"] = corrects

                def side(conn, saved, old=corrects) -> None:
                    conn.execute(
                        "INSERT INTO correction_links (new_seq, old_seq) VALUES (%s, %s)",
                        (saved[0].seq, old),
                    )

            store.check_post_rate(ip, user_id)
            record = store.add_entry(first, last, str(body.get("text", "")), ip, dumps(meta), side)
            store.record_hit(ip, user_id)
        except (ValidationError, ErrTitleIsSet, ErrNoTitle, ErrDenied, ErrRateLimited) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        return _json(record.to_dict(), 201)

    @app.post("/api/display")
    async def save_display(request: Request):
        try:
            if store.settings().get("users_may_change_display") != "true":
                raise ErrDenied("the admin has turned off display changes")
            user = _current(request)
            body = await _read_json(request, 8 * 1024, set(DISPLAY_KEYS))
            clean = {key: store.clean_setting(key, str(body[key])) for key in body}
            if user is None:
                return _json({"saved": "browser"})
            store.save_display(user["id"], clean)
        except (ValidationError, ErrDenied) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        return _json({"saved": "account"})

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
            filters = (
                request.query_params.get("q", ""),
                request.query_params.get("name", ""),
                request.query_params.get("start", ""),
                request.query_params.get("end", ""),
                request.query_params.get("tag", ""),
            )
            if any(filters):
                rows = store.search_records(*filters, limit)
            else:
                rows = store.list_after(after, limit)
        except (ValidationError, ErrDenied) as exc:
            return _fail(exc)
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

    @app.get("/admin/api/state")
    def admin_state(request: Request):
        try:
            _admin(request)
        except ErrDenied as exc:
            return _fail(exc)
        settings = store.settings()
        cert = describe(config.cert_dir)
        warn_days = int(settings.get("cert_warn_days", "30"))
        if cert.get("present") and cert.get("seconds_left", 0) < warn_days * 86400:
            cert["warning"] = "This certificate expires soon."
        return _json({"settings": settings, "users": store.list_users(), "certificate": cert})

    @app.post("/admin/api/settings")
    async def admin_settings(request: Request):
        try:
            _admin(request)
            body = await _read_json(request, 16 * 1024, {"key", "value"})
            record = store.set_setting(str(body.get("key", "")), str(body.get("value", "")))
        except (ValidationError, ErrDenied, ErrNoTitle) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        return _json(record.to_dict())

    @app.post("/admin/api/users")
    async def admin_users(request: Request):
        try:
            _admin(request)
            body = await _read_json(
                request,
                8 * 1024,
                {"action", "username", "first_name", "last_name", "password", "role", "enabled"},
            )
            action = str(body.get("action", ""))
            username = str(body.get("username", ""))
            if action == "create":
                store.create_user(
                    username,
                    str(body.get("first_name", "")),
                    str(body.get("last_name", "")),
                    str(body.get("password", "")),
                    str(body.get("role", "user")),
                )
            elif action == "enabled":
                store.set_user_enabled(username, bool(body.get("enabled")))
            elif action == "role":
                store.set_user_role(username, str(body.get("role", "")))
            elif action == "password":
                store.set_user_password(username, str(body.get("password", "")))
            else:
                raise ValidationError("unknown user action")
        except (ValidationError, ErrDenied, ErrNoTitle) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        return _json({"ok": True})

    @app.post("/admin/api/certificate")
    async def admin_certificate(request: Request):
        try:
            _admin(request)
            body = await _read_json(request, 256 * 1024, {"action", "certificate", "key", "hostname"})
            action = str(body.get("action", ""))
            if action == "generate":
                hostname = store.settings().get("site_fqdn") or str(body.get("hostname", "")) or "caplog.local"
                cert_pem, key_pem, _expires = generate_self_signed(hostname)
            elif action == "upload":
                cert_pem = str(body.get("certificate", "")).encode()
                key_pem = str(body.get("key", "")).encode()
            else:
                raise ValidationError("unknown certificate action")
            expires = write_pair(config.cert_dir, cert_pem, key_pem)
            saved = store.log_system(
                "A TLS certificate was saved. It expires on "
                + expires.strftime("%Y-%m-%d")
                + ". Restart CapLog before the new certificate is used.",
                {"event": "certificate"},
            )
        except (ValidationError, ErrDenied, ErrNoTitle) as exc:
            return _fail(exc)
        except Exception as exc:
            return _fail(exc)
        info = describe(config.cert_dir)
        info["restart_required"] = True
        info["seq"] = saved.seq
        return _json(info)

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
            write_pdf(buf, store.title(), rows, record, store.settings().get("site_fqdn", ""))
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
