from pathlib import Path

from starlette.testclient import TestClient

from caplog.net import parse_trusted_proxies
from caplog.web import Config, create_app

STATIC = Path(__file__).resolve().parent.parent / "static"


def _client(store, trusted=None, client=("127.0.0.1", 1234)):
    app = create_app(
        store,
        Config(
            checkpoint_path="",
            public_key=None,
            max_import_bytes=1024,
            trusted=trusted or [],
            static_path=STATIC,
        ),
    )
    return TestClient(app, client=client)


def _headers(client):
    client.get("/admin")
    token = client.cookies.get("caplog_csrf")
    return {"X-CapLog": "1", "X-CSRF": token}


def _setup(client, title="Watch Log"):
    headers = _headers(client)
    made = client.post(
        "/admin/setup",
        headers=headers,
        json={
            "title": title,
            "username": "ada",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "password": "correct horse battery",
        },
    )
    return made, headers


def test_missing_header_limit_and_title(db):
    store = db["store"]
    with _client(store) as client:
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
        missing = client.post("/api/entries", json={"first_name": "Ada", "last_name": "Lovelace", "text": "hi"})
        assert missing.status_code == 403
        too_long = client.post(
            "/api/entries",
            headers=headers,
            json={"first_name": "Grace", "last_name": "Hopper", "text": "x" * 501},
        )
        assert too_long.status_code == 400
        assert "500" in too_long.json()["error"]


def test_trusted_proxy_ip(db):
    store = db["store"]
    trusted = parse_trusted_proxies("10.0.0.8")
    with _client(store, trusted=trusted, client=("10.0.0.8", 9)) as client:
        _made, headers = _setup(client)
        res = client.post(
            "/api/entries",
            headers={**headers, "X-Forwarded-For": "203.0.113.5, 10.0.0.8"},
            json={"first_name": "Grace", "last_name": "Hopper", "text": "hello"},
        )
        assert res.status_code == 201
        assert res.json()["ip"] == "203.0.113.5"
    with _client(store, trusted=trusted, client=("192.0.2.8", 9)) as client:
        _state = client.get("/api/state")
        headers = {"X-CapLog": "1", "X-CSRF": client.cookies.get("caplog_csrf")}
        res = client.post(
            "/api/entries",
            headers={**headers, "X-Forwarded-For": "203.0.113.9"},
            json={"first_name": "Grace", "last_name": "Hopper", "text": "second"},
        )
        assert res.status_code == 201
        assert res.json()["ip"] == "192.0.2.8"
