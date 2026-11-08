"""Command-line entry point."""

from __future__ import annotations

import argparse
import getpass
import imaplib
import os
import socket
import ssl
import sys
from datetime import date, datetime
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .fetch import (
    MAX_SAMPLE, OutputDirError, choose_sample, fetch_sample, inside_git_repo, picks_from_specs,
    prepare_output_dir, write_run_metadata,
)
from .folders import parse_list_response, select_folders
from .headers import is_valid_address, normalize_address
from .preview import run_preview
from .report import (
    render_folders, render_message_list, render_preview, render_sample_plan,
    render_sample_result,
)
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


class _Exit(Exception):
    """Stop with an exit code after a message has been printed."""

    def __init__(self, code: int):
        self.code = code


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")


def _sample_count(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}")
    if not 1 <= n <= MAX_SAMPLE:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_SAMPLE}")
    return n


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", type=Path, help="TOML config file (default ~/.config/email-archiver/config.toml)")
    p.add_argument("--user", help="Mailbox login, e.g. you@cox.net")
    p.add_argument("--sender", action="append", default=[], metavar="ADDRESS",
                   help="Sender address to match exactly; repeat for several")
    p.add_argument("--folders", help="Comma-separated folder names to search (overrides Spam/Trash exclusion)")
    p.add_argument("--start", type=_parse_date, help="First date to include, YYYY-MM-DD")
    p.add_argument("--end", type=_parse_date, help="Last date to include, YYYY-MM-DD")
    p.add_argument("--host", help="IMAP host (default imap.mail.yahoo.com)")
    p.add_argument("--port", type=int, help="IMAP port (default 993)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="email-archiver",
        description="Archive email from chosen senders in a Cox/Yahoo mailbox to yearly PDFs.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("preview", help="Phase 1: read-only count of matching messages by year and folder.")
    _add_common(pv)
    pv.add_argument("--list-folders", action="store_true", help="Only list folders and which would be searched")

    sm = sub.add_parser("sample", help="Phase 2: fetch a few matching messages read-only and save them as .eml.")
    _add_common(sm)
    sm.add_argument("--output-dir", type=Path,
                    help="Archive folder (outside any git repo); samples go in <dir>/_samples/<timestamp>/")
    sm.add_argument("--count", type=_sample_count, default=5,
                    help=f"How many messages to pick automatically (default 5, max {MAX_SAMPLE})")
    sm.add_argument("--uid", action="append", default=[], metavar="FOLDER:UID",
                    help="Fetch this exact message instead of an automatic pick; repeat for several")
    sm.add_argument("--list", action="store_true",
                    help="List every matching message with its FOLDER:UID and exit (fetches nothing)")
    return p


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr)


def _prompt_senders() -> list[str]:
    raw = input("Sender address(es), comma-separated: ")
    return [s for s in (x.strip() for x in raw.split(",")) if s]


def get_password(user: str, environ=None, prompt=None) -> tuple[str, str]:
    """Return (password, source). Uses $EMAIL_ARCHIVER_APP_PASSWORD if set and
    non-empty; otherwise prompts with no echo. source is "env" or "prompt"."""
    environ = os.environ if environ is None else environ
    value = environ.get(PASSWORD_ENV_VAR, "").strip()
    if value:
        return value, "env"
    prompt = prompt or getpass.getpass
    return prompt(f"App password for {user} (input hidden): "), "prompt"


def _settings(args: argparse.Namespace, need_senders: bool):
    """Resolve config + flags. Returns (cfg, host, port, user, senders, include)."""
    try:
        cfg = load_config(args.config, required=args.config is not None)
    except ConfigError as exc:
        _err(f"Config error: {exc}")
        raise _Exit(2)

    if args.start and args.end and args.start > args.end:
        _err("--start must be on or before --end")
        raise _Exit(2)

    user = args.user or cfg.username or input("Mailbox login (e.g. you@cox.net): ").strip()
    if not is_valid_address(user):
        _err(f"Not a valid email address: {user!r}")
        raise _Exit(2)

    senders: list[str] = []
    if need_senders:
        senders = args.sender or cfg.senders or _prompt_senders()
        bad = [s for s in senders if not is_valid_address(s)]
        if bad or not senders:
            _err("Invalid or missing sender address(es): " + (", ".join(bad) or "none given"))
            raise _Exit(2)
        senders = list(dict.fromkeys(normalize_address(s) for s in senders))

    include = ([f.strip() for f in args.folders.split(",") if f.strip()]
               if args.folders else cfg.folders)
    return cfg, args.host or cfg.host, args.port or cfg.port, user, senders, include


def _read_password(user: str) -> tuple[str, str]:
    password, source = get_password(user)
    if not password:
        _err("No password entered.")
        raise _Exit(2)
    if source == "env":
        _progress(f"Using app password from ${PASSWORD_ENV_VAR}.")
    return password, source


def _login(conn, user: str, password: str, source: str) -> None:
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error:
        _err(APP_PASSWORD_HELP)
        if source == "env":
            _err(ENV_PASSWORD_HELP)
        raise _Exit(3)


