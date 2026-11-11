"""SQLite and PostgreSQL persistence for accounts and append-only legal consent."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3

import psycopg
from psycopg.conninfo import conninfo_to_dict


GOOGLE_ISSUER = "https://accounts.google.com"


class IdentityStorageError(Exception):
    """A storage/configuration failure safe to display without connection details."""


@dataclass(frozen=True)
class Identity:
    provider: str
    issuer: str
    subject: str
    email: str | None
    email_verified: bool | None

    @classmethod
    def from_google_claims(cls, claims):
        issuer = claims.get("iss")
        subject = claims.get("sub")
        if issuer not in (GOOGLE_ISSUER, "accounts.google.com"):
            raise ValueError("A Google issuer is required.")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("A stable Google subject is required.")
        email = claims.get("email")
        verified = claims.get("email_verified")
        return cls(
            provider="google", issuer=GOOGLE_ISSUER, subject=subject,
            email=email if isinstance(email, str) else None,
            email_verified=verified if isinstance(verified, bool) else None,
        )


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class IdentityStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.storage_key = str(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    email TEXT,
                    email_verified INTEGER CHECK (email_verified IN (0, 1)),
                    first_seen_at_utc TEXT NOT NULL,
                    last_seen_at_utc TEXT NOT NULL,
                    UNIQUE (issuer, subject)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS legal_acknowledgments (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    accepted_at_utc TEXT NOT NULL,
                    terms_version TEXT NOT NULL,
                    privacy_version TEXT NOT NULL,
                    legal_version TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_legal_acks_user_versions
                ON legal_acknowledgments
                    (user_id, terms_version, privacy_version, legal_version)
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def record_user(self, identity, *, new_sign_in=True):
        now = utc_now()
        with self.connect() as conn:
            self._execute(conn, """
                INSERT INTO users (
                    provider, issuer, subject, email, email_verified,
                    first_seen_at_utc, last_seen_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (issuer, subject) DO UPDATE SET
                    email = excluded.email,
                    email_verified = excluded.email_verified,
                    last_seen_at_utc = CASE WHEN ? THEN excluded.last_seen_at_utc
                                           ELSE users.last_seen_at_utc END
            """, (
                identity.provider, identity.issuer, identity.subject, identity.email,
                None if identity.email_verified is None else int(identity.email_verified),
                now, now, new_sign_in,
            ))
            return self._execute(conn,
                "SELECT id FROM users WHERE issuer = ? AND subject = ?",
                (identity.issuer, identity.subject),
            ).fetchone()[0]

    def has_consent(self, user_id, versions):
        with self.connect() as conn:
            return self._execute(conn, """
                SELECT 1 FROM legal_acknowledgments
                WHERE user_id = ? AND terms_version = ?
                    AND privacy_version = ? AND legal_version = ?
                LIMIT 1
            """, (user_id, *versions)).fetchone() is not None

    def accept_consent(self, user_id, versions):
        with self.connect() as conn:
            self._execute(conn, """
                INSERT INTO legal_acknowledgments (
                    user_id, accepted_at_utc, terms_version, privacy_version, legal_version
                ) VALUES (?, ?, ?, ?, ?)
            """, (user_id, utc_now(), *versions))

    @staticmethod
    def _execute(conn, statement, parameters):
        return conn.execute(statement, parameters)


class PostgresIdentityStore(IdentityStore):
    """Use a pre-provisioned schema with a restricted, non-owner runtime role."""

    def __init__(self, database_url):
        try:
            if not isinstance(database_url, str) or not database_url.startswith(
                ("postgresql://", "postgres://"),
            ):
                raise ValueError
            options = conninfo_to_dict(database_url)
            if not options.get("host") or not options.get("dbname") or not options.get("user"):
                raise ValueError
            if options.get("sslmode", "verify-full") not in ("require", "verify-ca", "verify-full"):
                raise ValueError
        except (ValueError, psycopg.Error):
            raise IdentityStorageError("Invalid PostgreSQL configuration.") from None
        self._database_url = database_url
        self._sslrootcert = options.get("sslrootcert", "system")
        self.storage_key = "postgresql:" + hashlib.sha256(database_url.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("""
                SELECT id, provider, issuer, subject, email, email_verified,
                       first_seen_at_utc, last_seen_at_utc FROM users LIMIT 0
            """)
            conn.execute("""
                SELECT id, user_id, accepted_at_utc, terms_version, privacy_version,
                       legal_version FROM legal_acknowledgments LIMIT 0
            """)

    @contextmanager
    def connect(self):
        try:
            with psycopg.connect(
                self._database_url, sslmode="verify-full", sslrootcert=self._sslrootcert,
                connect_timeout=10,
            ) as conn:
                conn.execute("SET search_path TO cb_identity, pg_catalog")
                conn.execute("SET LOCAL statement_timeout = '15s'")
                conn.execute("SET LOCAL lock_timeout = '5s'")
                yield conn
        except psycopg.Error:
            raise IdentityStorageError("PostgreSQL account storage is unavailable.") from None

    @staticmethod
    def _execute(conn, statement, parameters):
        # Only fixed application SQL is translated; values remain bound parameters.
        return conn.execute(statement.replace("?", "%s"), parameters)


def create_identity_store(path, *, backend=None, database_url=None):
    if backend is None:
        backend = "postgresql" if database_url is not None else "sqlite"
    if backend == "postgresql":
        return PostgresIdentityStore(database_url)
    if backend == "sqlite" and database_url is None:
        return IdentityStore(path)
    raise IdentityStorageError("Invalid identity storage backend configuration.")
