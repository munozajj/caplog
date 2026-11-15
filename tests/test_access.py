from pathlib import Path

from tests.test_web import STATIC, _client, _headers, _setup

ROOT = Path(__file__).resolve().parent.parent
USER_PAGES = (
    "index.html",
    "verify.html",
    "display.html",
    "app.js",
    "display.js",
    "verify.js",
    "help/user.html",
)


def test_user_pages_have_no_admin_link():
    for name in USER_PAGES:
        text = (STATIC / name).read_text()
        assert "/admin" not in text, name
    page = (ROOT / "static" / "help" / "user.html").read_text()
    assert "Show example" in page or "show example" in page.lower()
    assert "Menu" in page


def test_before_setup_user_site_is_closed_and_admin_shows_setup(db):
    client = _client(db["store"])
    page = client.get("/")
    assert page.status_code == 200
    assert "This logbook has not been configured by the administrator yet." in page.text
    assert "/admin" not in page.text
    headers = _headers(client)
    refused = client.post(
        "/api/entries",
        headers=headers,
        json={"first_name": "Ada", "last_name": "Lovelace", "text": "too soon"},
    )
    assert refused.status_code == 409
    state = client.get("/api/state")
    assert state.status_code == 409
    setup = client.get("/admin")
    assert setup.status_code == 200
    assert "Logbook title" in setup.text
    assert "Admin username" in setup.text


def test_setup_once_then_admin_gate(db):
    client = _client(db["store"])
    made, headers = _setup(client)
    assert made.status_code == 201
    again = client.post(
        "/admin/setup",
        headers=headers,
        json={
            "title": "Other",
            "username": "grace",
            "first_name": "Grace",
            "last_name": "Hopper",
            "password": "correct horse battery",
        },
    )
    assert again.status_code == 409
    home = client.get("/")
    assert "not been configured" not in home.text
    assert "/admin" not in home.text
    signed_out = client.get("/admin", follow_redirects=False)
    assert signed_out.status_code == 303
    assert signed_out.headers["location"] == "/admin/login"
    login = client.get("/admin/login")
    assert login.status_code == 200
    assert "Admin sign in" in login.text


def test_sessions_are_separate(db):
    client = _client(db["store"])
    _made, headers = _setup(client)
    user = client.post("/api/login", headers=headers, json={"username": "ada", "password": "correct horse battery"})
    assert user.status_code == 200
    user_cookie = user.headers["set-cookie"]
    assert "caplog_session=" in user_cookie
    assert "Path=/" in user_cookie
    assert "caplog_admin=" not in user_cookie
    blocked = client.get("/admin/api/state")
    assert blocked.status_code == 401
    page = client.get("/admin", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/admin/login"
    admin = client.post("/admin/login", headers=headers, json={"username": "ada", "password": "correct horse battery"})
    assert admin.status_code == 200
    admin_cookie = admin.headers["set-cookie"]
    assert "caplog_admin=" in admin_cookie
    assert "Path=/admin" in admin_cookie
    opened = client.get("/admin/api/state")
    assert opened.status_code == 200
    fresh = _client(db["store"])
    fresh.get("/admin/login")
    fresh_headers = {"X-CapLog": "1", "X-CSRF": fresh.cookies.get("caplog_csrf")}
    fresh.post("/admin/login", headers=fresh_headers, json={"username": "ada", "password": "correct horse battery"})
    alone = fresh.get("/api/state")
    assert alone.json()["user"] is None


def test_non_admin_cannot_sign_in_at_admin(db):
    client = _client(db["store"])
    _made, headers = _setup(client)
    admin = client.post("/admin/login", headers=headers, json={"username": "ada", "password": "correct horse battery"})
    assert admin.status_code == 200
    created = client.post(
        "/admin/api/users",
        headers=headers,
        json={
            "action": "create",
            "username": "grace",
            "first_name": "Grace",
            "last_name": "Hopper",
            "password": "correct horse battery",
            "role": "user",
        },
    )
    assert created.status_code == 200
    other = _client(db["store"])
    other.get("/admin/login")
    other_headers = {"X-CapLog": "1", "X-CSRF": other.cookies.get("caplog_csrf")}
    denied = other.post(
        "/admin/login",
        headers=other_headers,
        json={"username": "grace", "password": "correct horse battery"},
    )
    assert denied.status_code == 403
    assert "caplog_admin=" not in denied.headers.get("set-cookie", "")
    assert other.get("/admin/api/state").status_code == 401
    user_side = other.post(
        "/api/login",
        headers=other_headers,
        json={"username": "grace", "password": "correct horse battery"},
    )
    assert user_side.status_code == 200
    assert other.get("/admin/api/state").status_code == 401


def test_admin_sign_in_shares_lockout(db):
    client = _client(db["store"])
    _made, headers = _setup(client)
    for _ in range(4):
        bad = client.post("/admin/login", headers=headers, json={"username": "ada", "password": "not-the-password"})
        assert bad.status_code == 403
    fifth = client.post("/api/login", headers=headers, json={"username": "ada", "password": "not-the-password"})
    assert fifth.status_code == 429
    locked = client.post("/admin/login", headers=headers, json={"username": "ada", "password": "correct horse battery"})
    assert locked.status_code == 429