def _folders(conn, cfg, include):
    typ, data = conn.list()
    if typ != "OK":
        _err("Could not list folders.")
        raise _Exit(4)
    try:
        return select_folders(parse_list_response(data), include, cfg.exclude_folders)
    except ValueError as exc:
        _err(str(exc))
        raise _Exit(2)


def _with_mailbox(args: argparse.Namespace, need_senders: bool, body) -> int:
    """Resolve settings, connect, log in, pick folders, then run ``body``.

    ``body(conn, cfg, host, user, senders, selected, excluded)`` returns an exit code.
    Handles network/TLS/safety errors uniformly and always logs out.
    """
    try:
        cfg, host, port, user, senders, include = _settings(args, need_senders)
        # Get the password before connecting, so a slow prompt can't time out the socket.
        password, source = _read_password(user)
    except _Exit as e:
        return e.code

    conn = None
    try:
        _progress(f"Connecting to {host}:{port} over TLS ...")
        conn = connect(host, port, timeout=cfg.timeout)
        try:
            _login(conn, user, password, source)
        finally:
            del password
        selected, excluded = _folders(conn, cfg, include)
        return body(conn, cfg, host, user, senders, selected, excluded)
    except _Exit as e:
        return e.code
    except ssl.SSLCertVerificationError as exc:
        _err(f"TLS certificate verification failed; refusing to connect. ({exc.verify_message})")
        return 4
    except (socket.gaierror, socket.timeout, ConnectionError, imaplib.IMAP4.abort) as exc:
        _err(f"Network error: {exc.__class__.__name__}: {exc}\n"
             "The mailbox is unchanged. Re-run to try again.")
        return 4
    except MailboxSafetyError as exc:
        _err(f"Internal safety stop (no change made to mailbox): {exc}")
        return 5
    except OSError as exc:
        _err(f"Error: {exc.__class__.__name__}: {exc}\nThe mailbox is unchanged.")
        return 4
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def _search(conn, cfg, senders, selected, excluded, args):
    if not selected:
        _err("No folders to search.")
        raise _Exit(2)
    return run_preview(
        conn, selected, excluded, senders, args.start, args.end,
        max_messages=cfg.max_messages_per_pdf,
        max_bytes=cfg.max_mb_per_pdf * 1024 * 1024,
        progress=_progress,
    )


def cmd_preview(args: argparse.Namespace) -> int:
    def body(conn, cfg, host, user, senders, selected, excluded):
        if args.list_folders:
            print(render_folders(selected, excluded))
            return 0
        result = _search(conn, cfg, senders, selected, excluded, args)
        print(render_preview(result, user, host))
        if not result.messages:
            print("\nNo matching messages found. Nothing would be exported.")
        else:
            print("\nNext: review these counts, then try `email-archiver sample` (Phase 2).")
        return 0

    return _with_mailbox(args, need_senders=not args.list_folders, body=body)


def cmd_sample(args: argparse.Namespace) -> int:
    if not args.list and args.output_dir is None:
        _err("--output-dir is required (choose a folder outside this project, e.g. ~/EmailArchive).")
        return 2
    # Validate the output location before connecting, but create it only when fetching.
    if not args.list:
        out = args.output_dir.expanduser().resolve()
        repo = inside_git_repo(out)
        if repo is not None:
            _err(f"{out} is inside the git repository {repo}. "
                 "Choose an output folder outside any repository (mail is private).")
            return 2

    def body(conn, cfg, host, user, senders, selected, excluded):
        started = datetime.now().astimezone()
        result = _search(conn, cfg, senders, selected, excluded, args)
        if not result.messages:
            print("No matching messages found. Nothing fetched.")
            return 0
        if args.list:
            print(render_message_list(result.messages))
            return 0

        if args.uid:
            try:
                picks = picks_from_specs(result.messages, args.uid)
            except ValueError as exc:
                _err(str(exc))
                return 2
            selection = "user-selected"
        else:
            picks = choose_sample(result.messages, args.count)
            selection = f"automatic ({args.count})"

        print(render_sample_plan(picks), flush=True)  # show the plan before progress lines
        try:
            run_dir = prepare_output_dir(args.output_dir)
        except OutputDirError as exc:
            _err(str(exc))
            return 2

        folder_raw = {f.name: f.raw for f in selected}
        outcomes = fetch_sample(conn, picks, run_dir, folder_raw, progress=_progress)
        write_run_metadata(
            run_dir, version=__version__, host=host, senders=senders,
            folders=[f.name for f in selected], selection=selection,
            started=started, outcomes=outcomes,
        )
        print(render_sample_result(outcomes, picks, run_dir))
        if any(not o.ok for o in outcomes):
            print("\nSome messages FAILED; see PROBLEM lines above and sample-run.json.")
            return 1
        print(f"\nNext: open the .eml files in Apple Mail to check them:  open \"{run_dir}\"")
        return 0

    return _with_mailbox(args, need_senders=True, body=body)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "preview":
            return cmd_preview(args)
        if args.command == "sample":
            return cmd_sample(args)
    except KeyboardInterrupt:
        _err("\nCanceled. The mailbox is unchanged.")
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
