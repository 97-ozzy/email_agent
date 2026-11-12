# 04: Archive and extract email attachments

**What to build:** A user can retain the attachments belonging to imported messages and search-ready text can be derived from supported documents without losing unsupported, oversized, duplicated, or unreadable source files.

**Blocked by:** 03: Import and persist recent email bodies

**Status:** implemented

- [ ] Accessible attachments are saved locally and remain associated with every message in which they appear.
- [ ] Attachment bytes are keyed by SHA-256 so identical content is stored once while distinct files with the same filename remain distinct.
- [ ] PDF, DOCX, TXT, and ordinary text attachments are converted into preserved extracted text using existing readers where suitable.
- [ ] Unsupported and oversized attachments are retained but are not sent through text extraction.
- [ ] Inline images and graphical signature assets are excluded from textual indexing.
- [ ] Extraction failures are recorded per attachment and do not stop other messages or attachments from completing.
- [ ] Processing state distinguishes downloaded, parsed, indexed, skipped, unsupported, and failed outcomes as applicable.
- [ ] The CLI reports downloaded, indexed, unsupported, oversized, and failed attachment counts accurately.
- [ ] Synthetic tests cover supported formats, duplicate content, duplicate names, inline assets, oversized files, unsupported files, corrupted files, and nested MIME structures supported by the Gmail client.
- [ ] Original attachment bytes are stored in a content-addressed local archive keyed by SHA-256, while SQLite stores every message-to-attachment occurrence and its original source metadata separately.
- [ ] Existing installed-version LlamaIndex readers are used for PDF, DOCX, and TXT; no custom parser is introduced when a suitable reader exists.
- [ ] Extracted text is persisted independently of Qdrant so a later reindex does not require Gmail access, another download, or another attachment extraction pass for unchanged content.
- [ ] Before RAG implementation begins, the selected readers' installed-version interfaces, metadata behavior, and stable document-ID support are verified and the result is captured in implementation notes or tests.

