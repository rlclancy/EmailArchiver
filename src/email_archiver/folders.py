"""Folder discovery and Spam/Trash exclusion."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

_LIST_LINE = re.compile(
    r'^\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|"(?:[^"\\]|\\.)*")\s+(?P<name>.*)$',
    re.IGNORECASE,
)

JUNK_NAMES = frozenset({"bulk", "bulk mail", "spam", "junk", "junk e-mail", "junk email"})
TRASH_NAMES = frozenset({"trash", "deleted", "deleted items", "deleted messages"})


@dataclass(frozen=True)
class Folder:
    raw: str  # name exactly as the server reports it (modified UTF-7)
    name: str  # decoded name for display
    flags: frozenset[str]  # lowercased attributes, e.g. {"\\junk"}
    delimiter: str | None

    @property
    def selectable(self) -> bool:
        return not ({"\\noselect", "\\nonexistent"} & self.flags)


def decode_mutf7(value: str) -> str:
    """Decode an IMAP modified UTF-7 mailbox name (RFC 3501 section 5.1.3)."""
    out: list[str] = []
    i = 0
    try:
        while i < len(value):
            ch = value[i]
            if ch != "&":
                out.append(ch)
                i += 1
                continue
            end = value.index("-", i)
            if end == i + 1:
                out.append("&")
            else:
                chunk = value[i + 1 : end].replace(",", "/")
                chunk += "=" * (-len(chunk) % 4)
                out.append(base64.b64decode(chunk).decode("utf-16-be"))
            i = end + 1
    except (ValueError, UnicodeDecodeError):
        return value
    return "".join(out)


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return re.sub(r"\\(.)", r"\1", token[1:-1])
    return token


def parse_list_response(data: list) -> list[Folder]:
    """Parse the data returned by imaplib's ``list()``."""
    folders: list[Folder] = []
    for item in data:
        if item is None:
            continue
        literal: str | None = None
        if isinstance(item, tuple):
            line = item[0].decode("utf-8", "replace")
            literal = item[1].decode("utf-8", "replace")
        else:
            line = item.decode("utf-8", "replace")
        m = _LIST_LINE.match(line.strip())
        if not m:
            continue
        flags = frozenset(f.lower() for f in m.group("flags").split())
        delim_tok = m.group("delim")
        delimiter = None if delim_tok.upper() == "NIL" else _unquote(delim_tok)
        name_tok = m.group("name").strip()
        if literal is not None and re.fullmatch(r"\{\d+\}", name_tok):
            raw = literal
        else:
            raw = _unquote(name_tok)
        folders.append(Folder(raw=raw, name=decode_mutf7(raw), flags=flags, delimiter=delimiter))
    return folders


def _sort_key(folder: Folder) -> tuple[int, str]:
    return (0 if folder.raw.upper() == "INBOX" else 1, folder.name.lower())


def select_folders(
    folders: list[Folder],
    include: list[str] | None = None,
    extra_excluded: list[str] | None = None,
) -> tuple[list[Folder], list[tuple[Folder, str]]]:
    """Return (folders to search, [(excluded folder, reason)]).

    With ``include``, only the named folders are searched (exact display or raw
    name); an unknown name raises ValueError. Otherwise every selectable folder
    is searched except Spam/Trash (by SPECIAL-USE attribute, then by name) and
    any names in ``extra_excluded``.
    """
    extra = {n.lower() for n in (extra_excluded or [])}
    selected: list[Folder] = []
    excluded: list[tuple[Folder, str]] = []

    if include is not None:
        by_name = {f.name: f for f in folders} | {f.raw: f for f in folders}
        unknown = [n for n in include if n not in by_name]
        if unknown:
            raise ValueError("Unknown folder(s): " + ", ".join(unknown))
        wanted = {by_name[n].raw for n in include}
        for f in folders:
            if f.raw not in wanted:
                excluded.append((f, "not in --folders list"))
            elif not f.selectable:
                excluded.append((f, "not selectable (container only)"))
            else:
                selected.append(f)
    else:
        for f in folders:
            lname = f.name.lower()
            if not f.selectable:
                excluded.append((f, "not selectable (container only)"))
            elif "\\junk" in f.flags:
                excluded.append((f, "Spam (\\Junk attribute)"))
            elif "\\trash" in f.flags:
                excluded.append((f, "Trash (\\Trash attribute)"))
            elif lname in JUNK_NAMES:
                excluded.append((f, "Spam (name match)"))
            elif lname in TRASH_NAMES:
                excluded.append((f, "Trash (name match)"))
            elif lname in extra:
                excluded.append((f, "excluded by config"))
            else:
                selected.append(f)

    selected.sort(key=_sort_key)
    excluded.sort(key=lambda fr: _sort_key(fr[0]))
    return selected, excluded
