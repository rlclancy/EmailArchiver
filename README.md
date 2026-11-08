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
environment.yml       conda environment
```

## Setup (conda)

```sh
conda env create -f environment.yml
conda activate email-archiver
```

This installs the package in editable mode, so code changes take effect without reinstalling.
After `environment.yml` changes: `conda env update -f environment.yml --prune`.

Phases 1–2 use only the Python standard library (Python 3.11+).

## Phase 1: read-only preview

```sh
# See which folders exist and which would be searched
email-archiver preview --user you@cox.net --list-folders

# Count matching messages by year and folder
email-archiver preview --user you@cox.net --sender person@example.com --sender alt@example.org

# Limit to a date range (inclusive, by each message's Date header)
email-archiver preview --user you@cox.net --sender person@example.com --start 2023-01-01 --end 2023-12-31
```

You need a Yahoo **app password**. To create one, sign in at login.yahoo.com with your
cox.net address -> Account Security -> Generate app password.

If `EMAIL_ARCHIVER_APP_PASSWORD` is set, the tool uses it; otherwise it prompts (hidden input).
To set it from `~/.zshrc` without keeping the password in plain text, store it once in the
macOS Keychain:

```sh
security add-generic-password -s email-archiver -a you@cox.net -w   # prompts for the password
```

then add to `~/.zshrc`:

```sh
export EMAIL_ARCHIVER_APP_PASSWORD="$(security find-generic-password -s email-archiver -a you@cox.net -w 2>/dev/null)"
```

(A plain `export EMAIL_ARCHIVER_APP_PASSWORD='...'` also works, but leaves the password readable
in `~/.zshrc` and in any backups of it.)

Optional settings can go in `~/.config/email-archiver/config.toml`; see `config.example.toml`.

The preview never changes the mailbox: every command passes through a read-only guard
(`safe_imap.py`) that permits only EXAMINE, LIST, UID SEARCH and peek-only UID FETCH.

## Phase 2: fetch a sample of original messages

```sh
# Fetch 5 varied matches (oldest, newest, largest, smallest, a year-boundary message)
email-archiver sample --user you@cox.net --sender person@example.com --output-dir ~/EmailArchive

# List every match with its FOLDER:UID, then fetch exactly the ones you choose
email-archiver sample --user you@cox.net --sender person@example.com --list
email-archiver sample --user you@cox.net --sender person@example.com --output-dir ~/EmailArchive \
  --uid Inbox:12345 --uid Inbox:12400
```

Each message is saved byte-for-byte as `.eml` in `<output-dir>/_samples/<timestamp>/`
(folder readable only by you), with `sample-run.json` holding checksums and the
before/after flag check. It contains no subjects, bodies or credentials. The tool refuses an
output folder inside a git repository. Open the `.eml` files in Apple Mail to check them.

Each message's flags (read/unread, answered, etc.) are read before and after the
download; any change is reported as a failure, and the message is not saved.

## Tests

```sh
conda activate email-archiver
python -m unittest discover -s tests
```

## Privacy

- The tool never stores the app password. It reads it from `EMAIL_ARCHIVER_APP_PASSWORD` or a
  hidden prompt, and never accepts it as a command-line argument or from the config file.
- Archive output (`*.pdf`, `*.eml`, `attachments/`, `messages/`, manifests) is git-ignored
  because it contains private mail. Keep it outside this repo.
