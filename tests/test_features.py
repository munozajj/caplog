from pathlib import Path

from caplog.certs import generate_self_signed
from caplog.web import Config, create_app
from tests.test_web import STATIC

ROOT = Path(__file__).resolve().parent.parent


def _open(store, tmp_path):
    app = create_app(
        store,
        Config(
            checkpoint_path="",
            public_key=None,
            max_import_bytes=1024,
            trusted=[],
            static_path=STATIC,
            cert_dir=str(tmp_path),
            backup_dir=str(tmp_path / "backups"),
        ),
    )
    from starlette.testclient import TestClient

    client = TestClient(app)
    client.get("/api/state")
    headers = {"X-CapLog": "1", "X-CSRF": client.cookies.get("caplog_csrf")}
    made = client.post(
        "/api/setup",
        headers=headers,
        json={
            "title": "Watch Log",
            "username": "ada",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "password": "correct horse battery",
        },
    )
    assert made.status_code == 201
    return client, headers


def _login(client, headers, username="ada", password="correct horse battery"):
    res = client.post("/api/login", headers=headers, json={"username": username, "password": password})
    assert res.status_code == 200
    return res


def test_login_lockout_and_admin_routes(db, tmp_path):
    client, headers = _open(db["store"], tmp_path)
    blocked = client.get("/api/admin/state")
    assert blocked.status_code == 403
    for _ in range(4):
        bad = client.post("/api/login", headers=headers, json={"username": "ada", "password": "not-the-password"})
        assert bad.status_code == 403
    fifth = client.post("/api/login", headers=headers, json={"username": "ada", "password": "not-the-password"})
    assert fifth.status_code == 429
    locked = client.post("/api/login", headers=headers, json={"username": "ada", "password": "correct horse battery"})
    assert locked.status_code == 429
    assert "locked" in locked.json()["error"]


def test_post_modes_names_and_corrections(db, tmp_path):
    store = db["store"]
    client, headers = _open(store, tmp_path)
    _login(client, headers)
    denied = client.post("/api/admin/settings", headers=headers, json={"key": "post_mode", "value": "accounts"})
    assert denied.status_code == 200
    posted = client.post("/api/entries", headers=headers, json={"text": "from the account"})
    assert posted.status_code == 201
    assert posted.json()["first_name"] == "Ada"
    assert posted.json()["last_name"] == "Lovelace"
    assert '"poster":"account"' in posted.json()["meta"]
    client.post("/api/logout", headers=headers)
    rejected = client.post(
        "/api/entries",
        headers=headers,
        json={"first_name": "Grace", "last_name": "Hopper", "text": "no account"},
    )
    assert rejected.status_code == 403
    _login(client, headers)
    original = posted.json()["seq"]
    corrected = client.post(
        "/api/entries",
        headers=headers,
        json={"text": "a later note", "corrects": original},
    )
    assert corrected.status_code == 201
    assert store.correction_map()[original] == [corrected.json()["seq"]]
    system = [row for row in store.list_after(0, 100) if row.kind == "system" and "post_mode" in row.body]
    assert system


def test_rate_limit_and_display_does_not_change_records(db, tmp_path):
    store = db["store"]
    client, headers = _open(store, tmp_path)
    _login(client, headers)
    assert client.post("/api/admin/settings", headers=headers, json={"key": "feature_rate_limit", "value": "true"}).status_code == 200
    assert client.post("/api/admin/settings", headers=headers, json={"key": "rate_limit_count", "value": "2"}).status_code == 200
    assert client.post("/api/entries", headers=headers, json={"text": "one"}).status_code == 201
    second = client.post("/api/entries", headers=headers, json={"text": "two"})
    assert second.status_code == 201
    before = store.get(second.json()["seq"])
    third = client.post("/api/entries", headers=headers, json={"text": "three"})
    assert third.status_code == 429
    saved = client.post("/api/display", headers=headers, json={"post_box": "left", "show_ip": "false"})
    assert saved.status_code == 200
    after = store.get(second.json()["seq"])
    assert after.hash == before.hash
    assert after.body == before.body


def test_certificate_upload_validation(db, tmp_path):
    client, headers = _open(db["store"], tmp_path)
    _login(client, headers)
    bad = client.post(
        "/api/admin/certificate",
        headers=headers,
        json={"action": "upload", "certificate": "nope", "key": "nope"},
    )
    assert bad.status_code == 400
    cert_pem, key_pem, _expires = generate_self_signed("log.example")
    _other, other_key, _other_exp = generate_self_signed("other.example")
    mismatch = client.post(
        "/api/admin/certificate",
        headers=headers,
        json={"action": "upload", "certificate": cert_pem.decode(), "key": other_key.decode()},
    )
    assert mismatch.status_code == 400
    good = client.post(
        "/api/admin/certificate",
        headers=headers,
        json={"action": "upload", "certificate": cert_pem.decode(), "key": key_pem.decode()},
    )
    assert good.status_code == 200
    assert "expires" in good.json()
    assert (tmp_path / "key.pem").stat().st_mode & 0o777 == 0o600
    logged = [row for row in db["store"].list_after(0, 50) if row.kind == "system" and "certificate" in row.body.lower()]
    assert logged
    assert "PRIVATE" not in logged[0].body


def test_help_matches_readme_settings():
    readme = (ROOT / "README.md").read_text()
    admin = (ROOT / "static" / "help" / "admin.html").read_text()
    user = (ROOT / "static" / "help" / "user.html").read_text()
    for name in (
        "post_mode",
        "site_fqdn",
        "https_enabled",
        "ntp_server",
        "ntp_threshold",
        "max_entry_length",
        "accent_color",
        "feature_search",
        "feature_corrections",
        "feature_scheduled_export",
        "post_box",
    ):
        assert name in readme
        assert name in admin
    assert "network team" in readme
    assert "west door" in user
    assert "cannot edit" in admin
