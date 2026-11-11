"""Offline checks for stable account identity and durable consent history."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

import identity_store
from identity_store import GOOGLE_ISSUER, Identity, IdentityStore


VERSIONS = ("terms-1", "privacy-1", "legal-1")


def claims(subject="subject-1", **overrides):
    return {"iss": GOOGLE_ISSUER, "sub": subject, "email": "reader@example.com",
            "email_verified": True, **overrides}


def test_identity_is_stable_when_email_changes(tmp_path, monkeypatch):
    path = tmp_path / "accounts.sqlite3"
    store = IdentityStore(path)
    monkeypatch.setattr(identity_store, "utc_now", lambda: "2026-10-20T10:00:00+00:00")
    user_id = store.record_user(Identity.from_google_claims(claims()))
    store.accept_consent(user_id, VERSIONS)
    monkeypatch.setattr(identity_store, "utc_now", lambda: "2026-10-21T10:00:00+00:00")
    reopened = IdentityStore(path)
    assert reopened.record_user(Identity.from_google_claims(
        claims(iss="accounts.google.com", email="changed@example.com", email_verified=False),
    )) == user_id
    assert reopened.has_consent(user_id, VERSIONS)
    with reopened.connect() as conn:
        row = conn.execute("""
            SELECT issuer, subject, email, email_verified, first_seen_at_utc, last_seen_at_utc
            FROM users
        """).fetchone()
        assert row == (
            GOOGLE_ISSUER, "subject-1", "changed@example.com", 0,
            "2026-10-20T10:00:00+00:00", "2026-10-21T10:00:00+00:00",
        )


def test_shared_email_does_not_share_account_or_consent(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    first = store.record_user(Identity.from_google_claims(claims("first")))
    second = store.record_user(Identity.from_google_claims(claims("second")))
    assert first != second
    store.accept_consent(first, VERSIONS)
    assert not store.has_consent(second, VERSIONS)


def test_missing_optional_claims_and_reruns(tmp_path, monkeypatch):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    identity = Identity.from_google_claims({"iss": GOOGLE_ISSUER, "sub": "stable"})
    monkeypatch.setattr(identity_store, "utc_now", lambda: "first")
    user_id = store.record_user(identity)
    monkeypatch.setattr(identity_store, "utc_now", lambda: "rerun")
    assert store.record_user(identity, new_sign_in=False) == user_id
    with store.connect() as conn:
        assert conn.execute("""
            SELECT email, email_verified, first_seen_at_utc, last_seen_at_utc FROM users
        """).fetchone() == (None, None, "first", "first")


@pytest.mark.parametrize("overrides", [
    {"iss": "https://attacker.example"}, {"iss": None},
    {"sub": None}, {"sub": ""}, {"sub": "  "}, {"sub": 123},
])
def test_invalid_identity_is_rejected(overrides):
    with pytest.raises(ValueError):
        Identity.from_google_claims(claims(**overrides))


def test_untyped_verification_claim_is_not_treated_as_true():
    assert Identity.from_google_claims(claims(email_verified="false")).email_verified is None


@pytest.mark.parametrize("index", range(3))
def test_each_version_change_requires_new_consent_and_preserves_history(tmp_path, index):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    user_id = store.record_user(Identity.from_google_claims(claims()))
    store.accept_consent(user_id, VERSIONS)
    changed = list(VERSIONS)
    changed[index] = "version-2"
    assert not store.has_consent(user_id, changed)
    store.accept_consent(user_id, changed)
    reopened = IdentityStore(store.path)
    assert reopened.has_consent(user_id, changed)
    with reopened.connect() as conn:
        rows = conn.execute("""
            SELECT user_id, accepted_at_utc, terms_version, privacy_version, legal_version
            FROM legal_acknowledgments ORDER BY id
        """).fetchall()
        assert len(rows) == 2
        assert rows[0][2:] == VERSIONS
        assert rows[1][2:] == tuple(changed)
        assert all(row[1].endswith("+00:00") for row in rows)


def test_foreign_keys_are_enforced(tmp_path):
    store = IdentityStore(tmp_path / "accounts.sqlite3")
    with pytest.raises(sqlite3.IntegrityError):
        store.accept_consent(999, VERSIONS)


def test_concurrent_startup_and_sign_in_are_idempotent(tmp_path):
    path = tmp_path / "accounts.sqlite3"

    def sign_in(_):
        return IdentityStore(path).record_user(Identity.from_google_claims(claims()))

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(sign_in, range(8)))
    assert len(set(ids)) == 1
