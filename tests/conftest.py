import os
import uuid

import psycopg
import pytest
from psycopg import sql

from caplog.store import Store, migrate


def _admin_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture(scope="session")
def postgres_url():
    url = _admin_url()
    if url:
        yield url
        return
    if not os.path.exists(os.environ.get("CAPLOG_PG_BIN", "/tmp/pg/bin/initdb")):
        pytest.skip("PostgreSQL binaries are not available and TEST_DATABASE_URL is unset")
    from tests.pgembed import Postgres

    server = Postgres()
    try:
        yield server.url
    finally:
        server.stop()


@pytest.fixture
def db(postgres_url):
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(postgres_url, autocommit=True)
    admin.execute("SELECT 1")
    for role, password in (("caplog_owner", "ownerpw"), ("caplog_app", "apppw")):
        admin.execute(
            sql.SQL("SELECT 1 FROM pg_roles WHERE rolname = {}").format(sql.Literal(role))
        )
        exists = admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            (role,),
        ).fetchone()
        if not exists:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                    sql.Identifier(role),
                    sql.Literal(password),
                )
            )
    admin.execute(
        sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier("caplog_owner"))
    )
    admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO caplog_app").format(sql.Identifier(name)))
    admin.close()

    def dsn(user: str, password: str) -> str:
        # postgres_url is postgresql://postgres@host:port/postgres
        rest = postgres_url.split("://", 1)[1]
        if "@" in rest:
            host = rest.split("@", 1)[1].rsplit("/", 1)[0]
        else:
            host = rest.rsplit("/", 1)[0]
        return f"postgresql://{user}:{password}@{host}/{name}"

    owner = dsn("caplog_owner", "ownerpw")
    app = dsn("caplog_app", "apppw")
    admin_db = dsn("postgres", "")
    # superuser URL has no password in the embedded server
    if "@" in postgres_url.split("://", 1)[1]:
        admin_db = postgres_url.rsplit("/", 1)[0] + "/" + name
    else:
        admin_db = postgres_url.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(admin_db, autocommit=True) as conn:
        conn.execute("ALTER SCHEMA public OWNER TO caplog_owner")
    migrate(owner)
    store = Store(app)
    try:
        yield {"store": store, "app": app, "owner": owner, "admin": admin_db, "name": name}
    finally:
        store.close()
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
