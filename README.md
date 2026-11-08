# Email Archiver

Local command-line tool that exports every email from one or more chosen senders
in a Cox (@cox.net, Yahoo-hosted) mailbox into searchable PDFs, one per calendar year.
The mailbox is never changed and nothing leaves your machine.

See [docs/DesignDoc_EmailArchiver.txt](docs/DesignDoc_EmailArchiver.txt) for the full design.

## Layout

```
docs/                 design documents
src/email_archiver/   application package
tests/                tests (no network needed)
config.example.toml   sample non-secret settings
```

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Phase 1 uses only the Python standard library (Python 3.11+).

## Phase 1: read-only preview

```sh
# See which folders exist and which would be searched
email-archiver preview --user you@cox.net --list-folders

# Count matching messages by year and folder
email-archiver preview --user you@cox.net --sender person@example.com --sender alt@example.org

# Limit to a date range (inclusive, by each message's Date header)
email-archiver preview --user you@cox.net --sender person@example.com --start 2023-01-01 --end 2023-12-31
```

You will be prompted for a Yahoo **app password** (hidden input). To create one, sign in at
login.yahoo.com with your cox.net address -> Account Security -> Generate app password.

Optional settings can go in `~/.config/email-archiver/config.toml`; see `config.example.toml`.

The preview never changes the mailbox: every command passes through a read-only guard
(`safe_imap.py`) that permits only EXAMINE, LIST, UID SEARCH and peek-only UID FETCH.

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Privacy

- The app password is prompted for at runtime and never stored.
- Archive output (`*.pdf`, `*.eml`, `attachments/`, `messages/`, manifests) is git-ignored
  because it contains private mail. Keep it outside this repo.
