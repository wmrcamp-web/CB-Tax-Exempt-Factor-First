"""Offline Streamlit tests for the simple, session-only legal cover sheet."""

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import access_gate


ROOT = Path(__file__).resolve().parent
GATED_APP = """
import streamlit as st
from access_gate import require_access
st.set_page_config(page_title="Access test")
require_access()
st.title("Protected workspace")
"""


def button(page, label):
    return next(item for item in page.button if item.label == label)


def protected(page):
    return any(item.value == "Protected workspace" for item in page.title)


@pytest.fixture(autouse=True)
def no_external_setup(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The cover sheet must not use authentication or database configuration.")

    monkeypatch.setattr(st, "login", forbidden)
    monkeypatch.setattr(st, "logout", forbidden)
    monkeypatch.setattr(st, "secrets", None)
    monkeypatch.setattr("identity_store.create_identity_store", forbidden)
    monkeypatch.setenv("CB_IDENTITY_BACKEND", "postgresql")
    monkeypatch.setenv("CB_IDENTITY_DATABASE_URL", "invalid-unused-configuration")


def test_cover_sheet_shows_all_notices_and_blocks_features():
    page = AppTest.from_string(GATED_APP).run()
    assert not page.exception
    assert not protected(page)
    assert not page.checkbox
    assert [item.label for item in page.expander] == [
        "Terms of Service / Terms of Use", "Privacy Notice", "Legal Notice",
    ]
    assert all(item.proto.expanded for item in page.expander)
    assert page.warning
    assert page.info
    text = "\n".join(item.value for item in page.markdown)
    assert access_gate.OPEN_APP_NOTICE in text
    assert "William R. Camp" in text
    assert "wmrcamp@gmail.com" in text
    assert "session" in text
    assert "Credentials will be required at a future date." in text
    assert not button(page, "Open App").disabled


def test_open_app_without_email_and_reruns():
    page = AppTest.from_string(GATED_APP).run()
    button(page, "Open App").click().run()
    assert not page.exception
    assert protected(page)
    assert page.session_state["_cover_sheet_email"] == ""
    page.run()
    assert protected(page)
    assert not page.text_input
    assert any("Research tool only; not investment advice." in item.value for item in page.caption)
    assert any(item.label == "Legal notices and use warnings" for item in page.expander)


def test_optional_email_is_session_only_and_not_interpreted_as_markup():
    page = AppTest.from_string(GATED_APP).run()
    page.text_input[0].input(" reader@example.com ").run()
    assert not protected(page)
    button(page, "Open App").click().run()
    assert protected(page)
    assert page.session_state["_cover_sheet_email"] == "reader@example.com"
    fresh = AppTest.from_string(GATED_APP).run()
    assert not protected(fresh)
    assert fresh.text_input[0].value == ""
    assert all("reader@example.com" not in item.value for item in page.markdown)


def test_unverified_email_is_not_an_access_credential():
    page = AppTest.from_string(GATED_APP).run()
    page.text_input[0].input("<script>alert(1)</script>").run()
    button(page, "Open App").click().run()
    assert not page.exception
    assert protected(page)
    assert all("<script>" not in item.value for item in page.markdown)


@pytest.mark.parametrize("constant", ["TERMS_VERSION", "PRIVACY_VERSION", "LEGAL_VERSION"])
def test_document_version_changes_reopen_cover_sheet(monkeypatch, constant):
    page = AppTest.from_string(GATED_APP).run()
    button(page, "Open App").click().run()
    assert protected(page)
    monkeypatch.setattr(access_gate, constant, "new-version")
    page.run()
    assert not protected(page)
    button(page, "Open App").click().run()
    assert protected(page)


@pytest.mark.parametrize("entry", [
    "app.py", "cb_taxable_portfolio_app.py",
    "pages/1_Backtest_Results.py", "pages/2_How_It_Works.py",
])
def test_direct_navigation_always_shows_cover_before_features(monkeypatch, entry):
    def forbidden(*args, **kwargs):
        pytest.fail("App data was accessed before Open App.")

    monkeypatch.setattr("data_provider.DEFAULT_PROVIDER.get_benchmark_return", forbidden)
    monkeypatch.setattr("backtest_exports.read_summary", forbidden)
    page = AppTest.from_file(str(ROOT / entry)).run(timeout=20)
    assert not page.exception
    assert not page.get("download_button")
    assert not page.sidebar.date_input
    assert button(page, "Open App")


def test_backtest_page_opens_without_authentication_or_database(monkeypatch):
    def no_summary(*args):
        raise ValueError("Offline test has no saved results")

    monkeypatch.setattr("backtest_exports.read_summary", no_summary)
    page = AppTest.from_file(str(ROOT / "pages" / "1_Backtest_Results.py")).run()
    button(page, "Open App").click().run()
    assert not page.exception
    assert any(item.value == "Historical Backtest Results" for item in page.title)


def test_cover_sheet_acceptance_carries_across_pages():
    app = """
import streamlit as st
from access_gate import require_access
st.set_page_config(page_title="Navigation test")
page = st.sidebar.radio("Page", ["Dashboard", "Other page"])
require_access()
st.title(page)
"""
    page = AppTest.from_string(app).run()
    button(page, "Open App").click().run()
    page.sidebar.radio[0].set_value("Other page").run()
    assert not page.exception
    assert any(item.value == "Other page" for item in page.title)
    assert not page.text_input
