"""Unit tests for Schwab OAuth helpers that do not call the live API."""

from scripts.schwab_auth import _code_from_redirect
from scripts.schwab_tokens import authorization_url


def test_authorization_url_includes_client_and_redirect():
    url = authorization_url("app-key", "https://127.0.0.1:8182")
    assert "client_id=app-key" in url
    assert "response_type=code" in url
    assert "redirect_uri=" in url


def test_code_from_full_redirect_url():
    code = _code_from_redirect("https://127.0.0.1:8182/?code=ABC%40xyz&session=1")
    assert code == "ABC@xyz"


def test_code_from_bare_token():
    assert _code_from_redirect("plaincode") == "plaincode"
