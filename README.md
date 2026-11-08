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

PDF rendering (Phase 3+) uses WeasyPrint, pypdf and nh3, pinned in `environment.yml`.
conda-forge supplies WeasyPrint's native libraries (pango, cairo), so Homebrew is not needed.

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

## Phase 3: render saved messages to a PDF

```sh
# Renders every .eml in a sample folder to <folder>/sample.pdf (no network needed)
email-archiver render ~/EmailArchive/_samples/20261108-114242

# Or choose files and the output path
email-archiver render a.eml b.eml --output ~/EmailArchive/test.pdf
```

The PDF opens with a table of contents (date, sender, subject, page). Each message starts on
a new page with a Date/From/To/Cc/Subject header block, in chronological order. Each message's
year comes from its own Date header and timezone. HTML is sanitized (no scripts, forms,
`<style>` blocks or positioning) and rendered fully offline. Remote images are never fetched,
and each message notes how many were blocked. Images embedded in the message itself are shown.

## Phase 4: attachments

`render` also handles attachments. Types are detected from the file contents, so a
photo labelled `application/octet-stream` is still shown as a photo.

| Attachment | In the PDF |
|---|---|
| JPEG, PNG, GIF, WebP images | shown after the message (animated GIFs: first frame) |
| TIFF, BMP, HEIC photos | converted locally (Pillow / macOS `sips`) and shown |
| PDF | its pages are drawn into the archive, numbered like the rest of the document |
| Plain text, HTML | shown in a box; HTML is sanitized like message bodies |
| Office files, video, audio, SVG, archives, encrypted or damaged files | saved separately; the PDF lists the name and the reason |

Images that already appear inside an HTML message body are not repeated.
Separately saved files go to `attachments/<year>/` next to the PDF (private folder).
They keep their original name, plus a short message tag if two files share a name. Files with no
extension get one from their detected type. `<pdf-name>-report.json` lists every
attachment's outcome and SHA-256, with no message content.

Limits: PDFs over 100 pages and images over 60 megapixels are saved instead of embedded.
Text over 200,000 characters is shown truncated, with the full file saved.

## Phase 5: full export

```sh
# 1. A limited run first: one year, then compare with the preview
email-archiver export --user you@cox.net --sender person@example.com --output-dir ~/EmailArchive --year 2015

# 2. The full export (already-downloaded messages are reused, not fetched again)
email-archiver export --user you@cox.net --sender person@example.com --output-dir ~/EmailArchive
```

It searches, shows the plan (PDFs, counts, sizes) and asks before starting (`--yes` skips the question).

```
~/EmailArchive/
  Cox-Sender-Archive-2015.pdf      one per year (or -2016-part-01.pdf … when split by month)
  messages/2015/*.eml              every original message, byte for byte
  attachments/2015/*               attachments that cannot be shown in a PDF (linked from it)
  export-manifest.json             identifiers, checksums, paths, counts; no subjects or bodies
  export.checkpoint                resume state
```

- **Resumable.** Progress is saved after every message. If the run is interrupted (Ctrl-C, sleep,
  network loss), run the same command again: verified downloads are reused and unchanged PDFs are
  not rebuilt. Network drops are retried with a fresh login (waits of 2, 4, 8, 16 and 32 s).
- **Nothing silently dropped.** The result reconciles the search count against the PDFs and lists
  every message not exported with its reason (e.g. flags changed, folder renumbered by the server).
  Re-running retries them.
- **Large years.** Years over the limit (1,500 messages or 200 MB) are either kept whole or split
  into calendar-month parts. You choose once (`--split monthly|never`, or answer the prompt) and the
  choice is remembered.
- The output folder must be outside any git repository, and holds one sender list per folder.
- `--no-manifest` skips the manifest; `--prefix` changes the PDF names.

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
