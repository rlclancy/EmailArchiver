"""Command-line entry point."""

from __future__ import annotations

import argparse
import getpass
import imaplib
import os
import socket
import ssl
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .folders import parse_list_response, select_folders
from .headers import is_valid_address, normalize_address
from .preview import run_preview
from .report import render_folders, render_preview
from .safe_imap import MailboxSafetyError, connect

PASSWORD_ENV_VAR = "EMAIL_ARCHIVER_APP_PASSWORD"

APP_PASSWORD_HELP = """\
Login failed. Not retrying, to avoid locking the account.

Cox addresses are now hosted by Yahoo Mail and usually need an app password:
  1. Sign in at https://login.yahoo.com with your full @cox.net address.
  2. Open Account Info -> Account Security -> Generate app password.
  3. Run this tool again and paste that password at the prompt
     (or set it in $EMAIL_ARCHIVER_APP_PASSWORD).
"""

ENV_PASSWORD_HELP = """\
The password came from $EMAIL_ARCHIVER_APP_PASSWORD. Check that it holds the
current Yahoo app password (not your regular password), or unset it to be
prompted instead.
"""


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="email-archiver",
        description="Archive email from chosen senders in a Cox/Yahoo mailbox to yearly PDFs.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("preview", help="Phase 1: read-only count of matching messages by year and folder.")
    pv.add_argument("--config", type=Path, help="TOML config file (default ~/.config/email-archiver/config.toml)")
    pv.add_argument("--user", help="Mailbox login, e.g. you@cox.net")
    pv.add_argument("--sender", action="append", default=[], metavar="ADDRESS",
                    help="Sender address to match exactly; repeat for several")
    pv.add_argument("--folders", help="Comma-separated folder names to search (overrides Spam/Trash exclusion)")
    pv.add_argument("--start", type=_parse_date, help="First date to include, YYYY-MM-DD")
    pv.add_argument("--end", type=_parse_date, help="Last date to include, YYYY-MM-DD")
    pv.add_argument("--list-folders", action="store_true", help="Only list folders and which would be searched")
    pv.add_argument("--host", help="IMAP host (default imap.mail.yahoo.com)")
    pv.add_argument("--port", type=int, help="IMAP port (default 993)")
    return p


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _prompt_senders() -> list[str]:
    raw = input("Sender address(es), comma-separated: ")
    return [s for s in (x.strip() for x in raw.split(",")) if s]


def get_password(user: str, environ=None, prompt=getpass.getpass) -> tuple[str, str]:
    """Return (password, source). Uses $EMAIL_ARCHIVER_APP_PASSWORD if set and
    non-empty; otherwise prompts with no echo. source is "env" or "prompt"."""
    environ = os.environ if environ is None else environ
    value = environ.get(PASSWORD_ENV_VAR, "").strip()
    if value:
        return value, "env"
    return prompt(f"App password for {user} (input hidden): "), "prompt"


def cmd_preview(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config, required=args.config is not None)
    except ConfigError as exc:
        _err(f"Config error: {exc}")
        return 2

    host = args.host or cfg.host
    port = args.port or cfg.port
    if args.start and args.end and args.start > args.end:
        _err("--start must be on or before --end")
        return 2

    user = args.user or cfg.username or input("Mailbox login (e.g. you@cox.net): ").strip()
    if not is_valid_address(user):
        _err(f"Not a valid email address: {user!r}")
        return 2

    senders: list[str] = []
    if not args.list_folders:
        senders = args.sender or cfg.senders or _prompt_senders()
        bad = [s for s in senders if not is_valid_address(s)]
        if bad or not senders:
            _err("Invalid or missing sender address(es): " + (", ".join(bad) or "none given"))
            return 2
        senders = list(dict.fromkeys(normalize_address(s) for s in senders))

    include = ([f.strip() for f in args.folders.split(",") if f.strip()]
               if args.folders else cfg.folders)

    password, pw_source = get_password(user)
    if not password:
        _err("No password entered.")
        return 2
    if pw_source == "env":
        print(f"Using app password from ${PASSWORD_ENV_VAR}.", file=sys.stderr)

    conn = None
    try:
        print(f"Connecting to {host}:{port} over TLS ...", file=sys.stderr)
        conn = connect(host, port, timeout=cfg.timeout)
        try:
            conn.login(user, password)
        except imaplib.IMAP4.error:
            _err(APP_PASSWORD_HELP)
            if pw_source == "env":
                _err(ENV_PASSWORD_HELP)
            return 3
        finally:
            del password

        typ, data = conn.list()
        if typ != "OK":
            _err("Could not list folders.")
            return 4
        try:
            selected, excluded = select_folders(parse_list_response(data), include, cfg.exclude_folders)
        except ValueError as exc:
            _err(str(exc))
            return 2

        if args.list_folders:
            print(render_folders(selected, excluded))
            return 0
        if not selected:
            _err("No folders to search.")
            return 2

        result = run_preview(
            conn, selected, excluded, senders, args.start, args.end,
            max_messages=cfg.max_messages_per_pdf,
            max_bytes=cfg.max_mb_per_pdf * 1024 * 1024,
            progress=lambda m: print(m, file=sys.stderr),
        )
        print(render_preview(result, user, host))
        if not result.messages:
            print("\nNo matching messages found. Nothing would be exported.")
        else:
            print("\nNext: review these counts. Export (Phase 2+) is not implemented yet.")
        return 0
    except ssl.SSLCertVerificationError as exc:
        _err(f"TLS certificate verification failed; refusing to connect. ({exc.verify_message})")
        return 4
    except (socket.gaierror, socket.timeout, ConnectionError, OSError, imaplib.IMAP4.abort) as exc:
        _err(f"Network error: {exc.__class__.__name__}: {exc}\n"
             "Nothing was written and the mailbox is unchanged. Re-run to try again.")
        return 4
    except MailboxSafetyError as exc:
        _err(f"Internal safety stop (no change made to mailbox): {exc}")
        return 5
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "preview":
            return cmd_preview(args)
    except KeyboardInterrupt:
        _err("\nCanceled. The mailbox is unchanged and no files were written.")
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
