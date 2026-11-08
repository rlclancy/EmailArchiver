# Email Archiver

Local command-line tool that exports every email from one or more chosen senders
in a Cox (@cox.net, Yahoo-hosted) mailbox into searchable PDFs, one per calendar year.
The mailbox is never changed and nothing leaves your machine.

See [docs/DesignDoc_EmailArchiver.txt](docs/DesignDoc_EmailArchiver.txt) for the full design.

## Layout

```
docs/                 design documents
src/email_archiver/   application package
tests/                tests
```

## Development

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
email-archiver
```

## Privacy

- The app password is prompted for at runtime and never stored.
- Archive output (`*.pdf`, `*.eml`, `attachments/`, `messages/`, manifests) is git-ignored
  because it contains private mail. Keep it outside this repo.
