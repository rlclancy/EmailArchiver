"""Read-only IMAP connection.

Every command sent to the server passes through ``ReadOnlyIMAP._command``,
which allows only a small set of non-mutating commands. This enforces the
design rule that the mailbox is never changed: no SELECT (only EXAMINE), no
STORE/COPY/MOVE/APPEND/EXPUNGE, and no FETCH item that could set \\Seen.
"""

from __future__ import annotations

import imaplib
import re
import ssl

DEFAULT_HOST = "imap.mail.yahoo.com"
DEFAULT_PORT = 993

# Commands that cannot alter mailbox contents or flags.
_ALLOWED_COMMANDS = frozenset(
    {"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "NOOP", "LOGOUT", "ID"}
)
_ALLOWED_UID_SUBCOMMANDS = frozenset({"SEARCH", "FETCH"})

# FETCH items that implicitly set \Seen: BODY[...], BINARY[...], RFC822,
# RFC822.TEXT, RFC822.HEADER. Only BODY.PEEK[...] and RFC822.SIZE are allowed.
_FORBIDDEN_FETCH_ITEM = re.compile(
    r"\bBODY\[|\bBINARY\[|\bRFC822\b(?!\.SIZE)", re.IGNORECASE
)


class MailboxSafetyError(RuntimeError):
    """Raised when code attempts an IMAP command that could alter the mailbox."""


def check_command(name: str, args: tuple) -> None:
    """Raise MailboxSafetyError unless the command is known to be read-only."""
    name = name.upper()
    if name == "UID":
        if not args:
            raise MailboxSafetyError("UID command without subcommand")
        sub = str(args[0]).upper()
        if sub not in _ALLOWED_UID_SUBCOMMANDS:
            raise MailboxSafetyError(f"UID {sub} is not permitted (read-only mode)")
        if sub == "FETCH":
            items = " ".join(
                a.decode("ascii", "replace") if isinstance(a, bytes) else str(a)
                for a in args[2:]
            )
            if _FORBIDDEN_FETCH_ITEM.search(items):
                raise MailboxSafetyError(
                    f"FETCH items {items!r} could mark messages read; use BODY.PEEK"
                )
        return
    if name not in _ALLOWED_COMMANDS:
        raise MailboxSafetyError(f"{name} is not permitted (read-only mode)")


class ReadOnlyIMAP(imaplib.IMAP4_SSL):
    """IMAP4_SSL that refuses any command able to modify the mailbox."""

    def _command(self, name, *args):  # noqa: D401 - imaplib internal hook
        check_command(name, args)
        return super()._command(name, *args)

    def select(self, mailbox="INBOX", readonly=True):
        if not readonly:
            raise MailboxSafetyError("SELECT is not permitted; mailboxes open read-only")
        return super().select(mailbox, readonly=True)


def imap_quote(value: str) -> str:
    """Quote a string as an IMAP quoted-string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def connect(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 60.0) -> ReadOnlyIMAP:
    """Open a TLS connection that verifies the server certificate (fails closed)."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return ReadOnlyIMAP(host, port, ssl_context=context, timeout=timeout)
