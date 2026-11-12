# Attachment reader compatibility

Ticket 04 was implemented and tested against these locked versions:

- `gmail-agent 0.3.1`
- `llama-index-readers-file 0.7.0`
- `llama-index-core 0.14.25`
- `pypdf 6.21.4`
- `docx2txt 0.9`

The installed Gmail client exposes `attachments MESSAGE_ID --json`, which walks nested MIME
parts and returns filename, MIME type, reported size, part ID, attachment ID, and inline state.
Its `download MESSAGE_ID --name PART_ID --json` command downloads one occurrence without
depending on filename uniqueness.

The installed `PDFReader`, `DocxReader`, and `FlatReader` each expose
`load_data(Path, extra_info=...)`. PDF documents preserve `file_name` and page labels, DOCX
documents preserve `file_name`, and flat-text documents preserve `filename` and `extension`.
All readers merge the supplied source metadata into every returned document.

Reader-created document IDs are random and are not suitable for idempotent reindexing. The
application therefore assigns each extracted document a stable SHA-256 ID derived from the
attachment content SHA-256 and reader document ordinal. Repeated occurrences reuse the same
document. Text and merged metadata are persisted in SQLite-backed archive records and local UTF-8
files independently of Qdrant.
