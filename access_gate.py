"""Session-only legal cover sheet shared by every Streamlit page."""

import streamlit as st


OWNER_NAME = "William R. Camp"
CONTACT_EMAIL = "wmrcamp@gmail.com"
TERMS_VERSION = "2026-10-20"
PRIVACY_VERSION = "2026-10-21"
LEGAL_VERSION = "2026-10-20"
OPEN_APP_NOTICE = (
    "BY OPENING THE APP, YOU HAVE READ ALL THE USE WARNINGS "
    "AND ARE AGREEING TO THE TERMS OF SERVICE."
)


def show_legal_documents(*, expanded=False):
    with st.expander("Terms of Service / Terms of Use", expanded=expanded):
        st.markdown(f"""
### Terms of Use
**Version: {TERMS_VERSION} · Owner: {OWNER_NAME}**

You may use this application for its intended educational and research purposes.
No ownership of the software or related materials is transferred to you.
The application is provided as is, without warranties of accuracy or fitness for
your circumstances. Results are not investment, financial, tax, or legal advice.
Investing involves risk, including loss of principal; past performance does not
guarantee future results. Verify all information and consult qualified advisers.

Do not copy, modify, distribute, reverse engineer, or create derivative works of
the software or proprietary materials without prior written permission, except
where permitted by applicable law or third-party licenses.
Permissions and questions: {CONTACT_EMAIL}.
""")
    with st.expander("Privacy Notice", expanded=expanded):
        st.markdown(f"""
### Privacy Notice
**Version: {PRIVACY_VERSION} · Owner: {OWNER_NAME}**

You may enter an email address voluntarily before opening the app. It is
self-reported, not verified, and is not used to authenticate you. Leaving it
blank does not prevent access. The email and your acknowledgment of these notices
are held only in the current Streamlit session. This cover sheet does not save
them to an account database, send emails, or provide a permanent consent record.
A new session or a change to the legal document versions requires opening the
app through the cover sheet again.
Records collected under the previous sign-in flow are not deleted by this
change; contact the owner about access, retention, or deletion.
Credentials will be required at a future date.

The app does not require Google sign-in or collect a Google password.
The hosting provider may process connection information and keep service logs
under its own policies. This cover sheet does not record IP addresses or browser
user agents. Closing the session does not delete the hosting provider's logs.
For privacy questions or requests to access or delete your records, contact
{CONTACT_EMAIL}.
""")
    with st.expander("Legal Notice", expanded=expanded):
        st.markdown(f"""
### Legal Notice
**Version: {LEGAL_VERSION}**

© 2026 {OWNER_NAME}. All rights reserved.

The original source code, software logic, and related proprietary materials are
owned personally by {OWNER_NAME} and protected by copyright. Unauthorized copying,
modification, distribution, or reverse engineering is prohibited, subject to
applicable law. Third-party components remain subject to their respective licenses.
No rights are granted beyond use under the Terms of Use.
Permissions: {CONTACT_EMAIL}.
""")


def legal_footer():
    st.divider()
    st.caption(f"© 2026 {OWNER_NAME}. All rights reserved.")
    st.caption(
        "[Terms of Use](#terms-of-use) · [Privacy Notice](#privacy-notice) · "
        f"[Legal Notice](#legal-notice) · Contact: [{CONTACT_EMAIL}](mailto:{CONTACT_EMAIL})"
    )


def show_use_warnings():
    st.warning(
        "RESEARCH TOOL ONLY — NOT INVESTMENT ADVICE. This app, its analysis, "
        "portfolio results, and related materials are for educational and research "
        "purposes only. They are not investment, financial, tax, or legal advice, "
        "nor a recommendation or solicitation to buy, sell, or hold any security. "
        "Investing involves risk, including possible loss of principal. Past "
        "performance does not guarantee future results. Consult a qualified "
        "financial professional who understands your circumstances."
    )
    st.info(
        "AI-GENERATED CONTENT AND LIMITATIONS. This app and its code were "
        "developed with AI assistance and may contain errors, omissions, "
        "inaccuracies, or fabricated information. Data, calculations, and results "
        "may be incomplete or incorrect and have not been independently verified. "
        "Review and verify information before relying on it for any purpose."
    )


def require_access():
    with st.container(border=True):
        st.markdown("**CB Tax Exempt Portfolio Engine**")

    versions = (TERMS_VERSION, PRIVACY_VERSION, LEGAL_VERSION)
    if st.session_state.get("_cover_sheet_versions") == versions:
        st.caption(
            "Research tool only; not investment advice. Past performance does not "
            "guarantee future results. Full legal notices and use warnings:"
        )
        with st.expander("Legal notices and use warnings"):
            show_use_warnings()
            show_legal_documents()
        return

    st.title("Welcome — Please Read Before Opening")
    st.write("Review the use warnings, Terms of Service, Privacy Notice, and Legal Notice below.")
    with st.container(border=True):
        st.markdown(f"**Owner:** {OWNER_NAME}  \n**Contact:** {CONTACT_EMAIL}")
        show_use_warnings()
        show_legal_documents(expanded=True)
    with st.container(border=True):
        st.markdown(f"**{OPEN_APP_NOTICE}**")
        email = st.text_input(
            "Email address (optional)",
            help="Not verified. Kept only in this session; no email is sent.",
            key="_cover_sheet_email_input",
            max_chars=254,
        )
        st.caption("Clicking Open App confirms your agreement. If you do not agree, close this page.")
        if st.button("Open App", type="primary"):
            st.session_state["_cover_sheet_email"] = email.strip()
            st.session_state["_cover_sheet_versions"] = versions
            st.rerun()
    legal_footer()
    st.stop()
