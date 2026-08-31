"""One-time Schwab OAuth login for the CLI importer.

Schwab's callback is usually HTTPS (`https://127.0.0.1:8182`), which a
plain HTTP listener cannot satisfy. The reliable path is: open the printed
URL, complete login, then paste the full redirected address (it contains
`?code=`).

Usage (from `backend/`):

    python -m scripts.schwab_auth
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app.config import settings
from scripts.schwab_tokens import (
    DEFAULT_TOKEN_PATH,
    SchwabAuthError,
    authorization_url,
    exchange_code,
    save_tokens,
)


def _code_from_redirect(raw: str) -> str:
    text = raw.strip().strip("'\"")
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    codes = query.get("code")
    if codes:
        return unquote(codes[0])
    # User pasted just the code.
    if text and "://" not in text and "=" not in text:
        return unquote(text)
    raise SchwabAuthError("Could not find ?code= in what you pasted. Paste the full redirect URL.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tokens-file",
        default=str(DEFAULT_TOKEN_PATH),
        help="Where to write tokens (default: backend/.schwab_tokens.json)",
    )
    args = parser.parse_args(argv)

    client_id = settings.schwab_client_id
    client_secret = settings.schwab_client_secret
    redirect_uri = settings.schwab_redirect_uri
    if not client_id or not client_secret:
        print(
            "Set SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET in backend/.env first.",
            file=sys.stderr,
        )
        return 1

    url = authorization_url(client_id, redirect_uri)
    print("Open this URL in a browser and sign in to Schwab:\n")
    print(url)
    print(
        "\nAfter login you will land on the app callback URL (it may fail to "
        "load — that is expected). Copy the full address bar URL and paste it here."
    )
    try:
        pasted = input("Redirect URL: ")
    except EOFError:
        print("No input received.", file=sys.stderr)
        return 1

    try:
        code = _code_from_redirect(pasted)
        tokens = exchange_code(
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
        save_tokens(tokens, path=Path(args.tokens_file))
    except SchwabAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Saved tokens to {args.tokens_file} (file is gitignored).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
