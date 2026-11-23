"""Command line: `olx-auto <command>`. Run `olx-auto -h` for help."""
import argparse
import getpass
import json
import shutil
import sys
import urllib.parse
from pathlib import Path

from . import drafting, listings, users
from .config import Settings
from .olx import OlxError


def _user(settings: Settings, name: str) -> users.User:
    user = users.get_user(settings.data_dir, name)
    if not user:
        sys.exit(f"No such user: {name}")
    return user


def _ask_password() -> str:
    pw = getpass.getpass("Password: ")
    if len(pw) < 8:
        sys.exit("Password must be at least 8 characters")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords don't match")
    return pw


def cmd_serve(settings: Settings, args) -> None:
    import uvicorn

    from .web.app import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port)


def cmd_add_user(settings: Settings, args) -> None:
    try:
        user = users.create_user(settings.data_dir, args.name, _ask_password(), args.display_name)
    except ValueError as e:
        sys.exit(str(e))
    if args.import_token:
        shutil.copy(args.import_token, user.token_file)
        user.token_file.chmod(0o600)
    print(f"Created {user.dir}")
    print(f"Edit {user.profile_file.name} and {user.style_guide_file.name} there.")


def cmd_set_password(settings: Settings, args) -> None:
    users.set_password(_user(settings, args.name), _ask_password())
    print("Password updated")


def cmd_login(settings: Settings, args) -> None:
    """CLI fallback for Connect OLX: paste the redirect URL by hand."""
    olx = _user(settings, args.name).olx(settings)
    print("Open this URL in a browser logged into the right OLX account and approve:\n")
    print(olx.authorize_url())
    raw = input("\nPaste the code (or the full redirect URL): ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("code", [raw])[0]
    olx.exchange_code(code)
    print(f"Saved token to {olx.token_file}")


def cmd_check(settings: Settings, args) -> None:
    """Smoke-test the OLX API with a user's tokens. Never prints secrets."""
    olx = _user(settings, args.name).olx(settings)
    checks = [
        ("client token", lambda: bool(olx.client_token())),
        ("categories (top level)", lambda: [(c["id"], c["name"]) for c in olx.categories()][:5]),
        ("currencies", lambda: olx.currencies()),
        ("users/me", lambda: {k: v for k, v in olx.me().items() if k in ("id", "name", "status")}),
        ("adverts", lambda: len(olx.adverts())),
    ]
    for name, fn in checks:
        try:
            print(f"OK   {name}: {str(fn())[:200]}")
        except (OlxError, RuntimeError, OSError) as e:
            print(f"FAIL {name}: {e}")


def cmd_draft(settings: Settings, args) -> None:
    """Run vision + agent on a listing in the foreground (same as the web app's background task)."""
    user = _user(settings, args.name)
    try:
        listing = listings.load(user, args.listing_id)
    except KeyError:
        sys.exit(f"No such listing: {args.listing_id}")
    if args.fresh:
        listing.update(observations=None, draft={}, locked=[], questions=[])
        listings.save(user, listing)
    listing = drafting.generate(settings, user, args.listing_id, instruction=args.instruction)
    if listing.get("ai_error"):
        sys.exit(f"Failed: {listing['ai_error']}")
    print(json.dumps({k: listing.get(k) for k in ("draft", "questions", "problems", "agent_stopped")},
                     ensure_ascii=False, indent=2))
    print(f"Cost so far: ${listings.total_cost(listing):.4f}  "
          f"(trace: {listings.trace_path(user, args.listing_id)})")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="olx-auto")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("add-user", help="create an app user")
    s.add_argument("name")
    s.add_argument("--display-name")
    s.add_argument("--import-token", type=Path, help="copy an existing OLX token file")
    s.set_defaults(fn=cmd_add_user)

    s = sub.add_parser("set-password", help="change a user's password")
    s.add_argument("name")
    s.set_defaults(fn=cmd_set_password)

    s = sub.add_parser("login", help="connect a user's OLX account (paste flow)")
    s.add_argument("name")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("check", help="smoke-test the OLX API for a user")
    s.add_argument("name")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("draft", help="(re)draft a listing with the AI (vision + agent)")
    s.add_argument("name")
    s.add_argument("listing_id")
    s.add_argument("--instruction", default="", help="refinement request, as from the card")
    s.add_argument("--fresh", action="store_true", help="drop observations, draft, locks and questions")
    s.set_defaults(fn=cmd_draft)

    args = p.parse_args(argv)
    args.fn(Settings.load(), args)
