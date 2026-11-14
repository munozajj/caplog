"""Database objects.

The table owner runs this. The application role is granted SELECT and INSERT
only. Triggers reject UPDATE, DELETE, and TRUNCATE even for the owner.
The insert trigger also refuses a row that does not continue the hash chain.

Lock id 847291 is arbitrary. It only has to stay the same for every insert
so two sessions cannot take the same entry number.
"""

GENESIS_HASH = "0" * 64

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS records (
        seq        bigint PRIMARY KEY,
        kind       text NOT NULL,
        created_at text NOT NULL,
        first_name text NOT NULL,
        last_name  text NOT NULL,
        ip         text NOT NULL,
        body       text NOT NULL,
        meta       text NOT NULL,
        prev_hash  text NOT NULL,
        hash       text NOT NULL UNIQUE
    )
    """,
    """
    CREATE OR REPLACE FUNCTION caplog_reject_update()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION 'caplog: records are permanent and cannot be modified';
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION caplog_reject_delete()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION 'caplog: records are permanent and cannot be deleted';
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION caplog_reject_truncate()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        RAISE EXCEPTION 'caplog: records are permanent and cannot be truncated';
    END;
    $$
    """,
    f"""
    CREATE OR REPLACE FUNCTION caplog_insert_check()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
        expected_seq bigint;
        expected_prev text;
    BEGIN
        PERFORM pg_advisory_xact_lock(847291);
        SELECT COALESCE(MAX(seq), -1) + 1 INTO expected_seq FROM records;
        IF NEW.seq IS DISTINCT FROM expected_seq THEN
            RAISE EXCEPTION 'caplog: entry number must be the next in sequence';
        END IF;
        SELECT hash INTO expected_prev FROM records ORDER BY seq DESC LIMIT 1;
        IF expected_prev IS NULL THEN
            expected_prev := '{GENESIS_HASH}';
        END IF;
        IF NEW.prev_hash IS DISTINCT FROM expected_prev THEN
            RAISE EXCEPTION 'caplog: previous hash must match the last record';
        END IF;
        IF (NEW.seq = 0) IS DISTINCT FROM (NEW.kind = 'genesis') THEN
            RAISE EXCEPTION 'caplog: only record 0 may be the title';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    "DROP TRIGGER IF EXISTS records_no_update ON records",
    """
    CREATE TRIGGER records_no_update
    BEFORE UPDATE ON records
    FOR EACH ROW
    EXECUTE FUNCTION caplog_reject_update()
    """,
    "DROP TRIGGER IF EXISTS records_no_delete ON records",
    """
    CREATE TRIGGER records_no_delete
    BEFORE DELETE ON records
    FOR EACH ROW
    EXECUTE FUNCTION caplog_reject_delete()
    """,
    "DROP TRIGGER IF EXISTS records_no_truncate ON records",
    """
    CREATE TRIGGER records_no_truncate
    BEFORE TRUNCATE ON records
    FOR EACH STATEMENT
    EXECUTE FUNCTION caplog_reject_truncate()
    """,
    "DROP TRIGGER IF EXISTS records_insert_check ON records",
    """
    CREATE TRIGGER records_insert_check
    BEFORE INSERT ON records
    FOR EACH ROW
    EXECUTE FUNCTION caplog_insert_check()
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        username      text NOT NULL UNIQUE,
        first_name    text NOT NULL,
        last_name     text NOT NULL,
        password_hash text NOT NULL,
        role          text NOT NULL,
        enabled       boolean NOT NULL DEFAULT true,
        is_master     boolean NOT NULL DEFAULT false,
        display_json  text NOT NULL DEFAULT '',
        locked_until  text NOT NULL DEFAULT '',
        created_at    text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token      text PRIMARY KEY,
        user_id    bigint NOT NULL,
        expires_at text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key   text PRIMARY KEY,
        value text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
        id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        username text NOT NULL,
        ip       text NOT NULL,
        at       text NOT NULL,
        ok       boolean NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS post_hits (
        id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        bucket text NOT NULL,
        at     text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS correction_links (
        new_seq bigint PRIMARY KEY,
        old_seq bigint NOT NULL
    )
    """,
]
