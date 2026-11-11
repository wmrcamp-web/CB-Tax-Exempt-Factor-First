"""PostgreSQL adapter checks; real-server tests require a disposable test database."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import secrets
from unittest.mock import MagicMock
from urllib.parse import quote, urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql
import pytest

import identity_store
from identity_store import (
    Identity, IdentityStorageError, IdentityStore, PostgresIdentityStore, create_identity_store,
)
from test_identity_store import VERSIONS, claims


SCHEMA = Path(__file__).resolve().parent / "docs" / "identity_postgres.sql"


def test_sqlite_remains_default(tmp_path):
    store = create_identity_store(tmp_path / "local.sqlite3")
    assert type(store) is IdentityStore
    assert store.storage_key == str(store.path)


@pytest.mark.parametrize("backend,url", [
    ("postgresql", None), ("postgresql", ""), ("postgresql", "not-a-url"),
    ("postgresql", "sqlite:///data"), ("postgresql", "postgresql://localhost/db"),
    ("postgresql", "postgresql://app@localhost/db?sslmode=disable"),
    ("postgresql", "postgresql://app@localhost/db?sslmode=prefer"),
    ("postgresql", "postgresql://app@localhost/db?sslmode=allow"),
    ("sqlite", "postgresql://app@localhost/db"),
    ("unknown", None),
])
def test_invalid_configuration_never_falls_back_to_sqlite(tmp_path, backend, url):
    path = tmp_path / "unused.sqlite3"
    with pytest.raises(IdentityStorageError):
        create_identity_store(path, backend=backend, database_url=url)
    assert not path.exists()


def test_postgres_enforces_verified_tls_and_hides_url(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", connect)
    url = "postgresql://app@localhost/accounts?sslmode=require"
    store = PostgresIdentityStore(url)
    connect.assert_called_once_with(
        url, sslmode="verify-full", sslrootcert="system", connect_timeout=10,
    )
    assert url not in store.storage_key
    assert "localhost" not in store.storage_key
    assert store.storage_key.startswith("postgresql:")
    conn = connect.return_value.__enter__.return_value
    statements = [call.args[0] for call in conn.execute.call_args_list]
    assert "SET search_path TO cb_identity, pg_catalog" in statements
    assert not any("CREATE" in statement for statement in statements)


def test_driver_failure_is_sanitized(monkeypatch):
    def fail(*args, **kwargs):
        raise psycopg.OperationalError("private connection details")

    monkeypatch.setattr(psycopg, "connect", fail)
    with pytest.raises(IdentityStorageError) as exc:
        PostgresIdentityStore("postgresql://app@localhost/accounts")
    assert "private" not in str(exc.value)
    assert exc.value.__suppress_context__


def test_postgres_values_are_bound_parameters(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", connect)
    store = PostgresIdentityStore("postgresql://app@localhost/accounts")
    identity = Identity.from_google_claims(claims(subject="'); DROP TABLE users; --"))
    store.record_user(identity)
    conn = connect.return_value.__enter__.return_value
    insert = next(call for call in conn.execute.call_args_list if "INSERT INTO users" in call.args[0])
    assert "%s" in insert.args[0]
    assert "?" not in insert.args[0]
    assert identity.subject not in insert.args[0]
    assert insert.args[1][2] == identity.subject
    assert insert.args[1][4] == 1


@pytest.fixture(scope="module")
def postgres_url():
    """Provision only an explicitly supplied disposable database, never the live Neon DB."""
    admin_url = os.environ.get("CB_TEST_POSTGRES_URL")
    if not admin_url:
        pytest.skip("Set CB_TEST_POSTGRES_URL to a disposable PostgreSQL database.")
    role = "identity_test_" + uuid.uuid4().hex
    password = secrets.token_urlsafe(32)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
        conn.execute(SCHEMA.read_text(encoding="utf-8"))  # Provisioning is idempotent.
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password),
        ))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA cb_identity TO {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL(
            "GRANT SELECT, INSERT, UPDATE ON cb_identity.users TO {}"
        ).format(sql.Identifier(role)))
        conn.execute(sql.SQL(
            "GRANT SELECT, INSERT ON cb_identity.legal_acknowledgments TO {}"
        ).format(sql.Identifier(role)))
        conn.execute(sql.SQL(
            "GRANT USAGE ON ALL SEQUENCES IN SCHEMA cb_identity TO {}"
        ).format(sql.Identifier(role)))
    parsed = urlsplit(admin_url)
    hostname = parsed.netloc.rsplit("@", 1)[-1]
    runtime_url = urlunsplit(parsed._replace(
        netloc=f"{quote(role)}:{quote(password)}@{hostname}",
    ))
    try:
        yield runtime_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture
def pg_account(postgres_url):
    store = PostgresIdentityStore(postgres_url)
    identity = Identity.from_google_claims(claims(subject=uuid.uuid4().hex))
    return store, identity


def test_postgres_durable_identity_consent_and_version_history(pg_account, postgres_url, monkeypatch):
    store, identity = pg_account
    monkeypatch.setattr(identity_store, "utc_now", lambda: "2026-10-20T10:00:00+00:00")
    user_id = store.record_user(identity)
    store.accept_consent(user_id, VERSIONS)
    monkeypatch.setattr(identity_store, "utc_now", lambda: "2026-10-21T10:00:00+00:00")
    changed_email = Identity.from_google_claims(claims(subject=identity.subject, email="new@example.com"))
    reopened = PostgresIdentityStore(postgres_url)
    assert reopened.record_user(changed_email) == user_id
    assert reopened.has_consent(user_id, VERSIONS)
    for index in range(3):
        updated = list(VERSIONS)
        updated[index] = "next-version"
        assert not reopened.has_consent(user_id, updated)
        reopened.accept_consent(user_id, updated)
        assert PostgresIdentityStore(postgres_url).has_consent(user_id, updated)
    with reopened.connect() as conn:
        row = conn.execute("""
            SELECT email, email_verified, first_seen_at_utc, last_seen_at_utc
            FROM users WHERE id = %s
        """, (user_id,)).fetchone()
        assert row == ("new@example.com", 1, "2026-10-20T10:00:00+00:00",
                       "2026-10-21T10:00:00+00:00")
        assert conn.execute(
            "SELECT count(*) FROM legal_acknowledgments WHERE user_id = %s", (user_id,),
        ).fetchone()[0] == 4


def test_postgres_optional_claims_and_reruns(pg_account, monkeypatch):
    store, identity = pg_account
    identity = Identity.from_google_claims({"iss": identity.issuer, "sub": identity.subject})
    monkeypatch.setattr(identity_store, "utc_now", lambda: "first")
    user_id = store.record_user(identity)
    monkeypatch.setattr(identity_store, "utc_now", lambda: "rerun")
    store.record_user(identity, new_sign_in=False)
    with store.connect() as conn:
        assert conn.execute("""
            SELECT email, email_verified, first_seen_at_utc, last_seen_at_utc
            FROM users WHERE id = %s
        """, (user_id,)).fetchone() == (None, None, "first", "first")


def test_postgres_shared_email_does_not_share_consent(pg_account):
    store, identity = pg_account
    first = store.record_user(identity)
    second = store.record_user(Identity.from_google_claims(claims(subject=uuid.uuid4().hex)))
    store.accept_consent(first, VERSIONS)
    assert first != second
    assert not store.has_consent(second, VERSIONS)


def test_postgres_concurrent_startup_and_upserts(pg_account, postgres_url):
    _, identity = pg_account

    def sign_in(_):
        return PostgresIdentityStore(postgres_url).record_user(identity)

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(sign_in, range(8)))
    assert len(set(ids)) == 1


def test_postgres_constraints_and_restricted_role(pg_account):
    store, identity = pg_account
    user_id = store.record_user(identity)
    store.accept_consent(user_id, VERSIONS)
    with pytest.raises(IdentityStorageError):
        store.accept_consent(-1, VERSIONS)
    for statement in (
        "DELETE FROM legal_acknowledgments",
        "UPDATE legal_acknowledgments SET legal_version = 'tampered'",
        "DROP TABLE users",
        "CREATE TABLE cb_identity.forbidden (id integer)",
    ):
        with pytest.raises(IdentityStorageError):
            with store.connect() as conn:
                conn.execute(statement)
    assert store.has_consent(user_id, VERSIONS)


def test_postgres_rolls_back_failed_transactions(pg_account):
    store, identity = pg_account
    user_id = store.record_user(identity)
    with pytest.raises(RuntimeError):
        with store.connect() as conn:
            conn.execute("""
                INSERT INTO legal_acknowledgments
                    (user_id, accepted_at_utc, terms_version, privacy_version, legal_version)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, "never committed", *VERSIONS))
            raise RuntimeError("abort transaction")
    assert not store.has_consent(user_id, VERSIONS)
